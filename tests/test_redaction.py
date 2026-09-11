from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

from agent_session_exporter.adapters import Message, SessionDocument, build_session_document
from agent_session_exporter.claude_cloud import _remote_envelope
from agent_session_exporter.cli import main
from agent_session_exporter.codex_cloud import exec_codex_cloud, sync_codex_cloud
from agent_session_exporter.core import ClaudeCloudConfig, Config, EventStore, normalize_event
from agent_session_exporter.importers import import_export
from agent_session_exporter.redaction import REDACTED, canonical_identity, redact_text, redact_value
from agent_session_exporter.renderer import _new_note_path, render_markdown, sync_session, sync_vault

# Synthetic values with provider-valid shapes, never real credentials.
GITHUB = "ghp_" + "a" * 36
OPENAI = "sk-" + "a" * 20 + "T3BlbkFJ" + "b" * 20


def config_for(root: Path, *, redact: bool = True) -> Config:
    return Config(
        vault_path=root / "vault", destination="archive", state_dir=root / "state",
        device_id="test-device", redact=redact, include_tool_details=False,
        sync_on_capture=True, project_aliases={}, claude_cloud=ClaudeCloudConfig(),
        buffer_key_path=root / "keys" / "buffer.json",
    )


def note_content(config: Config) -> str:
    return "\n".join(path.read_text() for path in config.vault_path.rglob("*.md"))


def write_pre_fix_note(config: Config, event) -> Path:
    """Reproduce a render state created before raw/pseudonymized keys were grouped."""
    document = build_session_document([event])
    if config.redact:
        values = redact_value(asdict(document))
        values["messages"] = [Message(**message) for message in values["messages"]]
        document = SessionDocument(**values)
    path = _new_note_path(document, config.destination, config.path_timezone)
    content = render_markdown(document, rendered_at="2026-09-12T00:01:00Z")
    target = config.vault_path / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    with EventStore(config.state_dir) as store:
        store.set_render_state(
            store.session_key(event.source, event.device_id, event.session_id),
            hashlib.sha256(content.encode()).hexdigest(), path.as_posix(),
            source_hash=hashlib.sha256(render_markdown(document).encode()).hexdigest(),
        )
    return target


class RedactionTest(unittest.TestCase):
    def test_legacy_and_pseudonymized_sessions_reuse_one_complete_note(self) -> None:
        opaque = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
        for identity in ("session", "device", "both"):
            for prior_state in ("unrendered", "legacy-note", "split", "overwritten"):
                for immediate in (False, True):
                    with self.subTest(identity=identity, prior_state=prior_state, immediate=immediate), \
                            tempfile.TemporaryDirectory() as directory:
                        config = config_for(Path(directory))
                        if identity in ("device", "both"):
                            config = replace(config, device_id=opaque)
                        session = opaque if identity in ("session", "both") else "session-1"
                        legacy_config = replace(config, redact=False)
                        legacy = normalize_event({
                            "session_id": session, "hook_event_name": "UserPromptSubmit",
                            "timestamp": "2026-09-12T00:00:01Z", "prompt": "legacy-only-message",
                        }, "claude-code", legacy_config, inspect_cwd=False)
                        with EventStore(config.state_dir) as store:
                            store.add_event(legacy)
                            old_event = store.list_events()[0]
                        original_path = None
                        if prior_state != "unrendered":
                            original_path = write_pre_fix_note(
                                config if prior_state == "overwritten" else legacy_config, old_event,
                            )
                        current = normalize_event({
                            "session_id": session, "hook_event_name": "UserPromptSubmit",
                            "timestamp": "2026-09-12T00:00:02Z", "prompt": "new-only-message",
                        }, "claude-code", config, inspect_cwd=False)
                        with EventStore(config.state_dir) as store:
                            store.add_event(current)
                            before = [asdict(event) for event in store.list_events()]
                            new_event = store.list_events()[-1]
                            self.assertEqual(len(store.list_session_keys()), 1)
                            for envelope in (legacy, current):
                                events = store.session_events(
                                    envelope["source"], envelope["device_id"], envelope["session_id"],
                                )
                                self.assertEqual([asdict(event) for event in events], before)
                        if prior_state in ("split", "overwritten"):
                            write_pre_fix_note(config, new_event)
                        if immediate:
                            self.assertTrue(sync_session(
                                config, current["source"], current["device_id"], current["session_id"],
                            ))
                        else:
                            self.assertEqual(sync_vault(config), (1, 0))
                        paths = list(config.vault_path.rglob("*.md"))
                        self.assertEqual(len(paths), 1)
                        if original_path is not None:
                            self.assertEqual(paths, [original_path])
                        text = paths[0].read_text()
                        self.assertIn("legacy-only-message", text)
                        self.assertIn("new-only-message", text)
                        self.assertNotIn(opaque, text)
                        self.assertEqual(sync_vault(config), (0, 1))
                        with EventStore(config.state_dir) as store:
                            self.assertEqual([asdict(event) for event in store.list_events()], before)
                            self.assertEqual(len(store.list_render_states()), 1)
                        # Toggling output redaction must not split the session again.
                        sync_vault(legacy_config)
                        self.assertEqual(list(config.vault_path.rglob("*.md")), paths)

    def test_alias_lookup_keeps_sources_devices_and_sessions_distinct(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = config_for(Path(directory), redact=False)
            device, session = "legacy-device", "legacy-session"
            with EventStore(config.state_dir) as store:
                for index, (source, current_device, current_session) in enumerate((
                    ("claude-code", device, session),
                    ("claude-code", canonical_identity(device), session),
                    ("claude-code", device, canonical_identity(session)),
                    ("claude-code", canonical_identity(device), canonical_identity(session)),
                    ("claude-code", "other-device", session),
                    ("claude-code", device, "other-session"),
                    ("codex-cli", device, session),
                )):
                    store.add_event(normalize_event({
                        "session_id": current_session, "prompt": str(index),
                    }, source, config, device_id=current_device, inspect_cwd=False))
                    # Exercise cache invalidation as new aliases are added.
                    self.assertEqual(len(store.list_session_keys()), max(1, index - 2))
                with patch("agent_session_exporter.redaction._detectors", side_effect=AssertionError("scan")):
                    self.assertEqual(len(store.list_session_keys()), 4)
                    self.assertEqual([event.payload["prompt"] for event in store.session_events(
                        "claude-code", canonical_identity(device), canonical_identity(session),
                    )], ["0", "1", "2", "3"])

    def test_split_note_repair_preserves_user_edits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = config_for(Path(directory))
            opaque = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
            for enabled in (False, True):
                envelope = normalize_event({
                    "session_id": opaque, "hook_event_name": "UserPromptSubmit",
                    "timestamp": "2026-09-12T00:00:00Z", "prompt": f"message-{enabled}",
                }, "claude-code", replace(config, redact=enabled), inspect_cwd=False)
                with EventStore(config.state_dir) as store:
                    store.add_event(envelope)
                    event = store.list_events()[-1]
                path = write_pre_fix_note(replace(config, redact=enabled), event)
            path.write_text(path.read_text() + "\nUser annotation\n")
            before = {note: note.read_bytes() for note in config.vault_path.rglob("*.md")}
            with self.assertRaisesRegex(ValueError, "content differs from render state"):
                sync_vault(config)
            self.assertEqual({note: note.read_bytes() for note in config.vault_path.rglob("*.md")}, before)
            with EventStore(config.state_dir) as store:
                self.assertEqual(len(store.list_render_states()), 2)

    def test_split_note_repair_preserves_other_owners_and_recovers_from_failures(self) -> None:
        for failure in ("shared-owner", "write", "unlink", "state"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                config = config_for(Path(directory))
                opaque = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
                for enabled in (False, True):
                    envelope = normalize_event({
                        "session_id": opaque, "hook_event_name": "UserPromptSubmit",
                        "timestamp": "2026-09-12T00:00:00Z", "prompt": f"message-{enabled}",
                    }, "claude-code", replace(config, redact=enabled), inspect_cwd=False)
                    with EventStore(config.state_dir) as store:
                        store.add_event(envelope)
                        event = store.list_events()[-1]
                    path = write_pre_fix_note(replace(config, redact=enabled), event)
                if failure == "shared-owner":
                    with EventStore(config.state_dir) as store:
                        store.set_render_state(
                            store.session_key("claude-code", "other-device", "other-session"),
                            hashlib.sha256(path.read_bytes()).hexdigest(),
                            path.relative_to(config.vault_path).as_posix(),
                        )
                before = {note: note.read_bytes() for note in config.vault_path.rglob("*.md")}
                if failure == "shared-owner":
                    with self.assertRaisesRegex(ValueError, "shared with a different session"):
                        sync_vault(config)
                else:
                    target = {
                        "write": "agent_session_exporter.renderer._atomic_write",
                        "unlink": "pathlib.Path.unlink",
                        "state": "agent_session_exporter.core.EventStore.set_render_state",
                    }[failure]
                    with patch(target, side_effect=OSError("interrupted")):
                        with self.assertRaisesRegex(OSError, "interrupted"):
                            sync_vault(config)
                if failure in ("shared-owner", "write"):
                    self.assertEqual({note: note.read_bytes() for note in config.vault_path.rglob("*.md")}, before)
                with EventStore(config.state_dir) as store:
                    self.assertEqual(len(store.list_render_states()), 3 if failure == "shared-owner" else 2)
                if failure != "shared-owner":
                    self.assertEqual(sync_vault(config), (1, 0))
                    self.assertEqual(len(list(config.vault_path.rglob("*.md"))), 1)
                    self.assertIn("message-False", note_content(config))
                    self.assertIn("message-True", note_content(config))

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
