from __future__ import annotations

import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from typing import Self
from unittest.mock import patch

from agent_session_exporter.core import (
    CollectorConfig,
    Config,
    EventStore,
    ServerConfig,
)
from agent_session_exporter.remote import (
    _request_json,
    pull_events,
    sync_codex_cloud,
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
        collector=CollectorConfig(
            url="https://collector.example",
            timeout_seconds=1.0,
        ),
        server=ServerConfig(),
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


class RemoteTest(unittest.TestCase):
    def test_request_json_builds_authenticated_post(self) -> None:
        with patch(
            "agent_session_exporter.remote.urllib.request.urlopen",
            return_value=FakeResponse({"accepted": True}),
        ) as urlopen:
            response = _request_json(
                "https://collector.example/v1/events",
                method="POST",
                token="test-token",
                payload={"event": "example"},
                timeout=4.0,
            )

        self.assertEqual(response, {"accepted": True})
        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Authorization"), "Bearer test-token")
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 4.0)

    def test_request_json_reports_http_error(self) -> None:
        error = urllib.error.HTTPError(
            "https://collector.example/v1/events",
            503,
            "Service Unavailable",
            {},
            io.BytesIO(b'{"error":"unavailable"}'),
        )
        with (
            patch(
                "agent_session_exporter.remote.urllib.request.urlopen",
                side_effect=error,
            ),
            self.assertRaisesRegex(RuntimeError, "HTTP 503"),
        ):
            _request_json("https://collector.example/v1/events")

    def test_pull_recovers_from_corrupted_stored_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = config_for(Path(directory))
            cursor_key = f"remote-cursor:{config.collector.url}"
            with EventStore(config.state_dir) as store:
                store.set_metadata(cursor_key, "not-an-integer")

            with patch(
                "agent_session_exporter.remote._request_json",
                return_value={"events": [], "next_after": 0},
            ) as request_json:
                self.assertEqual(pull_events(config), (0, 0))

            self.assertIn("after=0", request_json.call_args.args[0])

    def test_pull_rejects_invalid_next_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = config_for(Path(directory))
            for value in (None, "invalid", True, 1.5):
                with (
                    self.subTest(value=value),
                    patch(
                        "agent_session_exporter.remote._request_json",
                        return_value={"events": [], "next_after": value},
                    ),
                    self.assertRaisesRegex(
                        RuntimeError,
                        "invalid next_after cursor",
                    ),
                ):
                    pull_events(config)

    def test_sync_codex_cloud_parses_and_stores_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = config_for(Path(directory))
            output = json.dumps(
                [
                    {
                        "id": "task-1",
                        "repository": "example/project",
                        "branch": "main",
                        "updated_at": "2026-07-26T00:00:00Z",
                    }
                ]
            )
            with patch(
                "agent_session_exporter.remote._run_codex_cloud",
                return_value=output,
            ) as run_cloud:
                self.assertEqual(sync_codex_cloud(config, limit=1), 1)

            run_cloud.assert_called_once_with(["list", "--json", "--limit", "1"])
            with EventStore(config.state_dir) as store:
                events = store.list_events()
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].source, "codex-cloud")
            self.assertEqual(events[0].project, "project")


if __name__ == "__main__":
    unittest.main()
