from __future__ import annotations

import io
import json
import tempfile
import unittest
import urllib.error
import urllib.request
from dataclasses import replace
from email.message import Message
from pathlib import Path
from typing import Self
from unittest.mock import patch
from urllib.response import addinfourl

from agent_session_exporter.claude_cloud import (
    _remote_envelope,
    _request_json,
    pull_events,
)
from agent_session_exporter.core import (
    ClaudeCloudConfig,
    Config,
    EventStore,
    event_fingerprint,
)


def config_for(root: Path) -> Config:
    return Config(
        vault_path=None,
        destination="archive",
        state_dir=root / "state",
        device_id="test-device",
        redact=True,
        include_tool_details=False,
        sync_on_capture=True,
        project_aliases={},
        claude_cloud=ClaudeCloudConfig(
            url="https://inbox.example",
            timeout_seconds=1.0,
        ),
    )


class FakeResponse:
    def __init__(self, body: object) -> None:
        self.body = json.dumps(body).encode("utf-8")

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.body


def remote_event(**overrides: object) -> dict[str, object]:
    event: dict[str, object] = {
        "source": "claude-cloud",
        "device_id": "claude-cloud",
        "session_id": "session-1",
        "event_name": "UserPromptSubmit",
        "occurred_at": "2026-08-01T00:00:00.000Z",
        "cwd": "/workspace/example",
        "project": "example",
        "repository": "example/project",
        "branch": "main",
        "transcript_path": "",
        "payload": {
            "session_id": "session-1",
            "hook_event_name": "UserPromptSubmit",
            "prompt": "Hello",
        },
        "received_at": "2026-08-01T00:00:01.000Z",
    }
    event.update(overrides)
    fingerprint_input = {
        key: event[key]
        for key in (
            "source",
            "device_id",
            "session_id",
            "event_name",
            "occurred_at",
            "payload",
        )
    }
    event["fingerprint"] = event_fingerprint(fingerprint_input)
    return event


class ClaudeCloudTest(unittest.TestCase):
    def test_request_json_builds_authenticated_get(self) -> None:
        with patch(
            "agent_session_exporter.claude_cloud.urllib.request.OpenerDirector.open",
            return_value=FakeResponse({"events": [], "next_after": 0}),
        ) as open_request:
            response = _request_json(
                "https://inbox.example/v1/events",
                token="pull-token",
                timeout=4.0,
            )

        self.assertEqual(response, {"events": [], "next_after": 0})
        request = open_request.call_args.args[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.get_header("Authorization"), "Bearer pull-token")
        self.assertEqual(
            request.get_header("User-agent"),
            "agent-session-exporter/0.1.0",
        )
        self.assertEqual(open_request.call_args.kwargs["timeout"], 4.0)

    def test_request_json_reports_http_error(self) -> None:
        error = urllib.error.HTTPError(
            "https://inbox.example/v1/events",
            503,
            "Service Unavailable",
            {},
            io.BytesIO(b'{"error":"unavailable"}'),
        )
        with (
            patch(
                "agent_session_exporter.claude_cloud.urllib.request.OpenerDirector.open",
                side_effect=error,
            ),
            self.assertRaisesRegex(RuntimeError, "HTTP 503"),
        ):
            _request_json("https://inbox.example/v1/events")

    def test_remote_envelope_recomputes_fingerprint_and_clears_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = config_for(Path(directory))
            value = remote_event(
                event_name="sessionEnd",
                transcript_path="/etc/passwd",
                payload={
                    "session_id": "session-1",
                    "hook_event_name": "sessionEnd",
                    "transcript_path": "/workspace/transcript.jsonl",
                    "api_key": "secret-value",
                },
            )
            value["fingerprint"] = "untrusted"
            envelope = _remote_envelope(config, value)

        self.assertEqual(envelope["event_name"], "SessionEnd")
        self.assertEqual(envelope["transcript_path"], "")
        self.assertNotIn("transcript_path", envelope["payload"])
        self.assertEqual(envelope["payload"]["api_key"], "[REDACTED]")
        self.assertNotEqual(envelope["fingerprint"], "untrusted")

    def test_pull_requires_token(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            self.assertRaisesRegex(ValueError, "PULL_TOKEN"),
        ):
            pull_events(config_for(Path(directory)))

    def test_pull_requires_https_inbox(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = config_for(Path(directory))
            config = replace(
                config,
                claude_cloud=ClaudeCloudConfig(url="http://inbox.example"),
            )
            with self.assertRaisesRegex(ValueError, "HTTPS"):
                pull_events(config)

    def test_pull_rejects_redirects_without_forwarding_token_or_advancing_cursor(self) -> None:
        for status in (301, 302, 303, 307, 308):
            for target in ("https://inbox.example/moved", "https://other.example/events",
                           "http://other.example/events"):
                with self.subTest(status=status, target=target), tempfile.TemporaryDirectory() as directory:
                    config = config_for(Path(directory))
                    cursor_key = f"remote-cursor:{config.claude_cloud.url}"
                    with EventStore(config.state_dir) as store:
                        store.set_metadata(cursor_key, "5")
                    requests = []

                    def transport(request):
                        requests.append((request.full_url, request.get_header("Authorization")))
                        headers = Message()
                        code = status if len(requests) == 1 else 200
                        if len(requests) == 1:
                            headers["Location"] = target
                        response = addinfourl(io.BytesIO(b'{"events":[],"next_after":99}'),
                                              headers, request.full_url, code)
                        response.msg = "Test response"
                        return response

                    with (
                        patch.dict("os.environ", {"AGENT_SESSION_EXPORTER_PULL_TOKEN": "pull-token"}),
                        patch("urllib.request.HTTPSHandler.https_open", side_effect=transport),
                        patch("urllib.request.HTTPHandler.http_open", side_effect=transport),
                        self.assertRaisesRegex(RuntimeError, f"HTTP {status}"),
                    ):
                        pull_events(config)
                    self.assertEqual(requests, [("https://inbox.example/v1/events?after=5&limit=500",
                                                 "Bearer pull-token")])
                    with EventStore(config.state_dir) as store:
                        self.assertEqual(store.get_metadata(cursor_key), "5")
                        self.assertEqual(store.list_events(), [])

    def test_pull_recovers_from_corrupted_cursor_and_imports(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = config_for(Path(directory))
            cursor_key = f"remote-cursor:{config.claude_cloud.url}"
            with EventStore(config.state_dir) as store:
                store.set_metadata(cursor_key, "not-an-integer")

            with (
                patch.dict(
                    "os.environ",
                    {"AGENT_SESSION_EXPORTER_PULL_TOKEN": "pull-token"},
                ),
                patch(
                    "agent_session_exporter.claude_cloud._request_json",
                    return_value={"events": [remote_event()], "next_after": 1},
                ) as request_json,
            ):
                self.assertEqual(pull_events(config), (1, 1))

            self.assertIn("after=0", request_json.call_args.args[0])
            with EventStore(config.state_dir) as store:
                events = store.list_events()
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].source, "claude-cloud")

    def test_pull_rejects_invalid_next_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = config_for(Path(directory))
            for value in (None, "invalid", True, 1.5):
                with (
                    self.subTest(value=value),
                    patch.dict(
                        "os.environ",
                        {"AGENT_SESSION_EXPORTER_PULL_TOKEN": "pull-token"},
                    ),
                    patch(
                        "agent_session_exporter.claude_cloud._request_json",
                        return_value={"events": [], "next_after": value},
                    ),
                    self.assertRaisesRegex(
                        RuntimeError,
                        "invalid next_after cursor",
                    ),
                ):
                    pull_events(config)


if __name__ == "__main__":
    unittest.main()
