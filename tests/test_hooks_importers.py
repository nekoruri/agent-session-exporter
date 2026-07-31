from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from agent_session_exporter.core import (
    ClaudeCloudConfig,
    Config,
    EventStore,
)
from agent_session_exporter.hooks import claude_cloud_hooks, install_local_hooks
from agent_session_exporter.importers import import_export
from agent_session_exporter.renderer import sync_vault


def config_for(root: Path) -> Config:
    return Config(
        vault_path=root / "vault",
        destination="archive",
        state_dir=root / "state",
        device_id="test-device",
        redact=True,
        include_tool_details=False,
        sync_on_capture=True,
        project_aliases={},
        claude_cloud=ClaudeCloudConfig(),
    )


class HooksImportersTest(unittest.TestCase):
    def test_claude_cloud_hooks_use_remote_only_curl_forwarding(self) -> None:
        settings = json.loads(claude_cloud_hooks("https://inbox.example/"))
        hooks = settings["hooks"]
        self.assertEqual(
            set(hooks),
            {
                "UserPromptSubmit",
                "MessageDisplay",
                "Stop",
                "StopFailure",
                "SessionEnd",
            },
        )
        handler = hooks["UserPromptSubmit"][0]["hooks"][0]
        self.assertEqual(handler["type"], "command")
        self.assertEqual(handler["timeout"], 15)
        self.assertNotIn("url", handler)
        self.assertNotIn("headers", handler)
        command = handler["command"]
        self.assertIn(
            'test "${CLAUDE_CODE_REMOTE:-}" = "true" || exit 0',
            command,
        )
        self.assertIn("AGENT_SESSION_EXPORTER_INGEST_TOKEN", command)
        self.assertIn("curl --fail --silent --show-error", command)
        self.assertIn("--data-binary @-", command)
        self.assertIn(
            "https://inbox.example/v1/hooks/claude-cloud",
            command,
        )

        local = subprocess.run(
            command,
            shell=True,
            input="{}",
            capture_output=True,
            text=True,
            env={"CLAUDE_CODE_REMOTE": "false", "PATH": ""},
            check=False,
        )
        self.assertEqual(local.returncode, 0)

        missing_token = subprocess.run(
            command,
            shell=True,
            input="{}",
            capture_output=True,
            text=True,
            env={"CLAUDE_CODE_REMOTE": "true", "PATH": ""},
            check=False,
        )
        self.assertEqual(missing_token.returncode, 1)
        self.assertIn("INGEST_TOKEN is not set", missing_token.stderr)

    def test_claude_cloud_hooks_require_https(self) -> None:
        for value in (
            "http://inbox.example",
            "https://",
            "https://inbox.example/path",
            "https://token@inbox.example",
        ):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(
                    ValueError,
                    "HTTPS origin",
                ),
            ):
                claude_cloud_hooks(value)

    def test_hook_install_is_additive_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "settings.json"
            target.write_text(
                json.dumps({"permissions": {"allow": ["Read"]}}),
                encoding="utf-8",
            )
            _, added_first = install_local_hooks(
                "claude",
                executable="/opt/bin/ase",
                target=target,
            )
            _, added_second = install_local_hooks(
                "claude",
                executable="/opt/bin/ase",
                target=target,
            )
            loaded = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(loaded["permissions"], {"allow": ["Read"]})
            self.assertEqual(added_first, 4)
            self.assertEqual(added_second, 0)

    def test_imports_chatgpt_export_and_renders(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = config_for(root)
            export_path = root / "conversations.json"
            export_path.write_text(
                json.dumps(
                    [
                        {
                            "id": "conversation-1",
                            "title": "Imported example",
                            "create_time": 1767225600,
                            "update_time": 1767225660,
                            "current_node": "a2",
                            "mapping": {
                                "a1": {
                                    "parent": None,
                                    "message": {
                                        "id": "m1",
                                        "author": {"role": "user"},
                                        "create_time": 1767225600,
                                        "content": {"parts": ["Hello"]},
                                    },
                                },
                                "a2": {
                                    "parent": "a1",
                                    "message": {
                                        "id": "m2",
                                        "author": {"role": "assistant"},
                                        "create_time": 1767225660,
                                        "content": {"parts": ["Welcome"]},
                                    },
                                },
                            },
                        }
                    ]
                ),
                encoding="utf-8",
            )
            inserted, examined = import_export(export_path, config)
            self.assertEqual((inserted, examined), (1, 1))
            with EventStore(config.state_dir) as store:
                self.assertEqual(len(store.list_session_keys()), 1)
            self.assertEqual(sync_vault(config), (1, 0))
            note = next((root / "vault").rglob("*.md"))
            markdown = note.read_text(encoding="utf-8")
            self.assertIn("# Imported example", markdown)
            self.assertIn("Hello", markdown)
            self.assertIn("Welcome", markdown)

    def test_rejects_oversized_json_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            export_path = root / "conversations.json"
            export_path.write_text("{}", encoding="utf-8")
            with (
                patch("agent_session_exporter.importers.MAX_JSON_BYTES", 1),
                self.assertRaisesRegex(ValueError, "JSON file is too large"),
            ):
                import_export(export_path, config_for(root))

    def test_rejects_oversized_json_zip_member(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            export_path = root / "export.zip"
            with zipfile.ZipFile(export_path, "w") as archive:
                archive.writestr("conversations.json", "{}")
            with (
                patch("agent_session_exporter.importers.MAX_JSON_BYTES", 1),
                self.assertRaisesRegex(ValueError, "JSON member is too large"),
            ):
                import_export(export_path, config_for(root))


if __name__ == "__main__":
    unittest.main()
