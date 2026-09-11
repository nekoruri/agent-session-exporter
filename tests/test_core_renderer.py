from __future__ import annotations

import io
import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from agent_session_exporter.adapters import (
    build_session_document,
    parse_claude_transcript,
    parse_codex_transcript,
)
from agent_session_exporter.cli import run
from agent_session_exporter.core import (
    DEFAULT_DESTINATION,
    ClaudeCloudConfig,
    Config,
    EventStore,
    detect_local_timezone,
    load_config,
    normalize_event,
    render_initial_config,
)
from agent_session_exporter.renderer import session_title, sync_vault


def config_for(root: Path) -> Config:
    return Config(
        vault_path=root / "vault",
        destination=DEFAULT_DESTINATION,
        state_dir=root / "state",
        device_id="test-device",
        redact=True,
        include_tool_details=False,
        sync_on_capture=True,
        project_aliases={},
        claude_cloud=ClaudeCloudConfig(),
    )


class CoreRendererTest(unittest.TestCase):
    def test_redacts_and_deduplicates_events(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = config_for(Path(directory))
            payload = {
                "session_id": "session-1",
                "hook_event_name": "UserPromptSubmit",
                "prompt": "use sk-secretsecretsecret",
                "api_key": "do-not-store",
            }
            envelope = normalize_event(payload, "codex-cli", config)
            self.assertEqual(envelope["payload"]["api_key"], "[REDACTED]")
            self.assertIn("[REDACTED]", envelope["payload"]["prompt"])
            with EventStore(config.state_dir) as store:
                first_id, first_inserted = store.add_event(envelope)
                second_id, second_inserted = store.add_event(envelope)
            self.assertTrue(first_inserted)
            self.assertFalse(second_inserted)
            self.assertEqual(first_id, second_id)

    def test_normalizes_event_name_and_single_workspace_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = config_for(Path(directory))
            envelope = normalize_event(
                {
                    "session_id": "desktop-1",
                    "hook_event_name": "sessionEnd",
                    "workspace_roots": ["/home/masa/work/zenn-content"],
                },
                "claude-code",
                config,
                inspect_cwd=False,
            )
            self.assertEqual(envelope["event_name"], "SessionEnd")
            self.assertEqual(envelope["payload"]["hook_event_name"], "SessionEnd")
            self.assertEqual(envelope["cwd"], "/home/masa/work/zenn-content")
            self.assertEqual(envelope["project"], "zenn-content")

            with EventStore(config.state_dir) as store:
                store.add_event(envelope)
                document = build_session_document(
                    store.session_events("claude-code", "test-device", "desktop-1")
                )
            self.assertEqual(document.status, "completed")

    def test_does_not_guess_from_multiple_workspace_roots(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = config_for(Path(directory))
            envelope = normalize_event(
                {
                    "session_id": "desktop-2",
                    "hook_event_name": "sessionEnd",
                    "workspace_roots": ["/workspace/one", "/workspace/two"],
                },
                "claude-code",
                config,
                inspect_cwd=False,
            )
            self.assertEqual(envelope["cwd"], "")
            self.assertEqual(envelope["project"], "unknown")

    def test_sync_writes_stable_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = config_for(root)
            payloads = [
                {
                    "session_id": "session-2",
                    "hook_event_name": "UserPromptSubmit",
                    "timestamp": "2026-07-26T01:02:03+00:00",
                    "project": "demo",
                    "prompt": "Explain the failing test.",
                },
                {
                    "session_id": "session-2",
                    "hook_event_name": "Stop",
                    "timestamp": "2026-07-26T01:03:03+00:00",
                    "project": "demo",
                    "last_assistant_message": "The fixture is missing.",
                },
            ]
            with EventStore(config.state_dir) as store:
                for payload in payloads:
                    store.add_event(normalize_event(payload, "codex-cli", config))

            with patch(
                "agent_session_exporter.renderer.now_iso",
                return_value="2026-07-26T01:04:00+00:00",
            ):
                self.assertEqual(sync_vault(config), (1, 0))
            notes = list((root / "vault").rglob("*.md"))
            self.assertEqual(len(notes), 1)
            self.assertEqual(
                notes[0].relative_to(root / "vault").parts[:3],
                ("ai-sessions", "2026", "07"),
            )
            markdown = notes[0].read_text(encoding="utf-8")
            self.assertEqual(sync_vault(config), (0, 1))
            self.assertEqual(notes[0].read_text(encoding="utf-8"), markdown)
            self.assertIn("# Explain the failing test.", markdown)
            self.assertIn("## Assistant", markdown)
            self.assertIn("The fixture is missing.", markdown)
            self.assertIn('updated_at: "2026-07-26T01:03:03+00:00"', markdown)
            self.assertIn('rendered_at: "2026-07-26T01:04:00+00:00"', markdown)
            self.assertNotIn("archived_at:", markdown)
            self.assertIn('content_kind: "transcript"', markdown)
            self.assertIn("message_count: 2", markdown)
            self.assertIn("event_count: 2", markdown)
            self.assertRegex(markdown, r'revision: "[0-9a-f]{64}"')

    def test_metadata_only_session_has_ingest_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = config_for(root)
            envelope = normalize_event(
                {
                    "session_id": "metadata-only-1",
                    "hook_event_name": "SessionEnd",
                    "timestamp": "2026-07-26T02:00:00+00:00",
                    "project": "demo",
                },
                "claude-code",
                config,
            )
            with EventStore(config.state_dir) as store:
                store.add_event(envelope)

            self.assertEqual(sync_vault(config), (1, 0))
            note = next((root / "vault").rglob("*.md"))
            markdown = note.read_text(encoding="utf-8")
            self.assertIn('content_kind: "metadata_only"', markdown)
            self.assertIn("message_count: 0", markdown)
            self.assertIn("event_count: 1", markdown)

    def test_session_revision_changes_only_when_events_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = config_for(Path(directory))
            first = normalize_event(
                {
                    "session_id": "revision-1",
                    "hook_event_name": "UserPromptSubmit",
                    "timestamp": "2026-07-26T03:00:00+00:00",
                    "prompt": "First",
                },
                "codex-cli",
                config,
            )
            second = normalize_event(
                {
                    "session_id": "revision-1",
                    "hook_event_name": "Stop",
                    "timestamp": "2026-07-26T03:01:00+00:00",
                    "last_assistant_message": "Second",
                },
                "codex-cli",
                config,
            )
            with EventStore(config.state_dir) as store:
                store.add_event(first)
                initial = build_session_document(
                    store.session_events("codex-cli", "test-device", "revision-1")
                )
                repeated = build_session_document(
                    store.session_events("codex-cli", "test-device", "revision-1")
                )
                store.add_event(second)
                updated = build_session_document(
                    store.session_events("codex-cli", "test-device", "revision-1")
                )

            self.assertEqual(initial.revision, repeated.revision)
            self.assertNotEqual(initial.revision, updated.revision)
            self.assertEqual(updated.event_count, 2)

    def test_completed_session_sets_archived_at(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = config_for(root)
            envelope = normalize_event(
                {
                    "session_id": "completed-1",
                    "hook_event_name": "SessionEnd",
                    "timestamp": "2026-07-26T02:00:00+00:00",
                    "project": "demo",
                },
                "claude-code",
                config,
            )
            with EventStore(config.state_dir) as store:
                store.add_event(envelope)
            with patch(
                "agent_session_exporter.renderer.now_iso",
                return_value="2026-07-26T02:01:00+00:00",
            ):
                self.assertEqual(sync_vault(config), (1, 0))

            note = next((root / "vault").rglob("*.md"))
            markdown = note.read_text(encoding="utf-8")
            self.assertIn('updated_at: "2026-07-26T02:00:00+00:00"', markdown)
            self.assertIn('rendered_at: "2026-07-26T02:01:00+00:00"', markdown)
            self.assertIn('archived_at: "2026-07-26T02:00:00+00:00"', markdown)

    def test_existing_render_state_schema_adds_source_hash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory) / "state"
            state_dir.mkdir()
            database = state_dir / "events.sqlite3"
            connection = sqlite3.connect(database)
            connection.execute(
                """
                CREATE TABLE render_state (
                    session_key TEXT PRIMARY KEY,
                    content_hash TEXT NOT NULL,
                    note_path TEXT NOT NULL,
                    rendered_at TEXT NOT NULL
                )
                """
            )
            connection.commit()
            connection.close()

            with EventStore(state_dir) as store:
                columns = {
                    str(row["name"])
                    for row in store.connection.execute(
                        "PRAGMA table_info(render_state)"
                    )
                }
            self.assertIn("source_hash", columns)

    def test_default_destination_is_at_the_vault_root(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch(
                "agent_session_exporter.core.detect_local_timezone",
                return_value="Asia/Tokyo",
            ),
        ):
            config = load_config(Path(directory) / "missing.toml")
        self.assertEqual(config.destination, "ai-sessions")
        self.assertEqual(config.path_timezone, "Asia/Tokyo")

    def test_config_without_path_timezone_uses_detected_local_timezone(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.toml"
            config_path.write_text('destination = "archive"\n', encoding="utf-8")
            with patch(
                "agent_session_exporter.core.detect_local_timezone",
                return_value="America/New_York",
            ) as detect:
                config = load_config(config_path)

        self.assertEqual(config.path_timezone, "America/New_York")
        detect.assert_called_once_with()

    def test_loads_claude_cloud_config_and_legacy_collector_name(self) -> None:
        for section in ("claude_cloud", "collector"):
            with (
                self.subTest(section=section),
                tempfile.TemporaryDirectory() as directory,
            ):
                config_path = Path(directory) / "config.toml"
                config_path.write_text(
                    (
                        f"[{section}]\n"
                        'url = "https://inbox.example/"\n'
                        'token_env = "CUSTOM_PULL_TOKEN"\n'
                        "timeout_seconds = 7.0\n"
                    ),
                    encoding="utf-8",
                )
                config = load_config(config_path)

            self.assertEqual(config.claude_cloud.url, "https://inbox.example")
            self.assertEqual(config.claude_cloud.token_env, "CUSTOM_PULL_TOKEN")
            self.assertEqual(config.claude_cloud.timeout_seconds, 7.0)

    def test_initial_config_uses_claude_cloud_pull_token(self) -> None:
        content = render_initial_config(Path("/tmp/vault"), "archive", "UTC")
        self.assertIn("[claude_cloud]", content)
        self.assertIn("AGENT_SESSION_EXPORTER_PULL_TOKEN", content)
        self.assertNotIn("[collector]", content)

    def test_detect_local_timezone_skips_invalid_hints_and_falls_back(self) -> None:
        with patch(
            "agent_session_exporter.core._local_timezone_candidates",
            return_value=("Not/A-Timezone", "Asia/Tokyo"),
        ):
            self.assertEqual(detect_local_timezone(), "Asia/Tokyo")
        with patch(
            "agent_session_exporter.core._local_timezone_candidates",
            return_value=("Not/A-Timezone",),
        ):
            self.assertEqual(detect_local_timezone(), "UTC")

    def test_init_writes_detected_local_timezone(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.toml"
            with (
                patch(
                    "agent_session_exporter.core.detect_local_timezone",
                    return_value="Asia/Tokyo",
                ),
                patch("sys.stdout", new_callable=io.StringIO),
            ):
                exit_code = run(
                    [
                        "--config",
                        str(config_path),
                        "init",
                        "--vault",
                        str(root / "vault"),
                    ]
                )

            self.assertEqual(exit_code, 0)
            self.assertIn(
                'path_timezone = "Asia/Tokyo"',
                config_path.read_text(encoding="utf-8"),
            )

    def test_path_timezone_controls_note_directory_and_filename(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = replace(config_for(root), path_timezone="Asia/Tokyo")
            envelope = normalize_event(
                {
                    "session_id": "timezone-1",
                    "hook_event_name": "UserPromptSubmit",
                    "timestamp": "2026-07-31T16:42:16+00:00",
                    "project": "demo",
                    "prompt": "Use local date.",
                },
                "codex-cli",
                config,
            )
            with EventStore(config.state_dir) as store:
                store.add_event(envelope)

            self.assertEqual(sync_vault(config), (1, 0))
            note = next((root / "vault").rglob("*.md"))
            relative = note.relative_to(root / "vault")
            self.assertEqual(relative.parts[:3], ("ai-sessions", "2026", "08"))
            self.assertTrue(relative.name.startswith("2026-08-01-0142-"))

    def test_load_config_rejects_unknown_path_timezone(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.toml"
            config_path.write_text(
                'path_timezone = "Not/A-Timezone"\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "Unknown path_timezone"):
                load_config(config_path)

    def test_codex_and_claude_transcript_adapters(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            codex_path = root / "codex.jsonl"
            codex_rows = [
                {
                    "type": "event_msg",
                    "timestamp": "2026-01-01T00:00:00Z",
                    "payload": {
                        "type": "user_message",
                        "message": "Question",
                    },
                },
                {
                    "type": "event_msg",
                    "timestamp": "2026-01-01T00:00:01Z",
                    "payload": {
                        "type": "agent_message",
                        "message": "Answer",
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "name": "shell",
                        "arguments": "secret tool detail",
                    },
                },
            ]
            codex_path.write_text(
                "\n".join(json.dumps(row) for row in codex_rows) + "\n",
                encoding="utf-8",
            )
            messages, _ = parse_codex_transcript(codex_path)
            self.assertEqual(
                [message.text for message in messages], ["Question", "Answer"]
            )

            claude_path = root / "claude.jsonl"
            claude_rows = [
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": "Hello"}],
                    },
                },
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "text", "text": "Hi"},
                            {"type": "tool_use", "input": {"token": "hidden"}},
                        ],
                    },
                },
            ]
            claude_path.write_text(
                "\n".join(json.dumps(row) for row in claude_rows) + "\n",
                encoding="utf-8",
            )
            messages, _ = parse_claude_transcript(claude_path)
            self.assertEqual([message.text for message in messages], ["Hello", "Hi"])

    def test_title_prefers_hook_prompt_over_transcript_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = config_for(root)
            transcript = root / "codex-title.jsonl"
            rows = [
                {
                    "type": "event_msg",
                    "timestamp": "2026-01-01T00:00:00Z",
                    "payload": {
                        "type": "user_message",
                        "message": (
                            "# AGENTS.md instructions for /workspace\n"
                            "<environment_context>generated</environment_context>"
                        ),
                    },
                },
                {
                    "type": "event_msg",
                    "timestamp": "2026-01-01T00:00:01Z",
                    "payload": {
                        "type": "user_message",
                        "message": "Actual question from transcript",
                    },
                },
            ]
            transcript.write_text(
                "\n".join(json.dumps(row) for row in rows) + "\n",
                encoding="utf-8",
            )
            envelope = normalize_event(
                {
                    "session_id": "title-1",
                    "hook_event_name": "UserPromptSubmit",
                    "timestamp": "2026-01-01T00:00:01Z",
                    "transcript_path": str(transcript),
                    "prompt": "Actual question from hook",
                },
                "codex-cli",
                config,
            )
            with EventStore(config.state_dir) as store:
                store.add_event(envelope)
                document = build_session_document(
                    store.session_events("codex-cli", "test-device", "title-1")
                )

            self.assertEqual(session_title(document), "Actual question from hook")
            self.assertEqual(
                [message.text for message in document.messages[:2]],
                [
                    (
                        "# AGENTS.md instructions for /workspace\n"
                        "<environment_context>generated</environment_context>"
                    ),
                    "Actual question from transcript",
                ],
            )

    def test_title_skips_control_messages_without_hook_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = config_for(root)
            transcript = root / "claude-title.jsonl"
            rows = [
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "<environment_context>generated"}
                        ],
                    },
                },
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "Actual transcript question"}
                        ],
                    },
                },
            ]
            transcript.write_text(
                "\n".join(json.dumps(row) for row in rows) + "\n",
                encoding="utf-8",
            )
            envelope = normalize_event(
                {
                    "session_id": "title-2",
                    "hook_event_name": "SessionEnd",
                    "timestamp": "2026-01-01T00:01:00Z",
                    "transcript_path": str(transcript),
                },
                "claude-code",
                config,
            )
            with EventStore(config.state_dir) as store:
                store.add_event(envelope)
                document = build_session_document(
                    store.session_events("claude-code", "test-device", "title-2")
                )

            self.assertEqual(session_title(document), "Actual transcript question")

    def test_streamed_cloud_messages_keep_turn_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = config_for(Path(directory))
            payloads = [
                {
                    "session_id": "cloud-1",
                    "hook_event_name": "UserPromptSubmit",
                    "prompt": "First",
                },
                {
                    "session_id": "cloud-1",
                    "hook_event_name": "MessageDisplay",
                    "message_id": "message-1",
                    "index": 0,
                    "final": False,
                    "delta": "Part ",
                },
                {
                    "session_id": "cloud-1",
                    "hook_event_name": "MessageDisplay",
                    "message_id": "message-1",
                    "index": 1,
                    "final": True,
                    "delta": "one",
                },
                {
                    "session_id": "cloud-1",
                    "hook_event_name": "Stop",
                    "last_assistant_message": "Part one",
                },
                {
                    "session_id": "cloud-1",
                    "hook_event_name": "UserPromptSubmit",
                    "prompt": "Second",
                },
                {
                    "session_id": "cloud-1",
                    "hook_event_name": "MessageDisplay",
                    "message_id": "message-2",
                    "index": 0,
                    "final": True,
                    "delta": "Part two",
                },
            ]
            with EventStore(config.state_dir) as store:
                for index, payload in enumerate(payloads):
                    payload["timestamp"] = f"2026-01-01T00:00:0{index}Z"
                    store.add_event(
                        normalize_event(
                            payload,
                            "claude-cloud",
                            config,
                            inspect_cwd=False,
                        )
                    )
                events = store.session_events(
                    "claude-cloud",
                    "test-device",
                    "cloud-1",
                )
            document = build_session_document(events)
            self.assertEqual(
                [(message.role, message.text) for message in document.messages],
                [
                    ("user", "First"),
                    ("assistant", "Part one"),
                    ("user", "Second"),
                    ("assistant", "Part two"),
                ],
            )

    def test_capture_immediately_syncs_the_changed_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.toml"
            vault = root / "vault"
            config_path.write_text(
                (
                    f"state_dir = {json.dumps(str(root / 'state'))}\n"
                    f"{render_initial_config(vault, 'archive')}"
                ),
                encoding="utf-8",
            )
            payload = {
                "session_id": "automatic-1",
                "hook_event_name": "UserPromptSubmit",
                "timestamp": "2026-07-26T00:00:00Z",
                "prompt": "Archive this automatically.",
            }
            with patch("sys.stdin", io.StringIO(json.dumps(payload))):
                exit_code = run(
                    [
                        "--config",
                        str(config_path),
                        "capture",
                        "--source",
                        "codex-cli",
                    ]
                )
            self.assertEqual(exit_code, 0)
            notes = list(vault.rglob("*.md"))
            self.assertEqual(len(notes), 1)
            self.assertIn(
                "Archive this automatically.",
                notes[0].read_text(encoding="utf-8"),
            )


if __name__ == "__main__":
    unittest.main()
