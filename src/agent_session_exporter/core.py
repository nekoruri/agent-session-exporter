"""Configuration, persistence, event normalization, and redaction."""

from __future__ import annotations

import hashlib
import json
import os
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

from .redaction import redact_text, redact_value, session_identity

CONFIG_FILE_NAME = "config.toml"
DEFAULT_DESTINATION = "ai-sessions"
UTC_TIMEZONE_NAMES = {"UTC", "Etc/UTC", "Etc/GMT", "GMT"}
CANONICAL_EVENT_NAMES = {
    name.casefold(): name
    for name in (
        "UserPromptSubmit",
        "MessageDisplay",
        "Stop",
        "StopFailure",
        "SessionEnd",
        "TaskComplete",
        "Error",
        "ImportedConversation",
        "CodexCloudTask",
        "CodexCloudTaskSubmitted",
    )
}


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
class ClaudeCloudConfig:
    """Claude Cloud Worker inbox configuration."""

    url: str = ""
    token_env: str = "AGENT_SESSION_EXPORTER_PULL_TOKEN"
    timeout_seconds: float = 3.0


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
    claude_cloud: ClaudeCloudConfig
    path_timezone: str = "UTC"
    buffer_key_path: Path | None = None


def _expand_optional_path(value: Any) -> Path | None:
    if value in (None, ""):
        return None
    return Path(str(value)).expanduser().resolve()


def _zoneinfo_name_from_path(path: Path) -> str:
    """Return an IANA key from a path below a zoneinfo directory."""
    normalized = path.as_posix()
    marker = "/zoneinfo/"
    if marker not in normalized:
        return ""
    name = normalized.split(marker, 1)[1].strip("/")
    for prefix in ("posix/", "right/"):
        if name.startswith(prefix):
            name = name[len(prefix) :]
            break
    return name


def _local_timezone_candidates() -> Iterable[str]:
    """Yield local timezone hints without requiring third-party packages."""
    environment = os.environ.get("TZ", "").strip()
    if environment:
        yield environment

    timezone = datetime.now().astimezone().tzinfo
    key = getattr(timezone, "key", "")
    if key:
        yield str(key)

    try:
        localtime = Path("/etc/localtime").resolve(strict=True)
    except OSError:
        pass
    else:
        name = _zoneinfo_name_from_path(localtime)
        if name:
            yield name

    try:
        timezone_file = Path("/etc/timezone").read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        pass
    else:
        name = timezone_file.splitlines()[0].strip() if timezone_file else ""
        if name:
            yield name


def _normalize_timezone_candidate(value: object) -> str:
    name = str(value or "").strip()
    if name.startswith(":"):
        name = name[1:]
    if name.startswith("/"):
        name = _zoneinfo_name_from_path(Path(name))
    return name


def _validate_path_timezone(value: object) -> str:
    name = _normalize_timezone_candidate(value)
    if name in UTC_TIMEZONE_NAMES:
        return "UTC"
    try:
        ZoneInfo(name)
    except (ValueError, ZoneInfoNotFoundError) as error:
        raise ValueError(f"Unknown path_timezone: {name}") from error
    return name


def detect_local_timezone() -> str:
    """Return the local IANA timezone name, falling back safely to UTC."""
    for candidate in _local_timezone_candidates():
        try:
            return _validate_path_timezone(candidate)
        except ValueError:
            continue
    return "UTC"


def _path_timezone(value: object) -> str:
    name = _normalize_timezone_candidate(value)
    if not name:
        return detect_local_timezone()
    return _validate_path_timezone(name)


def load_config(path: Path | None = None) -> Config:
    """Load application configuration, using safe defaults when absent."""
    config_path = (path or default_config_path()).expanduser()
    raw: dict[str, Any] = {}
    if config_path.exists():
        with config_path.open("rb") as stream:
            raw = tomllib.load(stream)

    claude_cloud_raw = raw.get("claude_cloud") or raw.get("collector") or {}
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
        claude_cloud=ClaudeCloudConfig(
            url=str(claude_cloud_raw.get("url") or "").rstrip("/"),
            token_env=str(
                claude_cloud_raw.get("token_env")
                or "AGENT_SESSION_EXPORTER_PULL_TOKEN"
            ),
            timeout_seconds=float(
                claude_cloud_raw.get("timeout_seconds") or 3.0
            ),
        ),
        path_timezone=_path_timezone(raw.get("path_timezone")),
        buffer_key_path=(
            _expand_optional_path(raw.get("buffer_key_path"))
            or config_path.with_suffix(".buffer-keys.json")
        ),
    )


def render_initial_config(
    vault_path: Path,
    destination: str,
    path_timezone: str | None = None,
) -> str:
    """Render a minimal initial TOML configuration."""
    escaped_vault = json.dumps(str(vault_path.expanduser().resolve()))
    escaped_device = json.dumps(socket.gethostname())
    escaped_timezone = json.dumps(_path_timezone(path_timezone))
    return (
        f"vault_path = {escaped_vault}\n"
        f'destination = "{destination.strip("/")}"\n'
        f"path_timezone = {escaped_timezone}\n"
        f"device_id = {escaped_device}\n"
        "redact = true\n"
        "include_tool_details = false\n"
        "sync_on_capture = true\n"
        "\n"
        "[claude_cloud]\n"
        'url = ""\n'
        'token_env = "AGENT_SESSION_EXPORTER_PULL_TOKEN"\n'
        "timeout_seconds = 3.0\n"
        "\n"
        "[project_aliases]\n"
        '# "github.com/example/repository" = "repository"\n'
    )


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


def canonical_event_name(value: object) -> str:
    """Return the canonical spelling for a known hook event name."""
    text = str(value).strip()
    return CANONICAL_EVENT_NAMES.get(text.casefold(), text or "Unknown")


def _single_workspace_root(payload: Mapping[str, Any]) -> str:
    roots = payload.get("workspace_roots") or payload.get("workspaceRoots")
    if not isinstance(roots, list):
        return ""
    values = [
        str(root).strip()
        for root in roots
        if root is not None and str(root).strip()
    ]
    return values[0] if len(values) == 1 else ""


def normalize_event(
    payload: Mapping[str, Any],
    source: str,
    config: Config,
    *,
    device_id: str | None = None,
    inspect_cwd: bool = True,
) -> dict[str, Any]:
    """Convert a raw hook or imported payload into the internal envelope."""
    cleaned_payload = dict(payload)
    session_id = _first_string(
        cleaned_payload,
        ["session_id", "sessionId", "task_id", "taskId", "id"],
    )
    supplied_session_id = session_id
    if not session_id:
        identity_payload = redact_value(cleaned_payload) if config.redact else cleaned_payload
        session_id = event_fingerprint(identity_payload)[:24]

    raw_event_name = (
        _first_string(
            cleaned_payload,
            ["hook_event_name", "event_name", "event", "type"],
        )
        or "Unknown"
    )
    event_name = canonical_event_name(raw_event_name)
    if config.redact and event_name == "MessageDisplay" and not supplied_session_id:
        raise ValueError("MessageDisplay requires a session_id.")
    for event_key in ("hook_event_name", "event_name", "event", "type"):
        if str(cleaned_payload.get(event_key) or "").strip() == raw_event_name:
            cleaned_payload[event_key] = event_name
            break
    occurred_at = (
        _first_string(
            cleaned_payload,
            ["timestamp", "occurred_at", "created_at", "createdAt"],
        )
        or now_iso()
    )
    cwd = _first_string(cleaned_payload, ["cwd", "working_directory"])
    if not cwd:
        cwd = _single_workspace_root(cleaned_payload)
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
    return finalize_event(envelope, config)


def finalize_event(
    envelope: Mapping[str, Any], config: Config, *, identity_key: str = "",
) -> dict[str, Any]:
    """Apply the storage policy after enrichment, then fingerprint the stored data."""
    # Only the authenticated Worker envelope may supply a pre-redaction identity.
    # Raw hook payloads never control this field.
    envelope = dict(envelope)
    identity_key = identity_key or session_identity(envelope["device_id"], envelope["session_id"])
    envelope["identity_key"] = identity_key
    if config.redact and envelope["event_name"] == "MessageDisplay":
        from .stream_buffer import PendingMessage, buffer_key_path, chunk_fields

        raw = dict(envelope)
        raw["payload"] = dict(envelope["payload"])
        chunk_fields(raw["payload"])
        metadata = redact_value({**raw, "payload": {}})
        metadata["identity_key"] = identity_key
        return PendingMessage(metadata, raw, buffer_key_path(config))
    envelope = redact_value(dict(envelope)) if config.redact else dict(envelope)
    envelope["identity_key"] = identity_key
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
                "identity_key",
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
    identity_key: str = ""

    def to_envelope(self) -> dict[str, Any]:
        """Return the event as an API envelope."""
        data = asdict(self)
        data.pop("id", None)
        return data


class EventStore:
    """SQLite-backed durable event store."""

    def __init__(self, state_dir: Path) -> None:
        self._session_groups: dict[
            tuple[str, str], list[tuple[str, str, str, str]]
        ] | None = None
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
                rendered_at TEXT NOT NULL,
                source_hash TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS message_chunks (
                stream TEXT NOT NULL,
                idx INTEGER NOT NULL CHECK (idx >= 0 AND idx < 4096),
                final INTEGER NOT NULL CHECK (final IN (0, 1)),
                key_id TEXT NOT NULL,
                nonce BLOB NOT NULL,
                ciphertext BLOB NOT NULL,
                PRIMARY KEY (stream, idx)
            );
            CREATE TABLE IF NOT EXISTS message_receipts (
                stream TEXT PRIMARY KEY,
                event_id INTEGER NOT NULL REFERENCES events(id)
            );
            """
        )
        render_state_columns = {
            str(row["name"])
            for row in self.connection.execute("PRAGMA table_info(render_state)")
        }
        if "source_hash" not in render_state_columns:
            self.connection.execute(
                "ALTER TABLE render_state "
                "ADD COLUMN source_hash TEXT NOT NULL DEFAULT ''"
            )
        if "identity_key" not in {row["name"] for row in self.connection.execute("PRAGMA table_info(events)")}:
            self.connection.execute("ALTER TABLE events ADD COLUMN identity_key TEXT NOT NULL DEFAULT ''")
        self.connection.commit()
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def add_event(self, envelope: Mapping[str, Any]) -> tuple[int, bool]:
        """Return (id, inserted); (0, False) means encrypted chunks are still pending."""
        from .stream_buffer import PendingMessage, stage_message

        if isinstance(envelope, PendingMessage):
            return stage_message(self, envelope)
        with self.connection:
            return self._insert_event(envelope)

    def _insert_event(self, envelope: Mapping[str, Any]) -> tuple[int, bool]:
        """Insert inside the caller's transaction."""
        cursor = self.connection.execute(
            """
            INSERT OR IGNORE INTO events (
                fingerprint, source, device_id, session_id, event_name,
                occurred_at, cwd, project, repository, branch,
                transcript_path, payload_json, received_at, identity_key
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                envelope.get("identity_key", ""),
            ),
        )
        if cursor.rowcount:
            self._session_groups = None
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
            identity_key=str(row["identity_key"]),
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

    def _grouped_sessions(self) -> dict[tuple[str, str], list[tuple[str, str, str, str]]]:
        if self._session_groups is not None:
            return self._session_groups
        # ponytail: scan distinct keys once per store; persist an alias index if capture latency grows.
        rows = self.connection.execute(
            """
            SELECT source, device_id, session_id, identity_key, MIN(id) AS first_id
            FROM events
            GROUP BY source, device_id, session_id, identity_key
            ORDER BY first_id
            """
        ).fetchall()
        self._session_groups = {}
        for row in rows:
            source, device, session = str(row["source"]), str(row["device_id"]), str(row["session_id"])
            identity = str(row["identity_key"])
            # Legacy rows have no provenance: treat their stored IDs as raw, never guess.
            canonical = (source, identity or session_identity(device, session))
            self._session_groups.setdefault(canonical, []).append((source, device, session, identity))
        return self._session_groups

    def list_session_keys(self) -> list[tuple[str, str, str, str]]:
        """List logical sessions, keeping the earliest stored key as representative."""
        return [aliases[0] for aliases in self._grouped_sessions().values()]

    def session_aliases(
        self, source: str, device_id: str, session_id: str, identity_key: str = "",
    ) -> list[tuple[str, str, str, str]]:
        """Look up raw IDs, or pass the persisted identity when using masked IDs."""
        canonical = (source, identity_key or session_identity(device_id, session_id))
        return self._grouped_sessions().get(canonical, [(source, device_id, session_id, identity_key)])

    def session_events(
        self,
        source: str,
        device_id: str,
        session_id: str,
        identity_key: str = "",
    ) -> list[StoredEvent]:
        """Load all events for one logical session."""
        events = []
        for alias in self.session_aliases(source, device_id, session_id, identity_key):
            rows = self.connection.execute(
                """
                SELECT * FROM events
                WHERE source = ? AND device_id = ? AND session_id = ? AND identity_key = ?
                ORDER BY id
                """,
                alias,
            ).fetchall()
            events.extend(self._row_to_event(row) for row in rows)
        return sorted(events, key=lambda event: event.id)

    @staticmethod
    def session_key(source: str, device_id: str, session_id: str, identity_key: str = "") -> str:
        """Build a stable session key."""
        if identity_key:
            return canonical_json([source, device_id, session_id, identity_key])
        return f"{source}\x1f{device_id}\x1f{session_id}"

    def get_render_state(self, session_key: str) -> sqlite3.Row | None:
        """Return the last render state for a session."""
        return self.connection.execute(
            "SELECT * FROM render_state WHERE session_key = ?",
            (session_key,),
        ).fetchone()

    def list_render_states(self) -> list[sqlite3.Row]:
        """Return all render states in stable path order."""
        return self.connection.execute(
            "SELECT * FROM render_state ORDER BY note_path, session_key"
        ).fetchall()

    def set_render_state(
        self,
        session_key: str,
        content_hash: str,
        note_path: str,
        *,
        source_hash: str | None = None,
        rendered_at: str | None = None,
        superseded_keys: Iterable[str] = (),
    ) -> None:
        """Persist the last rendered content hash and path."""
        self.connection.execute(
            """
            INSERT INTO render_state (
                session_key, content_hash, note_path, rendered_at, source_hash
            ) VALUES (?, ?, ?, ?, COALESCE(?, ''))
            ON CONFLICT(session_key) DO UPDATE SET
                content_hash = excluded.content_hash,
                note_path = excluded.note_path,
                rendered_at = excluded.rendered_at,
                source_hash = COALESCE(?, render_state.source_hash)
            """,
            (
                session_key,
                content_hash,
                note_path,
                rendered_at or now_iso(),
                source_hash,
                source_hash,
            ),
        )
        self.connection.executemany(
            "DELETE FROM render_state WHERE session_key = ? AND session_key <> ?",
            ((key, session_key) for key in superseded_keys),
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
