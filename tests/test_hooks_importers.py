from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agent_session_exporter.core import (
    CollectorConfig,
    Config,
    EventStore,
    ServerConfig,
)
from agent_session_exporter.hooks import install_local_hooks
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
        collector=CollectorConfig(),
        server=ServerConfig(),
    )


class HooksImportersTest(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
