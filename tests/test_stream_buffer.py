from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from agent_session_exporter.claude_cloud import _remote_envelope
from agent_session_exporter.core import EventStore, normalize_event
from agent_session_exporter.stream_buffer import manage_keys
from test_redaction import GITHUB, config_for


class StreamBufferTest(unittest.TestCase):
    def test_cli_hooks_survive_separate_processes_and_keys_are_not_printed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.toml"
            config_path.write_text(f'state_dir = {json.dumps(str(root / "state"))}\nsync_on_capture = false\n')
            command = [sys.executable, "-m", "agent_session_exporter", "--config", str(config_path)]
            for index, chunk in enumerate((GITHUB[:4], GITHUB[4:])):
                payload = {"session_id": "session-1", "hook_event_name": "MessageDisplay",
                           "message_id": "message-1", "index": index, "final": bool(index), "delta": chunk}
                result = subprocess.run(command + ["capture", "--source", "claude-cloud"],
                                        input=json.dumps(payload), text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotIn(chunk, result.stdout + result.stderr)
            result = subprocess.run(command + ["buffer-keys", "--rotate"], text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            keys = json.loads(config_path.with_suffix(".buffer-keys.json").read_text())
            for secret in keys["keys"].values():
                self.assertNotIn(secret, result.stdout + result.stderr)
            with EventStore(root / "state") as store:
                self.assertEqual(store.list_events()[0].payload["delta"], "[REDACTED]")

    def send(self, config, index, text, *, final=False, message="message-1", remote=False):
        payload = {"session_id": "session-1", "hook_event_name": "MessageDisplay",
                   "message_id": message, "index": index, "final": final, "delta": text}
        if remote:
            envelope = _remote_envelope(config, {
                "source": "claude-cloud", "device_id": config.device_id, "session_id": "session-1",
                "event_name": "MessageDisplay", "occurred_at": "2026-09-12T00:00:00Z", "payload": payload,
            })
        else:
            envelope = normalize_event(payload, "claude-cloud", config, inspect_cwd=False)
        with EventStore(config.state_dir) as store:
            return store.add_event(envelope)

    def assert_no_plaintext(self, config, values):
        # Covers free pages, rollback/WAL data as well as live SQL values.
        for path in config.state_dir.iterdir():
            if path.is_file():
                data = path.read_bytes()
                for value in values:
                    self.assertNotIn(value.encode(), data, path.name)

    def test_restart_reorder_empty_final_retries_and_multiline_credentials(self):
        cases = [(GITHUB[:4], GITHUB[4:], ""),
                 ("-----BEGIN PRIVATE KEY-----\n", "MI" + "A" * 128 + "\n", "-----END PRIVATE KEY-----")]
        for parts in cases:
            for remote in (False, True):
                with self.subTest(parts=parts[0], remote=remote), tempfile.TemporaryDirectory() as directory:
                    config = config_for(Path(directory))
                    for index in (2, 1):
                        self.assertEqual(self.send(config, index, parts[index], final=index == 2, remote=remote), (0, False))
                        self.assert_no_plaintext(config, [part for part in parts if part])
                        with EventStore(config.state_dir) as store:
                            self.assertEqual(store.list_events(), [])
                    event_id, inserted = self.send(config, 0, parts[0], remote=remote)
                    self.assertTrue(inserted)
                    with EventStore(config.state_dir) as store:
                        events = store.list_events()
                        self.assertEqual(len(events), 1)
                        self.assertEqual(events[0].payload["delta"], "[REDACTED]")
                        self.assertEqual(store.connection.execute("SELECT count(*) FROM message_chunks").fetchone()[0], 0)
                    for index in (0, 2, 1):
                        self.assertEqual(self.send(config, index, parts[index], final=index == 2, remote=remote), (event_id, False))
                    self.assert_no_plaintext(config, [part for part in parts if part])

    def test_rotation_keeps_pending_keys_and_missing_key_does_not_regenerate(self):
        with tempfile.TemporaryDirectory() as directory:
            config = config_for(Path(directory))
            self.send(config, 0, GITHUB[:4])
            self.assertEqual(config.buffer_key_path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(config.buffer_key_path.parent.stat().st_mode & 0o777, 0o700)
            before = config.buffer_key_path.read_text()
            old_id = json.loads(before)["active"]
            with EventStore(config.state_dir) as store:
                rotated = manage_keys(store, config.buffer_key_path, rotate=True)
                self.assertNotEqual(rotated["active"], old_id)
                with self.assertRaises(ValueError):
                    manage_keys(store, config.buffer_key_path, retire=old_id)
            backup = config.buffer_key_path.read_text()
            config.buffer_key_path.unlink()
            with self.assertRaisesRegex(ValueError, "missing"):
                self.send(config, 1, GITHUB[4:], final=True)
            self.assertFalse(config.buffer_key_path.exists())
            config.buffer_key_path.write_text(backup)
            config.buffer_key_path.chmod(0o600)
            self.send(config, 1, GITHUB[4:], final=True)
            with EventStore(config.state_dir) as store:
                retired = manage_keys(store, config.buffer_key_path, retire=old_id)
                self.assertNotIn(old_id, retired["retained"])
            self.assert_no_plaintext(config, [GITHUB[:4], GITHUB[4:]])

    def test_conflicts_invalid_fields_corruption_and_failed_publish_keep_data_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            config = config_for(Path(directory))
            for index, final in ((True, False), (-1, False), (4096, False), (0, "false")):
                with self.assertRaises(ValueError):
                    self.send(config, index, "fragment", final=final)
            self.send(config, 0, GITHUB[:4])
            with self.assertRaisesRegex(ValueError, "Conflicting"):
                self.send(config, 0, "different")
            with patch.object(EventStore, "_insert_event", side_effect=RuntimeError("injected failure")):
                with self.assertRaises(RuntimeError):
                    self.send(config, 1, GITHUB[4:], final=True)
            self.assert_no_plaintext(config, [GITHUB[:4], GITHUB[4:]])
            with EventStore(config.state_dir) as store:
                self.assertEqual(store.list_events(), [])
                original = store.connection.execute("SELECT ciphertext FROM message_chunks").fetchone()[0]
                store.connection.execute("UPDATE message_chunks SET ciphertext = ?", (b"broken",))
                store.connection.commit()
            with self.assertRaisesRegex(ValueError, "decrypt"):
                self.send(config, 1, GITHUB[4:], final=True)
            with EventStore(config.state_dir) as store:
                store.connection.execute("UPDATE message_chunks SET ciphertext = ?", (original,))
                store.connection.commit()
            self.assertTrue(self.send(config, 1, GITHUB[4:], final=True)[1])

    def test_plain_conversation_is_preserved_and_message_ids_do_not_mix(self):
        with tempfile.TemporaryDirectory() as directory:
            config = config_for(Path(directory))
            self.send(config, 0, "こんにちは、", message="first")
            self.send(config, 0, "別の会話", message="second", final=True)
            self.send(config, 1, "世界🙂", message="first", final=True)
            with EventStore(config.state_dir) as store:
                texts = {event.payload["message_id"]: event.payload["delta"] for event in store.list_events()}
                self.assertEqual(texts, {"first": "こんにちは、世界🙂", "second": "別の会話"})
            with self.assertRaisesRegex(ValueError, "outside"):
                self.send(replace(config, buffer_key_path=config.state_dir / "key"), 0, "x")

    def test_message_identifiers_are_pseudonymized_without_collapsing_messages(self):
        with tempfile.TemporaryDirectory() as directory:
            config = config_for(Path(directory))
            for letter in ("a", "b"):
                self.send(config, 0, "Hello " + letter, final=True, message="ghp_" + letter * 36)
            with EventStore(config.state_dir) as store:
                ids = {event.payload["message_id"] for event in store.list_events()}
                self.assertEqual(len(ids), 2)
                self.assertTrue(all(value.startswith("redacted-") for value in ids))

    def test_buffer_limits_and_key_file_permissions_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            config = config_for(Path(directory))
            self.send(config, 0, GITHUB[:4])
            for setting in ("MAX_MESSAGE_BYTES", "MAX_BUFFER_BYTES"):
                with patch("agent_session_exporter.stream_buffer." + setting, 1):
                    with self.assertRaisesRegex(ValueError, "full"):
                        self.send(config, 1, GITHUB[4:], final=True)
            config.buffer_key_path.chmod(0o644)
            with self.assertRaisesRegex(ValueError, "600"):
                self.send(config, 1, GITHUB[4:], final=True)
            config.buffer_key_path.chmod(0o600)
            self.send(config, 2, "end", final=True)
            with self.assertRaisesRegex(ValueError, "Conflicting"):
                self.send(config, 1, "unexpected end", final=True)
            self.send(config, 1, GITHUB[4:])
            self.assert_no_plaintext(config, [GITHUB[4:]])
            git_tree = Path(directory) / "repository"
            (git_tree / ".git").mkdir(parents=True)
            (git_tree / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
            with self.assertRaisesRegex(ValueError, "Git"):
                self.send(replace(config, buffer_key_path=git_tree / "keys" / "key.json"),
                          0, "text", message="new-message")
