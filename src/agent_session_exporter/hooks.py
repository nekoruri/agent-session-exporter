"""Generate hook configuration snippets for supported agent surfaces."""

from __future__ import annotations

import json
import os
import shlex
import tempfile
import urllib.parse
from collections.abc import Mapping
from pathlib import Path
from typing import Any

CODEX_EVENTS = ["UserPromptSubmit", "Stop", "SessionEnd"]
CLAUDE_EVENTS = ["UserPromptSubmit", "Stop", "StopFailure", "SessionEnd"]


def _command(source: str, executable: str) -> str:
    return f"{shlex.quote(executable)} capture --source {shlex.quote(source)}"


def codex_hooks(executable: str = "ase") -> str:
    """Return a Codex hooks JSON fragment."""
    command = _command("codex-cli", executable)
    hooks = {
        event: [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": command,
                        "timeout": 3,
                    }
                ]
            }
        ]
        for event in CODEX_EVENTS
    }
    return json.dumps(
        {
            "description": "Archive Codex sessions for Obsidian.",
            "hooks": hooks,
        },
        ensure_ascii=False,
        indent=2,
    )


def claude_hooks(executable: str = "ase") -> str:
    """Return a Claude Code command hooks JSON fragment."""
    command = _command("claude-code", executable)
    hooks = {
        event: [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": command,
                    }
                ]
            }
        ]
        for event in CLAUDE_EVENTS
    }
    return json.dumps({"hooks": hooks}, ensure_ascii=False, indent=2)


def claude_cloud_hooks(inbox_url: str) -> str:
    """Return Claude Cloud command-hook settings for the Worker inbox."""
    base = inbox_url.rstrip("/")
    parsed = urllib.parse.urlsplit(base)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Claude Cloud inbox URL must be an HTTPS origin.")
    endpoint = shlex.quote(f"{base}/v1/hooks/claude-cloud")
    command = (
        'test "${CLAUDE_CODE_REMOTE:-}" = "true" || exit 0; '
        'test -n "${AGENT_SESSION_EXPORTER_INGEST_TOKEN:-}" || { '
        'echo "AGENT_SESSION_EXPORTER_INGEST_TOKEN is not set" >&2; exit 1; }; '
        "exec curl --fail --silent --show-error --output /dev/null "
        "--max-time 10 --header 'Content-Type: application/json' "
        '--header "Authorization: Bearer '
        '${AGENT_SESSION_EXPORTER_INGEST_TOKEN}" '
        "--header 'X-Claude-Code-Remote: true' --data-binary @- "
        f"{endpoint}"
    )
    hook = {
        "type": "command",
        "command": command,
        "timeout": 15,
    }
    events = [
        "UserPromptSubmit",
        "MessageDisplay",
        "Stop",
        "StopFailure",
        "SessionEnd",
    ]
    return json.dumps(
        {"hooks": {event: [{"hooks": [dict(hook)]}] for event in events}},
        ensure_ascii=False,
        indent=2,
    )


def _settings(source: str, executable: str) -> dict[str, Any]:
    raw = codex_hooks(executable) if source == "codex" else claude_hooks(executable)
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise TypeError("Internal hook generator returned invalid settings.")
    return value


def _handler_commands(group: object) -> set[str]:
    if not isinstance(group, Mapping):
        return set()
    handlers = group.get("hooks")
    if not isinstance(handlers, list):
        return set()
    return {
        str(handler.get("command"))
        for handler in handlers
        if isinstance(handler, Mapping) and handler.get("command")
    }


def install_local_hooks(
    source: str,
    *,
    executable: str = "ase",
    target: Path | None = None,
) -> tuple[Path, int]:
    """Merge exporter hooks without replacing unrelated user settings."""
    if source not in {"codex", "claude"}:
        raise ValueError("source must be codex or claude.")
    if target is None:
        target = (
            Path.home() / ".codex" / "hooks.json"
            if source == "codex"
            else Path.home() / ".claude" / "settings.json"
        )
    target = target.expanduser()
    existing: dict[str, Any] = {}
    if target.exists():
        with target.open(encoding="utf-8") as stream:
            loaded = json.load(stream)
        if not isinstance(loaded, dict):
            raise ValueError(f"Expected a JSON object in {target}.")
        existing = loaded

    generated = _settings(source, executable)
    existing_hooks = existing.setdefault("hooks", {})
    if not isinstance(existing_hooks, dict):
        raise TypeError(f"`hooks` must be a JSON object in {target}.")
    generated_hooks = generated.get("hooks")
    if not isinstance(generated_hooks, Mapping):
        raise TypeError("Generated hooks are invalid.")

    added = 0
    for event, groups in generated_hooks.items():
        if not isinstance(groups, list):
            continue
        event_groups = existing_hooks.setdefault(str(event), [])
        if not isinstance(event_groups, list):
            raise TypeError(f"`hooks.{event}` must be an array in {target}.")
        known_commands: set[str] = set()
        for group in event_groups:
            known_commands.update(_handler_commands(group))
        for group in groups:
            commands = _handler_commands(group)
            if commands and commands.issubset(known_commands):
                continue
            event_groups.append(group)
            known_commands.update(commands)
            added += 1

    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{target.name}.",
        dir=target.parent,
        text=True,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(existing, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return target, added
