"""Configuration, persistence, event normalization, and redaction."""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

CONFIG_FILE_NAME = "config.toml"
DEFAULT_DESTINATION = "ai-sessions"
SECRET_KEY_RE = re.compile(
    r"(?:^|[_-])(?:api[_-]?key|access[_-]?token|client[_-]?secret|"
    r"secret|token|password|passwd|authorization|cookie)(?:$|[_-])",
    re.IGNORECASE,
)
SECRET_VALUE_RES = [
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{12,}", re.IGNORECASE),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}"),
    re.compile(r"\b(?:gh[opsu]_|github_pat_)[A-Za-z0-9_]{12,}"),
]


def now_iso() -> str:
    """Return the current UTC time in ISO 8601 format."""
    return datetime.now(UTC).isoformat(timespec="seconds")


def default_config_path() -> Path:
    """Return the XDG-compatible configuration path."""
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".config"
    return root / "agent-session-exporter" / CONFIG_FILE_NAME


def default_state_dir() -> Path:
    """Return the XDG-compatible state directory."""
    base = os.environ.get("XDG_STATE_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".local" / "state"
    return root / "agent-session-exporter"


@dataclass(frozen=True)
class CollectorConfig:
    """Remote collector configuration."""

    url: str = ""
    token_env: str = "AGENT_SESSION_EXPORTER_TOKEN"
    timeout_seconds: float = 3.0


@dataclass(frozen=True)
class ServerConfig:
    """Local HTTP collector configuration."""

    listen: str = "127.0.0.1"
    port: int = 8765
    token_env: str = "AGENT_SESSION_EXPORTER_TOKEN"


@dataclass(frozen=True)
class Config:
    """Application configuration."""

    vault_path: Path | None
    destination: str
    state_dir: Path
    device_id: str
    redact: bool
    include_tool_details: bool
    sync_on_capture: bool
    project_aliases: dict[str, str]
    collector: CollectorConfig
    server: ServerConfig
    path_timezone: str = "UTC"


def _expand_optional_path(value: Any) -> Path | None:
    if value in (None, ""):
        return None
    return Path(str(value)).expanduser().resolve()


def _path_timezone(value: object) -> str:
    name = str(value or "UTC").strip()
    if name == "UTC":
        return name
    try:
        ZoneInfo(name)
    except ZoneInfoNotFoundError as error:
        raise ValueError(f"Unknown path_timezone: {name}") from error
    return name


def load_config(path: Path | None = None) -> Config:
    """Load application configuration, using safe defaults when absent."""
    config_path = (path or default_config_path()).expanduser()
    raw: dict[str, Any] = {}
    if config_path.exists():
        with config_path.open("rb") as stream:
            raw = tomllib.load(stream)

    collector_raw = raw.get("collector") or {}
    server_raw = raw.get("server") or {}
    aliases = raw.get("project_aliases") or {}
    state_value = raw.get("state_dir")

    return Config(
        vault_path=_expand_optional_path(raw.get("vault_path")),
        destination=str(raw.get("destination") or DEFAULT_DESTINATION).strip("/"),
        state_dir=(
            Path(str(state_value)).expanduser().resolve()
            if state_value
            else default_state_dir()
        ),
        device_id=str(raw.get("device_id") or socket.gethostname()),
        redact=bool(raw.get("redact", True)),
        include_tool_details=bool(raw.get("include_tool_details", False)),
        sync_on_capture=bool(raw.get("sync_on_capture", True)),
        project_aliases={str(key): str(value) for key, value in aliases.items()},
        collector=CollectorConfig(
            url=str(collector_raw.get("url") or "").rstrip("/"),
            token_env=str(
                collector_raw.get("token_env") or "AGENT_SESSION_EXPORTER_TOKEN"
            ),
            timeout_seconds=float(collector_raw.get("timeout_seconds") or 3.0),
        ),
        server=ServerConfig(
            listen=str(server_raw.get("listen") or "127.0.0.1"),
            port=int(server_raw.get("port") or 8765),
            token_env=str(
                server_raw.get("token_env") or "AGENT_SESSION_EXPORTER_TOKEN"
            ),
        ),
        path_timezone=_path_timezone(raw.get("path_timezone")),
    )


def render_initial_config(vault_path: Path, destination: str) -> str:
    """Render a minimal initial TOML configuration."""
    escaped_vault = json.dumps(str(vault_path.expanduser().resolve()))
    escaped_device = json.dumps(socket.gethostname())
    return (
        f"vault_path = {escaped_vault}\n"
        f'destination = "{destination.strip("/")}"\n'
        'path_timezone = "UTC"\n'
        f"device_id = {escaped_device}\n"
        "redact = true\n"
        "include_tool_details = false\n"
        "sync_on_capture = true\n"
        "\n"
        "[collector]\n"
        'url = ""\n'
        'token_env = "AGENT_SESSION_EXPORTER_TOKEN"\n'
        "timeout_seconds = 3.0\n"
        "\n"
        "[server]\n"
        'listen = "127.0.0.1"\n'
        "port = 8765\n"
        'token_env = "AGENT_SESSION_EXPORTER_TOKEN"\n'
        "\n"
        "[project_aliases]\n"
        '# "github.com/example/repository" = "repository"\n'
    )


def redact_text(value: str) -> str:
    """Redact common credential shapes from text."""
    result = value
    for pattern in SECRET_VALUE_RES:
        result = pattern.sub("[REDACTED]", result)
    return result


def redact_value(value: Any, key: str = "") -> Any:
    """Recursively redact credentials without altering JSON structure."""
    if key and SECRET_KEY_RE.search(key):
        return "[REDACTED]"
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, dict):
        return {
            str(child_key): redact_value(child_value, str(child_key))
            for child_key, child_value in value.items()
        }
    return value


def canonical_json(value: Any) -> str:
    """Serialize JSON deterministically."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def event_fingerprint(value: Mapping[str, Any]) -> str:
    """Return a stable event fingerprint."""
    encoded = canonical_json(value).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _run_git(cwd: Path, args: list[str]) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=1.0,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def inspect_project(cwd_value: str, aliases: Mapping[str, str]) -> dict[str, str]:
    """Resolve repository and project identity from a working directory."""
    cwd = Path(cwd_value).expanduser()
    try:
        cwd = cwd.resolve()
    except OSError:
        pass

    git_root_text = _run_git(cwd, ["rev-parse", "--show-toplevel"])
    git_root = Path(git_root_text) if git_root_text else None
    repository = _run_git(cwd, ["remote", "get-url", "origin"])
    branch = _run_git(cwd, ["branch", "--show-current"])

    identity_candidates = [
        repository,
        str(git_root) if git_root else "",
        str(cwd),
    ]
    project = ""
    for candidate in identity_candidates:
        if candidate and candidate in aliases:
            project = aliases[candidate]
            break

    if not project:
        if repository:
            trimmed = repository.rstrip("/").removesuffix(".git")
            project = trimmed.rsplit("/", 1)[-1].rsplit(":", 1)[-1]
        elif git_root:
            project = git_root.name
        else:
            project = cwd.name or "unknown"

    return {
        "cwd": str(cwd),
        "git_root": str(git_root) if git_root else "",
        "repository": repository,
        "branch": branch,
        "project": project or "unknown",
    }


def _first_string(payload: Mapping[str, Any], keys: Iterable[str]) -> str:
    for key in keys:
        value = payload.get(key)
        if value is not None and str(value).strip():
            return str(value)
    return ""


def normalize_event(
    payload: Mapping[str, Any],
    source: str,
    config: Config,
    *,
    device_id: str | None = None,
    inspect_cwd: bool = True,
) -> dict[str, Any]:
    """Convert a raw hook or imported payload into the internal envelope."""
    cleaned_payload = redact_value(dict(payload)) if config.redact else dict(payload)
    session_id = _first_string(
        cleaned_payload,
        ["session_id", "sessionId", "task_id", "taskId", "id"],
    )
    if not session_id:
        session_id = event_fingerprint(cleaned_payload)[:24]

    event_name = (
        _first_string(
            cleaned_payload,
            ["hook_event_name", "event_name", "event", "type"],
        )
        or "Unknown"
    )
    occurred_at = (
        _first_string(
            cleaned_payload,
            ["timestamp", "occurred_at", "created_at", "createdAt"],
        )
        or now_iso()
    )
    cwd = _first_string(cleaned_payload, ["cwd", "working_directory"])
    project_info = (
        inspect_project(cwd, config.project_aliases)
        if cwd and inspect_cwd
        else {
            "cwd": "",
            "git_root": "",
            "repository": "",
            "branch": "",
            "project": str(
                cleaned_payload.get("project")
                or (Path(cwd).name if cwd else "")
                or "unknown"
            ),
        }
    )
    if cwd and not inspect_cwd:
        project_info["cwd"] = cwd

    envelope: dict[str, Any] = {
        "source": source,
        "device_id": device_id or config.device_id,
        "session_id": session_id,
        "event_name": event_name,
        "occurred_at": occurred_at,
        "cwd": project_info["cwd"],
        "project": str(cleaned_payload.get("project") or project_info["project"]),
        "repository": str(
            cleaned_payload.get("repository") or project_info["repository"]
        ),
        "branch": str(cleaned_payload.get("branch") or project_info["branch"]),
        "transcript_path": _first_string(
            cleaned_payload,
            ["transcript_path", "transcriptPath"],
        ),
        "payload": cleaned_payload,
        "received_at": now_iso(),
    }
    envelope["fingerprint"] = event_fingerprint(
        {
            key: envelope[key]
            for key in (
                "source",
                "device_id",
                "session_id",
                "event_name",
                "occurred_at",
                "payload",
            )
        }
    )
    return envelope


@dataclass(frozen=True)
class StoredEvent:
    """Event loaded from SQLite."""

    id: int
    fingerprint: str
    source: str
    device_id: str
    session_id: str
    event_name: str
    occurred_at: str
    cwd: str
    project: str
    repository: str
    branch: str
    transcript_path: str
    payload: dict[str, Any]
    received_at: str

    def to_envelope(self) -> dict[str, Any]:
        """Return the event as an API envelope."""
        data = asdict(self)
        data.pop("id", None)
        return data


class EventStore:
    """SQLite-backed durable event store."""

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self.state_dir.chmod(0o700)
        except OSError:
            pass
        self.path = self.state_dir / "events.sqlite3"
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self._initialize()

    def close(self) -> None:
        """Close the database connection."""
        self.connection.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _initialize(self) -> None:
        self.connection.executescript(
            """
            PRAGMA journal_mode = WAL;
            PRAGMA foreign_keys = ON;

            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fingerprint TEXT NOT NULL UNIQUE,
                source TEXT NOT NULL,
                device_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                event_name TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                cwd TEXT NOT NULL,
                project TEXT NOT NULL,
                repository TEXT NOT NULL,
                branch TEXT NOT NULL,
                transcript_path TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                received_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS events_session_idx
            ON events(source, device_id, session_id, id);

            CREATE TABLE IF NOT EXISTS render_state (
                session_key TEXT PRIMARY KEY,
                content_hash TEXT NOT NULL,
                note_path TEXT NOT NULL,
                rendered_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        self.connection.commit()
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def add_event(self, envelope: Mapping[str, Any]) -> tuple[int, bool]:
        """Insert an event and return its id and whether it was newly added."""
        cursor = self.connection.execute(
            """
            INSERT OR IGNORE INTO events (
                fingerprint, source, device_id, session_id, event_name,
                occurred_at, cwd, project, repository, branch,
                transcript_path, payload_json, received_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                envelope["fingerprint"],
                envelope["source"],
                envelope["device_id"],
                envelope["session_id"],
                envelope["event_name"],
                envelope["occurred_at"],
                envelope.get("cwd", ""),
                envelope.get("project", "unknown"),
                envelope.get("repository", ""),
                envelope.get("branch", ""),
                envelope.get("transcript_path", ""),
                canonical_json(envelope.get("payload", {})),
                envelope.get("received_at", now_iso()),
            ),
        )
        self.connection.commit()
        if cursor.rowcount:
            return int(cursor.lastrowid), True
        row = self.connection.execute(
            "SELECT id FROM events WHERE fingerprint = ?",
            (envelope["fingerprint"],),
        ).fetchone()
        return int(row["id"]), False

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> StoredEvent:
        return StoredEvent(
            id=int(row["id"]),
            fingerprint=str(row["fingerprint"]),
            source=str(row["source"]),
            device_id=str(row["device_id"]),
            session_id=str(row["session_id"]),
            event_name=str(row["event_name"]),
            occurred_at=str(row["occurred_at"]),
            cwd=str(row["cwd"]),
            project=str(row["project"]),
            repository=str(row["repository"]),
            branch=str(row["branch"]),
            transcript_path=str(row["transcript_path"]),
            payload=json.loads(row["payload_json"]),
            received_at=str(row["received_at"]),
        )

    def list_events(
        self,
        *,
        after: int = 0,
        limit: int = 500,
    ) -> list[StoredEvent]:
        """List events after an id."""
        rows = self.connection.execute(
            "SELECT * FROM events WHERE id > ? ORDER BY id LIMIT ?",
            (after, min(max(limit, 1), 5000)),
        ).fetchall()
        return [self._row_to_event(row) for row in rows]

    def list_session_keys(self) -> list[tuple[str, str, str]]:
        """List distinct source, device, and session tuples."""
        rows = self.connection.execute(
            """
            SELECT source, device_id, session_id, MIN(id) AS first_id
            FROM events
            GROUP BY source, device_id, session_id
            ORDER BY first_id
            """
        ).fetchall()
        return [
            (str(row["source"]), str(row["device_id"]), str(row["session_id"]))
            for row in rows
        ]

    def session_events(
        self,
        source: str,
        device_id: str,
        session_id: str,
    ) -> list[StoredEvent]:
        """Load all events for one logical session."""
        rows = self.connection.execute(
            """
            SELECT * FROM events
            WHERE source = ? AND device_id = ? AND session_id = ?
            ORDER BY id
            """,
            (source, device_id, session_id),
        ).fetchall()
        return [self._row_to_event(row) for row in rows]

    @staticmethod
    def session_key(source: str, device_id: str, session_id: str) -> str:
        """Build a stable session key."""
        return f"{source}\x1f{device_id}\x1f{session_id}"

    def get_render_state(self, session_key: str) -> sqlite3.Row | None:
        """Return the last render state for a session."""
        return self.connection.execute(
            "SELECT * FROM render_state WHERE session_key = ?",
            (session_key,),
        ).fetchone()

    def set_render_state(
        self,
        session_key: str,
        content_hash: str,
        note_path: str,
    ) -> None:
        """Persist the last rendered content hash and path."""
        self.connection.execute(
            """
            INSERT INTO render_state (
                session_key, content_hash, note_path, rendered_at
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT(session_key) DO UPDATE SET
                content_hash = excluded.content_hash,
                note_path = excluded.note_path,
                rendered_at = excluded.rendered_at
            """,
            (session_key, content_hash, note_path, now_iso()),
        )
        self.connection.commit()

    def get_metadata(self, key: str, default: str = "") -> str:
        """Read a metadata value."""
        row = self.connection.execute(
            "SELECT value FROM metadata WHERE key = ?",
            (key,),
        ).fetchone()
        return str(row["value"]) if row else default

    def set_metadata(self, key: str, value: str) -> None:
        """Write a metadata value."""
        self.connection.execute(
            """
            INSERT INTO metadata(key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )
        self.connection.commit()


def read_stdin_json() -> dict[str, Any]:
    """Read one JSON object from stdin."""
    raw = sys.stdin.read()
    if not raw.strip():
        raise ValueError("Expected a JSON object on stdin.")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise TypeError("Expected a JSON object on stdin.")
    return value
