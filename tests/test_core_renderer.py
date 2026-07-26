from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_session_exporter.adapters import (
    build_session_document,
    parse_claude_transcript,
    parse_codex_transcript,
)
from agent_session_exporter.cli import run
from agent_session_exporter.core import (
    CollectorConfig,
    Config,
    EventStore,
    ServerConfig,
    normalize_event,
    render_initial_config,
)
from agent_session_exporter.renderer import sync_vault


def config_for(root: Path) -> Config:
    return Config(
        vault_path=root / "vault",
        destination="inbox/ai-sessions",
        state_dir=root / "state",
        device_id="test-device",
        redact=True,
        include_tool_details=False,
        sync_on_capture=True,
        project_aliases={},
        collector=CollectorConfig(),
        server=ServerConfig(),
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

            self.assertEqual(sync_vault(config), (1, 0))
            self.assertEqual(sync_vault(config), (0, 1))
            notes = list((root / "vault").rglob("*.md"))
            self.assertEqual(len(notes), 1)
            markdown = notes[0].read_text(encoding="utf-8")
            self.assertIn("# Explain the failing test.", markdown)
            self.assertIn("## Assistant", markdown)
            self.assertIn("The fixture is missing.", markdown)

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
