"""Encrypted, durable staging for MessageDisplay; only complete messages are published."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAX_CHUNKS = 4096
MAX_CHUNK_BYTES = 1024 * 1024
MAX_MESSAGE_BYTES = 8 * 1024 * 1024
MAX_BUFFER_BYTES = 64 * 1024 * 1024


def buffer_key_path(config) -> Path:
    from .core import default_config_path

    path = (config.buffer_key_path or default_config_path().with_suffix(".buffer-keys.json")).resolve()
    for excluded in (config.state_dir, config.vault_path):
        if excluded is not None and path.is_relative_to(excluded.resolve()):
            raise ValueError("Buffer keys must be outside the state directory and Vault.")
    return path


def encoded(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def chunk_fields(payload: dict) -> tuple[str, int, bool, str]:
    message_id = payload.get("message_id", payload.get("messageId"))
    index, final = payload.get("index"), payload.get("final")
    delta = payload.get("delta", payload.get("text", payload.get("message", "")))
    if (
        not isinstance(message_id, str) or not message_id or len(message_id) > 1024
        or type(index) is not int or not 0 <= index < MAX_CHUNKS
        or type(final) is not bool or not isinstance(delta, str)
    ):
        raise ValueError("Invalid MessageDisplay message_id, index, final or delta.")
    for alias in ("message_id", "messageId"):
        if alias in payload and payload[alias] != message_id:
            raise ValueError("Conflicting MessageDisplay identifiers.")
    for alias in ("delta", "text", "message"):
        if alias in payload and payload[alias] != delta:
            raise ValueError("Conflicting MessageDisplay text fields.")
    if len(encoded(payload)) > MAX_CHUNK_BYTES:
        raise ValueError("MessageDisplay chunk is too large.")
    return message_id, index, final, delta


def validate_keys(value: Any) -> dict:
    try:
        keys = value["keys"]
        if not keys or value["active"] not in keys:
            raise ValueError
        for key_id, key in keys.items():
            if not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{64}", key):
                raise ValueError
            if hashlib.sha256(bytes.fromhex(key)).hexdigest()[:16] != key_id:
                raise ValueError
    except (TypeError, KeyError, AttributeError, ValueError):
        raise ValueError("Invalid buffer key file.") from None
    return value


def new_key(value: dict) -> None:
    key = AESGCM.generate_key(bit_length=256)
    key_id = hashlib.sha256(key).hexdigest()[:16]
    value["keys"][key_id] = key.hex()
    value["active"] = key_id


@contextmanager
def locked_keys(path: Path, *, create: bool = False):
    # Hooks already run on POSIX; flock also serializes initial creation and rotation.
    import fcntl

    path = path.resolve()
    if any((parent / ".git").is_file() or (parent / ".git" / "HEAD").is_file()
           for parent in path.parents):
        raise ValueError("Buffer keys must be outside Git working trees.")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.parent.stat().st_mode & 0o077:
        raise ValueError("Buffer key directory must have mode 700.")
    descriptor = os.open(path.with_suffix(path.suffix + ".lock"), os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(descriptor, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if path.exists():
            info = path.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
                raise ValueError("Buffer key file must have mode 600.")
            try:
                value = validate_keys(json.loads(path.read_text()))
            except (UnicodeError, json.JSONDecodeError):
                raise ValueError("Invalid buffer key file.") from None
        elif create:
            value = {"active": "", "keys": {}}
            new_key(value)
            write_keys(path, value)
        else:
            raise ValueError("Buffer key file is missing; restore it before ingesting chunks.")
        yield value


def write_keys(path: Path, value: dict) -> None:
    """Replace atomically while holding locked_keys; never print key material."""
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(encoded(value))
            stream.flush()
            os.fsync(stream.fileno())
            os.replace(temporary, path)
            descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        finally:
            temporary.unlink(missing_ok=True)


def manage_keys(store, path: Path, *, rotate: bool = False, retire: str | None = None) -> dict:
    """Initialize/rotate keys; refuse to remove any key still used by this store."""
    db = store.connection
    db.execute("BEGIN IMMEDIATE")
    try:
        used = {row[0] for row in db.execute("SELECT DISTINCT key_id FROM message_chunks")}
        with locked_keys(path, create=not used) as keys:
            if not used.issubset(keys["keys"]):
                raise ValueError("Buffered message keys are missing; restore them before rotation.")
            if retire is not None:
                if retire == keys["active"] or retire in used or retire not in keys["keys"]:
                    raise ValueError("Cannot retire an active, in-use or unknown buffer key.")
                del keys["keys"][retire]
            if rotate:
                new_key(keys)
            write_keys(path, keys)
            return {"active": keys["active"], "retained": list(keys["keys"]), "in_use": sorted(used)}
    finally:
        db.rollback()


class PendingMessage(dict):
    """In-memory handoff from normalization to EventStore; raw is never an SQL value."""

    def __init__(self, metadata: dict, raw: dict, key_path: Path):
        super().__init__(metadata)
        self.raw = raw
        self.key_path = key_path


def stage_message(store, pending: PendingMessage) -> tuple[int, bool]:
    from .core import event_fingerprint
    from .redaction import canonical_identity, redact_value, session_identity

    raw = pending.raw
    message_id, index, final, _delta = chunk_fields(raw["payload"])
    stream_identity = [
        raw["source"], canonical_identity(raw["device_id"]),
        canonical_identity(raw["session_id"]), message_id,
    ]
    if raw["identity_key"] != session_identity(raw["device_id"], raw["session_id"]):
        # Worker IDs may already be masked; its persisted provenance distinguishes them.
        stream_identity.append(raw["identity_key"])
    stream_id = hashlib.sha256(encoded(stream_identity)).hexdigest()
    db = store.connection
    db.execute("BEGIN IMMEDIATE")
    try:
        receipt = db.execute("SELECT event_id FROM message_receipts WHERE stream = ?", (stream_id,)).fetchone()
        if receipt:
            db.commit()
            return int(receipt[0]), False
        has_pending = db.execute("SELECT 1 FROM message_chunks LIMIT 1").fetchone() is not None
        with locked_keys(pending.key_path, create=not has_pending) as keys:
            def decrypt(row):
                try:
                    key = bytes.fromhex(keys["keys"][row["key_id"]])
                    aad = encoded([stream_id, row["idx"], bool(row["final"]), row["key_id"]])
                    return json.loads(AESGCM(key).decrypt(row["nonce"], row["ciphertext"], aad))
                except (KeyError, InvalidTag, ValueError, UnicodeError):
                    raise ValueError("Cannot decrypt buffered message; restore the correct keys.") from None

            rows = db.execute("SELECT * FROM message_chunks WHERE stream = ? ORDER BY idx", (stream_id,)).fetchall()
            previous = next((row for row in rows if row["idx"] == index), None)
            if previous:
                if decrypt(previous)["payload"] != raw["payload"]:
                    raise ValueError("Conflicting MessageDisplay retry.")
            else:
                plaintext = encoded(raw)
                total = db.execute("SELECT COALESCE(SUM(length(ciphertext)), 0) FROM message_chunks").fetchone()[0]
                if (sum(len(row["ciphertext"]) for row in rows) + len(plaintext) + 16 > MAX_MESSAGE_BYTES
                        or total + len(plaintext) + 16 > MAX_BUFFER_BYTES):
                    raise ValueError("Encrypted message buffer is full; pending data was retained.")
                key_id = keys["active"]
                nonce = secrets.token_bytes(12)
                aad = encoded([stream_id, index, final, key_id])
                ciphertext = AESGCM(bytes.fromhex(keys["keys"][key_id])).encrypt(nonce, plaintext, aad)
                db.execute("INSERT INTO message_chunks VALUES (?, ?, ?, ?, ?, ?)",
                           (stream_id, index, int(final), key_id, nonce, ciphertext))
                rows = db.execute("SELECT * FROM message_chunks WHERE stream = ? ORDER BY idx", (stream_id,)).fetchall()
            finals = [row["idx"] for row in rows if row["final"]]
            if len(finals) > 1 or (finals and rows[-1]["idx"] > finals[0]):
                raise ValueError("Conflicting MessageDisplay final index.")
            if not finals or len(rows) != finals[0] + 1:
                db.commit()
                return 0, False
            # Retain the encrypted snapshot and keys in memory, then release both locks.
            db.commit()
    except BaseException:
        db.rollback()
        raise

    chunks = [decrypt(row) for row in rows]
    event = chunks[0]
    payload = event["payload"]
    text = "".join(chunk_fields(chunk["payload"])[3] for chunk in chunks)
    for alias in ("text", "message", "messageId"):
        payload.pop(alias, None)
    payload.update(message_id=message_id, index=0, final=True, delta=text)
    event = redact_value(event)
    event["identity_key"] = raw["identity_key"]
    event["fingerprint"] = event_fingerprint({key: event[key] for key in
        ("source", "device_id", "session_id", "event_name", "occurred_at", "payload", "identity_key")})

    db.execute("BEGIN IMMEDIATE")
    try:
        receipt = db.execute("SELECT event_id FROM message_receipts WHERE stream = ?", (stream_id,)).fetchone()
        if receipt:
            db.commit()
            return int(receipt[0]), False
        current = db.execute("SELECT * FROM message_chunks WHERE stream = ? ORDER BY idx", (stream_id,)).fetchall()
        if current != rows:
            raise ValueError("Buffered message changed during inspection; retry the chunk.")
        event_id, inserted = store._insert_event(event)
        db.execute("INSERT INTO message_receipts VALUES (?, ?)", (stream_id, event_id))
        db.execute("DELETE FROM message_chunks WHERE stream = ?", (stream_id,))
        db.commit()
        return event_id, inserted
    except BaseException:
        db.rollback()
        raise
