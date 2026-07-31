from __future__ import annotations

import hashlib
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path, PurePosixPath

from agent_session_exporter.core import (
    CollectorConfig,
    Config,
    EventStore,
    ServerConfig,
    normalize_event,
)
from agent_session_exporter.renderer import (
    GENERATED_MARKER,
    migrate_render_state,
    render_state_path_issues,
    sync_vault,
)


def config_for(root: Path, destination: str) -> Config:
    return Config(
        vault_path=root / "vault",
        destination=destination,
        state_dir=root / "state",
        device_id="test-device",
        redact=True,
        include_tool_details=False,
        sync_on_capture=True,
        project_aliases={},
        collector=CollectorConfig(),
        server=ServerConfig(),
    )


def create_old_note(root: Path) -> tuple[Config, PurePosixPath, str]:
    old_config = config_for(root, "inbox/ai-sessions")
    envelope = normalize_event(
        {
            "session_id": "migration-1",
            "hook_event_name": "UserPromptSubmit",
            "timestamp": "2026-07-26T01:02:03+00:00",
            "project": "demo",
            "prompt": "Move this note.",
        },
        "codex-cli",
        old_config,
    )
    with EventStore(old_config.state_dir) as store:
        store.add_event(envelope)
    if sync_vault(old_config) != (1, 0):
        raise AssertionError("fixture did not render")
    with EventStore(old_config.state_dir) as store:
        key = store.session_key("codex-cli", "test-device", "migration-1")
        state = store.get_render_state(key)
        if state is None:
            raise AssertionError("fixture has no render state")
        return old_config, PurePosixPath(str(state["note_path"])), key


class RenderStateMigrationTest(unittest.TestCase):
    def test_dry_run_then_apply_moves_generated_note(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old_config, old_path, key = create_old_note(root)
            config = replace(old_config, destination="ai-sessions")
            old_target = config.vault_path / Path(*old_path.parts)

            issues = render_state_path_issues(config)
            self.assertEqual(len(issues), 1)
            self.assertIn("outside destination", issues[0])

            planned, errors = migrate_render_state(config)
            self.assertEqual(errors, [])
            self.assertEqual([item.operation for item in planned], ["move"])
            new_path = planned[0].new_path
            new_target = config.vault_path / Path(*new_path.parts)
            self.assertTrue(old_target.exists())
            self.assertFalse(new_target.exists())

            applied, errors = migrate_render_state(config, apply=True)
            self.assertEqual(errors, [])
            self.assertEqual(applied, planned)
            self.assertFalse(old_target.exists())
            self.assertTrue(new_target.exists())
            with EventStore(config.state_dir) as store:
                state = store.get_render_state(key)
            self.assertIsNotNone(state)
            self.assertEqual(str(state["note_path"]), new_path.as_posix())
            self.assertEqual(render_state_path_issues(config), [])

    def test_rebinds_a_note_already_moved_to_destination(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old_config, old_path, key = create_old_note(root)
            config = replace(old_config, destination="ai-sessions")
            old_target = config.vault_path / Path(*old_path.parts)
            new_path = PurePosixPath("ai-sessions", *old_path.parts[-3:])
            new_target = config.vault_path / Path(*new_path.parts)
            new_target.parent.mkdir(parents=True)
            old_target.replace(new_target)

            migrations, errors = migrate_render_state(config, apply=True)
            self.assertEqual(errors, [])
            self.assertEqual([item.operation for item in migrations], ["rebind"])
            with EventStore(config.state_dir) as store:
                state = store.get_render_state(key)
            self.assertEqual(str(state["note_path"]), new_path.as_posix())

    def test_refuses_to_rebind_different_generated_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old_config, old_path, key = create_old_note(root)
            config = replace(old_config, destination="ai-sessions")
            new_path = PurePosixPath("ai-sessions", *old_path.parts[-3:])
            new_target = config.vault_path / Path(*new_path.parts)
            new_target.parent.mkdir(parents=True)
            conflicting = f"{GENERATED_MARKER}\n\n# Different\n"
            new_target.write_text(conflicting, encoding="utf-8")

            migrations, errors = migrate_render_state(config, apply=True)
            self.assertEqual(migrations, [])
            self.assertEqual(len(errors), 1)
            self.assertIn("content differs", errors[0])
            self.assertEqual(new_target.read_text(encoding="utf-8"), conflicting)
            with EventStore(config.state_dir) as store:
                state = store.get_render_state(key)
                old_target = config.vault_path / Path(*old_path.parts)
            self.assertEqual(str(state["note_path"]), old_path.as_posix())
            self.assertTrue(old_target.exists())

    def test_refuses_non_generated_note_even_when_hash_matches(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = config_for(root, "ai-sessions")
            note_path = PurePosixPath("legacy", "2026", "07", "manual.md")
            target = config.vault_path / Path(*note_path.parts)
            target.parent.mkdir(parents=True)
            content = "# Manual note\n"
            target.write_text(content, encoding="utf-8")
            with EventStore(config.state_dir) as store:
                store.set_render_state(
                    "manual-session",
                    hashlib.sha256(content.encode("utf-8")).hexdigest(),
                    note_path.as_posix(),
                )

            migrations, errors = migrate_render_state(config, apply=True)
            self.assertEqual(migrations, [])
            self.assertEqual(len(errors), 1)
            self.assertIn("refusing non-generated note", errors[0])
            self.assertTrue(target.exists())


if __name__ == "__main__":
    unittest.main()
