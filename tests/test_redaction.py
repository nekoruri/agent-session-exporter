from __future__ import annotations

import io
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

from agent_session_exporter.claude_cloud import _remote_envelope
from agent_session_exporter.cli import main
from agent_session_exporter.codex_cloud import exec_codex_cloud, sync_codex_cloud
from agent_session_exporter.core import ClaudeCloudConfig, Config, EventStore, normalize_event
from agent_session_exporter.importers import import_export
from agent_session_exporter.redaction import REDACTED, redact_text, redact_value
from agent_session_exporter.renderer import sync_session, sync_vault

# Synthetic values with provider-valid shapes, never real credentials.
GITHUB = "ghp_" + "a" * 36
OPENAI = "sk-" + "a" * 20 + "T3BlbkFJ" + "b" * 20


def config_for(root: Path, *, redact: bool = True) -> Config:
    return Config(
        vault_path=root / "vault", destination="archive", state_dir=root / "state",
        device_id="test-device", redact=redact, include_tool_details=False,
        sync_on_capture=True, project_aliases={}, claude_cloud=ClaudeCloudConfig(),
    )


def note_content(config: Config) -> str:
    return "\n".join(path.read_text() for path in config.vault_path.rglob("*.md"))


class RedactionTest(unittest.TestCase):
    def test_missing_session_id_deduplicates_after_redaction(self) -> None:
        for enabled in (True, False):
            with self.subTest(redact=enabled), tempfile.TemporaryDirectory() as directory:
                config = config_for(Path(directory), redact=enabled)
                events = [normalize_event({
                    "hook_event_name": "Stop", "timestamp": "2026-09-12T00:00:00Z",
                    "password": secret,
                }, "claude-code", config, inspect_cwd=False)
                    for secret in ("synthetic-one", "synthetic-two")]
                self.assertEqual(events[0]["session_id"] == events[1]["session_id"], enabled)
                self.assertEqual(events[0]["fingerprint"] == events[1]["fingerprint"], enabled)
                with EventStore(config.state_dir) as store:
                    for event in events:
                        store.add_event(event)
                    self.assertEqual(len(store.list_events()), 1 if enabled else 2)

    def test_exec_without_task_id_keeps_each_submission_independent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = config_for(Path(directory))
            ids = [UUID(int=1), UUID(int=2)]
            with (
                patch("agent_session_exporter.codex_cloud.now_iso", return_value="2026-09-12T00:00:00Z"),
                patch("agent_session_exporter.codex_cloud.uuid4", side_effect=ids),
                patch("agent_session_exporter.codex_cloud._run_codex_cloud", return_value=OPENAI),
            ):
                for _ in ids:
                    self.assertEqual(exec_codex_cloud(config, GITHUB, environment="test"), REDACTED)
            with EventStore(config.state_dir) as store:
                events = store.list_events()
                self.assertEqual([event.session_id for event in events], [value.hex for value in ids])
                for event in events:
                    self.assertEqual(event.payload["task_id"], event.session_id)
                    self.assertEqual(event.payload["prompt"], REDACTED)
                    self.assertEqual(event.payload["output"], REDACTED)

    def test_detected_identifiers_preserve_sessions_devices_and_rendering(self) -> None:
        opaque = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
        for enabled in (True, False):
            with self.subTest(redact=enabled), tempfile.TemporaryDirectory() as directory:
                config = config_for(Path(directory), redact=enabled)
                with EventStore(config.state_dir) as store:
                    for device in (opaque, opaque[::-1]):
                        for session in (opaque, opaque[::-1]):
                            event = normalize_event({
                                "session_id": session, "hook_event_name": "UserPromptSubmit",
                                "prompt": "Hello", "timestamp": "2026-09-12T00:00:00Z",
                            }, "claude-code", config, device_id=device, inspect_cwd=False)
                            self.assertEqual(event["payload"]["session_id"], event["session_id"])
                            if enabled:
                                self.assertNotIn(opaque, json.dumps(event))
                                self.assertNotIn(opaque[::-1], json.dumps(event))
                                self.assertEqual(redact_value(event), event)
                            for _ in range(2):
                                store.add_event(event)
                    self.assertEqual(len(store.list_session_keys()), 4)
                    self.assertEqual(len(store.list_events()), 4)
                self.assertEqual(sync_vault(config), (4, 0))
                self.assertEqual(len(list(config.vault_path.rglob("*.md"))), 4)
                self.assertEqual(sync_vault(config), (0, 4))
                if enabled:
                    self.assertNotIn(opaque, note_content(config))
                    self.assertNotIn(opaque[::-1], note_content(config))
        # All accepted aliases use the same pseudonym as the envelope's canonical key.
        aliases = {key: GITHUB for key in ("id", "sessionId", "task_id", "taskId", "deviceId")}
        pseudonym = redact_value(GITHUB, "session_id")
        self.assertEqual(set(redact_value(aliases).values()), {pseudonym})

    def test_full_tokens_and_all_assignments_are_masked_offline(self) -> None:
        text = f'{GITHUB} {OPENAI}\npassword = "one123"; password = "two456"'
        with (
            patch("requests.sessions.Session.request", side_effect=AssertionError("network")) as request,
            patch("detect_secrets.plugins.base.BasePlugin.verify", side_effect=AssertionError("verify")) as verify,
        ):
            result = redact_text(text)
        for secret in (GITHUB, "a" * 36, OPENAI, "one123", "two456"):
            self.assertNotIn(secret, result)
        self.assertEqual(redact_text(result), result)
        request.assert_not_called()
        verify.assert_not_called()

    def test_fields_urls_private_keys_and_allowlist_comments(self) -> None:
        private_key = "-----BEGIN PRIVATE KEY-----\nMI" + "A" * 128 + "\n-----END PRIVATE KEY-----"
        value = {
            "apiKey": "arbitrary!short", "privateKey": "unrecognized-format",
            "nested": [{"accessToken": "another-secret"}],
            "text": f"{GITHUB} # pragma: allowlist secret",
            "repository": "https://user:p%40ss!word@example.invalid/repo.git?token=short&view=1",
            "pem": private_key,
            "authorization_text": "Bearer short",
        }
        result = redact_value(value)
        self.assertEqual(result["apiKey"], REDACTED)
        self.assertEqual(result["privateKey"], REDACTED)
        self.assertEqual(result["nested"], [{"accessToken": REDACTED}])
        self.assertEqual(result["pem"], REDACTED)
        self.assertNotIn(GITHUB, result["text"])
        self.assertNotIn("user:", result["repository"])
        self.assertNotIn("p%40ss", result["repository"])
        self.assertNotIn("token=short", result["repository"])
        self.assertIn("view=1", result["repository"])
        self.assertEqual(redact_text("Bearer short"), REDACTED)
        self.assertEqual(value["apiKey"], "arbitrary!short")

    def test_normal_text_and_structure_survive(self) -> None:
        value = {"prompt": "Please fix issue 123 at https://example.invalid/docs.",
                 "count": 3, "done": False, "nested": [None, "203.0.113.1"],
                 "session_id": "12345678-1234-1234-1234-123456789012"}
        self.assertEqual(redact_value(value), value)

    def test_unquoted_opaque_tokens_use_library_entropy_detection(self) -> None:
        opaque = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz" * 2
        self.assertEqual(redact_text(f"Use {opaque} here."), f"Use {REDACTED} here.")
        # Hex commit/session identities must not be treated as opaque API tokens.
        commit = "0123456789abcdef" * 4
        self.assertEqual(redact_text(commit), commit)

    def test_cli_errors_do_not_echo_credentials_even_if_detection_fails(self) -> None:
        for failed in (False, True):
            with self.subTest(detector_failed=failed):
                stderr = io.StringIO()
                with (
                    patch("agent_session_exporter.cli.run", side_effect=RuntimeError(GITHUB)),
                    patch("sys.stderr", stderr),
                    patch("agent_session_exporter.cli.redact_text",
                          side_effect=RuntimeError("unavailable") if failed else redact_text),
                    self.assertRaises(SystemExit) as exit_context,
                ):
                    main()
                self.assertEqual(exit_context.exception.code, 1)
                self.assertNotIn(GITHUB, stderr.getvalue())
                self.assertNotIn("a" * 36, stderr.getvalue())

    def test_transcripts_are_masked_after_loading_in_both_sync_paths(self) -> None:
        for source in ("codex-cli", "claude-code"):
            for enabled in (True, False):
                with self.subTest(source=source, redact=enabled), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    config = config_for(root, redact=enabled)
                    path = root / "transcript.jsonl"
                    row = ({"type": "event_msg", "payload": {"type": "user_message", "message": GITHUB}}
                           if source == "codex-cli" else
                           {"type": "user", "message": {"role": "user", "content": GITHUB}})
                    path.write_text(json.dumps(row) + "\n")
                    envelope = normalize_event({"session_id": "session-1", "hook_event_name": "Stop",
                                                "transcript_path": str(path)}, source, config)
                    with EventStore(config.state_dir) as store:
                        store.add_event(envelope)
                    self.assertTrue(sync_session(config, source, config.device_id, "session-1"))
                    self.assertEqual(GITHUB in note_content(config), not enabled)
                    row = ({"type": "event_msg", "payload": {"type": "agent_message", "message": OPENAI}}
                           if source == "codex-cli" else
                           {"type": "assistant", "message": {"role": "assistant", "content": OPENAI}})
                    with path.open("a") as stream:
                        stream.write(json.dumps(row) + "\n")
                    self.assertEqual(sync_vault(config), (1, 0))
                    self.assertEqual(OPENAI in note_content(config), not enabled)
                    self.assertEqual(sync_vault(config), (0, 1))
                    self.assertIn(GITHUB, path.read_text())

    def test_split_messages_are_masked_after_assembly(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = config_for(Path(directory))
            with EventStore(config.state_dir) as store:
                for index, chunk in enumerate((GITHUB[:4], GITHUB[4:])):
                    store.add_event(normalize_event({
                        "session_id": "session-1", "hook_event_name": "MessageDisplay",
                        "message_id": "message-1", "delta": chunk, "index": index,
                        "final": index == 1,
                    }, "claude-cloud", config, inspect_cwd=False))
            sync_vault(config)
            self.assertNotIn(GITHUB, note_content(config))
            self.assertNotIn(GITHUB[4:], note_content(config))
            self.assertIn(REDACTED, note_content(config))

    def test_cloud_sync_and_exec_mask_database_diff_and_cli_output(self) -> None:
        for enabled in (True, False):
            with self.subTest(redact=enabled), tempfile.TemporaryDirectory() as directory:
                config = config_for(Path(directory), redact=enabled)
                task = {"id": "task-1", "prompt": GITHUB, "api_key": "arbitrary!short",
                        "updated_at": "2026-09-12T00:00:00Z"}
                for expected in (1, 0):
                    with patch("agent_session_exporter.codex_cloud._run_codex_cloud",
                               side_effect=[json.dumps([task]), OPENAI, "+" + GITHUB]):
                        self.assertEqual(sync_codex_cloud(config, include_details=True), expected)
                response = json.dumps({"id": "task-2", "message": OPENAI})
                with patch("agent_session_exporter.codex_cloud._run_codex_cloud", return_value=response) as run:
                    output = exec_codex_cloud(config, GITHUB, environment="test-environment")
                    run.assert_called_once_with(["exec", "--env", "test-environment", GITHUB])
                self.assertEqual(OPENAI in output, not enabled)
                with EventStore(config.state_dir) as store:
                    stored = json.dumps([event.to_envelope() for event in store.list_events()])
                    self.assertEqual(len(store.list_events()), 2)
                sync_vault(config)
                for token in (GITHUB, OPENAI):
                    self.assertEqual(token in stored, not enabled)
                    self.assertEqual(token in note_content(config), not enabled)
                self.assertEqual("arbitrary!short" in stored, not enabled)

    def test_git_enrichment_and_remote_metadata_are_sanitized(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = config_for(Path(directory))
            origin = "https://user:p%40ss!word@example.invalid/repo.git"
            with patch("agent_session_exporter.core._run_git", side_effect=[directory, origin, "main"]):
                envelope = normalize_event({"session_id": "session-1", "cwd": directory,
                                            "hook_event_name": "Stop"}, "codex-cli", config)
            self.assertEqual(envelope["repository"], "https://example.invalid/repo.git")
            with EventStore(config.state_dir) as store:
                store.add_event(envelope)
                self.assertNotIn("p%40ss", json.dumps(store.list_events()[0].to_envelope()))
            sync_vault(config)
            self.assertNotIn("p%40ss", note_content(config))
            remote = dict(envelope, source="claude-cloud", repository=origin, branch=GITHUB,
                          transcript_path="/outside/private.jsonl")
            cleaned = _remote_envelope(config, remote)
            self.assertNotIn("p%40ss", json.dumps(cleaned))
            self.assertEqual(cleaned["branch"], REDACTED)
            self.assertEqual(cleaned["transcript_path"], "")

    def test_imported_title_and_body_are_masked(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = config_for(root)
            path = root / "conversations.json"
            path.write_text(json.dumps([{"uuid": "import-1", "name": GITHUB,
                                         "chat_messages": [{"sender": "human", "text": OPENAI}]}]))
            self.assertEqual(import_export(path, config, source="claude"), (1, 1))
            with EventStore(config.state_dir) as store:
                stored = json.dumps(store.list_events()[0].to_envelope())
            sync_vault(config)
            for token in (GITHUB, OPENAI):
                self.assertNotIn(token, stored)
                self.assertNotIn(token, note_content(config))

    def test_existing_events_are_masked_on_render_without_rewriting_database(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = config_for(Path(directory))
            with EventStore(config.state_dir) as store:
                store.add_event(normalize_event({"session_id": "session-1", "prompt": GITHUB,
                                                "hook_event_name": "UserPromptSubmit"},
                                               "claude-code", replace(config, redact=False)))
            sync_vault(replace(config, redact=False))
            self.assertIn(GITHUB, note_content(config))
            self.assertEqual(sync_vault(config), (1, 0))
            self.assertNotIn(GITHUB, note_content(config))
            with EventStore(config.state_dir) as store:
                self.assertIn(GITHUB, json.dumps(store.list_events()[0].to_envelope()))

    def test_detector_failure_does_not_store_or_render_plaintext(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = config_for(Path(directory))
            response = json.dumps([{"id": "task-1", "prompt": GITHUB}])
            with (
                patch("agent_session_exporter.redaction._detectors", side_effect=RuntimeError("detector unavailable")),
                patch("agent_session_exporter.codex_cloud._run_codex_cloud", return_value=response),
                self.assertRaisesRegex(RuntimeError, "detector unavailable"),
            ):
                sync_codex_cloud(config)
            with EventStore(config.state_dir) as store:
                self.assertEqual(store.list_events(), [])
                store.add_event(normalize_event({"session_id": "session-1", "prompt": GITHUB},
                                               "claude-code", replace(config, redact=False)))
            with (
                patch("agent_session_exporter.redaction._detectors", side_effect=RuntimeError("detector unavailable")),
                self.assertRaisesRegex(RuntimeError, "detector unavailable"),
            ):
                sync_vault(config)
            self.assertEqual(note_content(config), "")


if __name__ == "__main__":
    unittest.main()
