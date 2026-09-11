from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path, PurePosixPath
from unittest.mock import patch

from agent_session_exporter import renderer as renderer_module
from agent_session_exporter.core import (
    ClaudeCloudConfig,
    Config,
    EventStore,
    normalize_event,
)
from agent_session_exporter.renderer import (
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
        claude_cloud=ClaudeCloudConfig(),
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

    def test_replaces_append_only_generated_note_without_backup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old_config, old_path, key = create_old_note(root)
            config = replace(old_config, destination="ai-sessions")
            old_target = config.vault_path / Path(*old_path.parts)
            current = old_target.read_text(encoding="utf-8")
            new_path = PurePosixPath("ai-sessions", *old_path.parts[-3:])
            new_target = config.vault_path / Path(*new_path.parts)
            new_target.parent.mkdir(parents=True)
            stale = current.replace(
                'title: "Move this note."',
                'title: "Old title"',
                1,
            ).replace(
                "# Move this note.\n",
                "# Old title\n",
                1,
            ).replace("\nMove this note.\n", "\n", 1)
            new_target.write_text(stale, encoding="utf-8")

            planned, errors = migrate_render_state(config)
            self.assertEqual(errors, [])
            self.assertEqual(
                [item.operation for item in planned],
                ["replace-append-only"],
            )
            self.assertIsNone(planned[0].backup_path)

            migrations, errors = migrate_render_state(config, apply=True)
            self.assertEqual(errors, [])
            self.assertEqual(migrations, planned)
            self.assertFalse(old_target.exists())
            self.assertEqual(new_target.read_text(encoding="utf-8"), current)
            self.assertEqual(list(new_target.parent.glob("*.stale-*.md")), [])
            with EventStore(config.state_dir) as store:
                state = store.get_render_state(key)
            self.assertEqual(str(state["note_path"]), new_path.as_posix())
            self.assertEqual(sync_vault(config), (0, 1))

    def test_refuses_append_only_replacement_after_destination_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old_config, old_path, key = create_old_note(root)
            config = replace(old_config, destination="ai-sessions")
            old_target = config.vault_path / Path(*old_path.parts)
            current = old_target.read_text(encoding="utf-8")
            new_path = PurePosixPath("ai-sessions", *old_path.parts[-3:])
            new_target = config.vault_path / Path(*new_path.parts)
            new_target.parent.mkdir(parents=True)
            stale = current.replace("\nMove this note.\n", "\n", 1)
            new_target.write_text(stale, encoding="utf-8")
            concurrent = f"{stale}\nConcurrent edit.\n"
            real_verify = renderer_module._verified_generated_hash
            changed = False

            def verify_after_change(path: Path, expected_hash: str) -> str:
                nonlocal changed
                if Path(path) == new_target and not changed:
                    new_target.write_text(concurrent, encoding="utf-8")
                    changed = True
                return real_verify(path, expected_hash)

            with patch(
                "agent_session_exporter.renderer._verified_generated_hash",
                side_effect=verify_after_change,
            ):
                migrations, errors = migrate_render_state(config, apply=True)

            self.assertEqual(migrations, [])
            self.assertEqual(len(errors), 1)
            self.assertIn("destination changed during migration", errors[0])
            self.assertTrue(old_target.exists())
            self.assertEqual(new_target.read_text(encoding="utf-8"), concurrent)
            with EventStore(config.state_dir) as store:
                state = store.get_render_state(key)
            self.assertEqual(str(state["note_path"]), old_path.as_posix())

    def test_replaces_append_only_note_with_multiline_title(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old_config, old_path, key = create_old_note(root)
            config = replace(old_config, destination="ai-sessions")
            old_target = config.vault_path / Path(*old_path.parts)
            current = old_target.read_text(encoding="utf-8")
            title = "First\r\nSecond\u0085Third\u2028Fourth\u2029Fifth"
            current = current.replace(
                'title: "Move this note."',
                f"title: {json.dumps(title, ensure_ascii=False)}",
                1,
            ).replace(
                "# Move this note.\n",
                f"# {title}\n",
                1,
            )
            old_target.write_text(current, encoding="utf-8")
            current_hash = hashlib.sha256(current.encode("utf-8")).hexdigest()
            with EventStore(config.state_dir) as store:
                store.set_render_state(key, current_hash, old_path.as_posix())

            new_path = PurePosixPath("ai-sessions", *old_path.parts[-3:])
            new_target = config.vault_path / Path(*new_path.parts)
            new_target.parent.mkdir(parents=True)
            stale = current.replace("\nMove this note.\n", "\n", 1)
            new_target.write_text(stale, encoding="utf-8")

            migrations, errors = migrate_render_state(config, apply=True)
            self.assertEqual(errors, [])
            self.assertEqual(
                [item.operation for item in migrations],
                ["replace-append-only"],
            )
            self.assertFalse(old_target.exists())
            self.assertEqual(new_target.read_bytes().decode("utf-8"), current)

    def test_backs_up_modified_generated_note_before_replacing_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old_config, old_path, key = create_old_note(root)
            config = replace(old_config, destination="ai-sessions")
            old_target = config.vault_path / Path(*old_path.parts)
            current = old_target.read_text(encoding="utf-8")
            new_path = PurePosixPath("ai-sessions", *old_path.parts[-3:])
            new_target = config.vault_path / Path(*new_path.parts)
            new_target.parent.mkdir(parents=True)
            stale = current.replace(
                "\nMove this note.\n",
                "\nEdited locally.\n",
                1,
            )
            new_target.write_text(stale, encoding="utf-8")

            planned, errors = migrate_render_state(config)
            self.assertEqual(errors, [])
            self.assertEqual([item.operation for item in planned], ["replace-stale"])
            self.assertIsNotNone(planned[0].backup_path)
            backup_path = planned[0].backup_path
            if backup_path is None:
                raise AssertionError("fixture has no backup path")
            backup_target = config.vault_path / Path(*backup_path.parts)
            self.assertFalse(backup_target.exists())

            migrations, errors = migrate_render_state(config, apply=True)
            self.assertEqual(errors, [])
            self.assertEqual(migrations, planned)
            self.assertFalse(old_target.exists())
            self.assertEqual(new_target.read_text(encoding="utf-8"), current)
            self.assertEqual(backup_target.read_text(encoding="utf-8"), stale)
            with EventStore(config.state_dir) as store:
                state = store.get_render_state(key)
            self.assertEqual(str(state["note_path"]), new_path.as_posix())
            self.assertEqual(sync_vault(config), (0, 1))

    def test_refuses_stale_generated_note_for_a_different_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old_config, old_path, key = create_old_note(root)
            config = replace(old_config, destination="ai-sessions")
            old_target = config.vault_path / Path(*old_path.parts)
            current = old_target.read_text(encoding="utf-8")
            new_path = PurePosixPath("ai-sessions", *old_path.parts[-3:])
            new_target = config.vault_path / Path(*new_path.parts)
            new_target.parent.mkdir(parents=True)
            different_session = current.replace(
                'session_id: "migration-1"',
                'session_id: "other-session"',
                1,
            )
            new_target.write_text(different_session, encoding="utf-8")

            migrations, errors = migrate_render_state(config, apply=True)
            self.assertEqual(migrations, [])
            self.assertEqual(len(errors), 1)
            self.assertIn("different session", errors[0])
            self.assertEqual(
                new_target.read_text(encoding="utf-8"),
                different_session,
            )
            with EventStore(config.state_dir) as store:
                state = store.get_render_state(key)
            self.assertEqual(str(state["note_path"]), old_path.as_posix())
            self.assertTrue(old_target.exists())

    def test_restores_stale_target_when_replacement_move_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old_config, old_path, _ = create_old_note(root)
            config = replace(old_config, destination="ai-sessions")
            old_target = config.vault_path / Path(*old_path.parts)
            current = old_target.read_text(encoding="utf-8")
            new_path = PurePosixPath("ai-sessions", *old_path.parts[-3:])
            new_target = config.vault_path / Path(*new_path.parts)
            new_target.parent.mkdir(parents=True)
            stale = current.replace(
                "\nMove this note.\n",
                "\nEdited locally.\n",
                1,
            )
            new_target.write_text(stale, encoding="utf-8")
            real_replace = os.replace

            def replace_with_failure(source: Path, target: Path) -> None:
                if Path(source) == old_target and Path(target) == new_target:
                    raise OSError("simulated move failure")
                real_replace(source, target)

            with (
                patch(
                    "agent_session_exporter.renderer.os.replace",
                    side_effect=replace_with_failure,
                ),
                self.assertRaisesRegex(OSError, "simulated move failure"),
            ):
                migrate_render_state(config, apply=True)

            self.assertTrue(old_target.exists())
            self.assertEqual(new_target.read_text(encoding="utf-8"), stale)

    def test_does_not_restore_stale_target_after_completed_move(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old_config, old_path, _ = create_old_note(root)
            config = replace(old_config, destination="ai-sessions")
            old_target = config.vault_path / Path(*old_path.parts)
            current = old_target.read_text(encoding="utf-8")
            new_path = PurePosixPath("ai-sessions", *old_path.parts[-3:])
            new_target = config.vault_path / Path(*new_path.parts)
            new_target.parent.mkdir(parents=True)
            stale = current.replace(
                "\nMove this note.\n",
                "\nEdited locally.\n",
                1,
            )
            new_target.write_text(stale, encoding="utf-8")
            planned, errors = migrate_render_state(config)
            self.assertEqual(errors, [])
            self.assertEqual([item.operation for item in planned], ["replace-stale"])
            backup_path = planned[0].backup_path
            if backup_path is None:
                raise AssertionError("fixture has no backup path")
            backup_target = config.vault_path / Path(*backup_path.parts)
            real_replace = os.replace

            def replace_then_interrupt(source: Path, target: Path) -> None:
                real_replace(source, target)
                if Path(source) == old_target and Path(target) == new_target:
                    raise KeyboardInterrupt

            with (
                patch(
                    "agent_session_exporter.renderer.os.replace",
                    side_effect=replace_then_interrupt,
                ),
                self.assertRaises(KeyboardInterrupt),
            ):
                migrate_render_state(config, apply=True)

            self.assertFalse(old_target.exists())
            self.assertEqual(new_target.read_text(encoding="utf-8"), current)
            self.assertEqual(backup_target.read_text(encoding="utf-8"), stale)
            migrations, errors = migrate_render_state(config, apply=True)
            self.assertEqual(errors, [])
            self.assertEqual([item.operation for item in migrations], ["rebind"])

    def test_refuses_non_generated_destination(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old_config, old_path, key = create_old_note(root)
            config = replace(old_config, destination="ai-sessions")
            old_target = config.vault_path / Path(*old_path.parts)
            new_path = PurePosixPath("ai-sessions", *old_path.parts[-3:])
            new_target = config.vault_path / Path(*new_path.parts)
            new_target.parent.mkdir(parents=True)
            manual = "# Manual note\n"
            new_target.write_text(manual, encoding="utf-8")

            migrations, errors = migrate_render_state(config, apply=True)
            self.assertEqual(migrations, [])
            self.assertEqual(len(errors), 1)
            self.assertIn("refusing non-generated note", errors[0])
            self.assertEqual(new_target.read_text(encoding="utf-8"), manual)
            self.assertTrue(old_target.exists())
            with EventStore(config.state_dir) as store:
                state = store.get_render_state(key)
            self.assertEqual(str(state["note_path"]), old_path.as_posix())

    def test_refuses_conflicting_stale_backup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old_config, old_path, _ = create_old_note(root)
            config = replace(old_config, destination="ai-sessions")
            old_target = config.vault_path / Path(*old_path.parts)
            current = old_target.read_text(encoding="utf-8")
            new_path = PurePosixPath("ai-sessions", *old_path.parts[-3:])
            new_target = config.vault_path / Path(*new_path.parts)
            new_target.parent.mkdir(parents=True)
            stale = current.replace(
                "\nMove this note.\n",
                "\nEdited locally.\n",
                1,
            )
            new_target.write_text(stale, encoding="utf-8")
            planned, errors = migrate_render_state(config)
            self.assertEqual(errors, [])
            self.assertEqual([item.operation for item in planned], ["replace-stale"])
            backup_path = planned[0].backup_path
            if backup_path is None:
                raise AssertionError("fixture has no backup path")
            backup_target = config.vault_path / Path(*backup_path.parts)
            conflicting_backup = current.replace(
                "Move this note.",
                "Different backup",
                1,
            )
            backup_target.write_text(conflicting_backup, encoding="utf-8")

            migrations, errors = migrate_render_state(config, apply=True)
            self.assertEqual(migrations, [])
            self.assertEqual(len(errors), 1)
            self.assertIn("stale backup conflicts", errors[0])
            self.assertEqual(new_target.read_text(encoding="utf-8"), stale)
            self.assertEqual(
                backup_target.read_text(encoding="utf-8"),
                conflicting_backup,
            )
            self.assertTrue(old_target.exists())

    def test_refuses_directory_at_stale_backup_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old_config, old_path, _ = create_old_note(root)
            config = replace(old_config, destination="ai-sessions")
            old_target = config.vault_path / Path(*old_path.parts)
            current = old_target.read_text(encoding="utf-8")
            new_path = PurePosixPath("ai-sessions", *old_path.parts[-3:])
            new_target = config.vault_path / Path(*new_path.parts)
            new_target.parent.mkdir(parents=True)
            stale = current.replace(
                "\nMove this note.\n",
                "\nEdited locally.\n",
                1,
            )
            new_target.write_text(stale, encoding="utf-8")
            planned, errors = migrate_render_state(config)
            self.assertEqual(errors, [])
            self.assertEqual([item.operation for item in planned], ["replace-stale"])
            backup_path = planned[0].backup_path
            if backup_path is None:
                raise AssertionError("fixture has no backup path")
            backup_target = config.vault_path / Path(*backup_path.parts)
            backup_target.mkdir()

            migrations, errors = migrate_render_state(config, apply=True)
            self.assertEqual(migrations, [])
            self.assertEqual(len(errors), 1)
            self.assertIn("stale backup conflicts", errors[0])
            self.assertIn("not a file", errors[0])
            self.assertTrue(old_target.exists())
            self.assertEqual(new_target.read_text(encoding="utf-8"), stale)

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
