"""Import ChatGPT and Claude account data exports."""

from __future__ import annotations

import json
import zipfile
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .adapters import text_from_content
from .core import Config, EventStore, normalize_event

MAX_JSON_BYTES = 256 * 1024 * 1024


def _timestamp(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, UTC).isoformat()
        except (ValueError, OSError, OverflowError):
            return str(value)
    return str(value)


def _json_values(path: Path) -> Iterable[tuple[str, Any]]:
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as archive:
            for member in archive.infolist():
                name = member.filename.lower()
                if not name.endswith(".json") or member.is_dir():
                    continue
                if member.file_size > MAX_JSON_BYTES:
                    raise ValueError(f"JSON member is too large: {member.filename}")
                with archive.open(member) as stream:
                    try:
                        yield member.filename, json.load(stream)
                    except (json.JSONDecodeError, UnicodeError):
                        continue
        return
    if path.stat().st_size > MAX_JSON_BYTES:
        raise ValueError(f"JSON file is too large: {path.name}")
    with path.open(encoding="utf-8") as stream:
        yield path.name, json.load(stream)


def _chatgpt_nodes(conversation: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    mapping = conversation.get("mapping")
    if not isinstance(mapping, Mapping):
        return []
    current = conversation.get("current_node")
    chain: list[Mapping[str, Any]] = []
    visited: set[str] = set()
    while isinstance(current, str) and current not in visited:
        visited.add(current)
        node = mapping.get(current)
        if not isinstance(node, Mapping):
            break
        chain.append(node)
        current = node.get("parent")
    if chain:
        chain.reverse()
        return chain
    nodes = [node for node in mapping.values() if isinstance(node, Mapping)]
    return sorted(
        nodes,
        key=lambda node: float((node.get("message") or {}).get("create_time") or 0),
    )


def _chatgpt_conversation(value: Mapping[str, Any]) -> dict[str, Any] | None:
    if not isinstance(value.get("mapping"), Mapping):
        return None
    messages: list[dict[str, Any]] = []
    for node in _chatgpt_nodes(value):
        message = node.get("message")
        if not isinstance(message, Mapping):
            continue
        author = message.get("author")
        role = str(author.get("role") or "") if isinstance(author, Mapping) else ""
        if role not in {"user", "assistant", "system"}:
            continue
        content = message.get("content")
        text = ""
        if isinstance(content, Mapping):
            text = text_from_content(content.get("parts") or content.get("text"))
        else:
            text = text_from_content(content)
        if not text:
            continue
        messages.append(
            {
                "id": str(message.get("id") or ""),
                "role": role,
                "content": text,
                "timestamp": _timestamp(message.get("create_time")),
            }
        )
    conversation_id = str(value.get("id") or value.get("conversation_id") or "")
    if not conversation_id or not messages:
        return None
    return {
        "id": conversation_id,
        "title": str(value.get("title") or "ChatGPT conversation"),
        "created_at": _timestamp(value.get("create_time")),
        "updated_at": _timestamp(value.get("update_time")),
        "messages": messages,
    }


def _claude_messages(value: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw_messages = (
        value.get("chat_messages") or value.get("messages") or value.get("conversation")
    )
    if not isinstance(raw_messages, list):
        return []
    messages: list[dict[str, Any]] = []
    for item in raw_messages:
        if not isinstance(item, Mapping):
            continue
        raw_role = str(
            item.get("role") or item.get("sender") or item.get("author") or ""
        ).lower()
        role = {
            "human": "user",
            "user": "user",
            "assistant": "assistant",
            "claude": "assistant",
            "system": "system",
        }.get(raw_role, "")
        if not role:
            continue
        content = item.get("content") or item.get("text") or item.get("message")
        if isinstance(content, Mapping):
            text = text_from_content(
                content.get("text") or content.get("content") or content.get("parts")
            )
        else:
            text = text_from_content(content)
        if not text:
            continue
        messages.append(
            {
                "id": str(item.get("id") or item.get("uuid") or ""),
                "role": role,
                "content": text,
                "timestamp": _timestamp(
                    item.get("created_at")
                    or item.get("createdAt")
                    or item.get("timestamp")
                ),
            }
        )
    return messages


def _claude_conversation(value: Mapping[str, Any]) -> dict[str, Any] | None:
    messages = _claude_messages(value)
    if not messages:
        return None
    conversation_id = str(
        value.get("uuid")
        or value.get("id")
        or value.get("conversation_id")
        or value.get("conversationId")
        or ""
    )
    if not conversation_id:
        return None
    return {
        "id": conversation_id,
        "title": str(value.get("name") or value.get("title") or "Claude conversation"),
        "created_at": _timestamp(
            value.get("created_at")
            or value.get("createdAt")
            or value.get("created_time")
        ),
        "updated_at": _timestamp(
            value.get("updated_at")
            or value.get("updatedAt")
            or value.get("updated_time")
        ),
        "messages": messages,
    }


def _candidate_objects(value: Any) -> Iterable[Mapping[str, Any]]:
    if isinstance(value, list):
        for item in value:
            if isinstance(item, Mapping):
                yield item
        return
    if not isinstance(value, Mapping):
        return
    for key in ("conversations", "chats", "data"):
        nested = value.get(key)
        if isinstance(nested, list):
            for item in nested:
                if isinstance(item, Mapping):
                    yield item
            return
    yield value


def import_export(
    path: Path,
    config: Config,
    *,
    source: str = "auto",
) -> tuple[int, int]:
    """Import conversations and return inserted and examined counts."""
    if source not in {"auto", "chatgpt", "claude"}:
        raise ValueError("source must be auto, chatgpt, or claude.")
    inserted = 0
    examined = 0
    with EventStore(config.state_dir) as store:
        for filename, value in _json_values(path):
            for candidate in _candidate_objects(value):
                conversation: dict[str, Any] | None = None
                detected_source = source
                if source in {"auto", "chatgpt"}:
                    conversation = _chatgpt_conversation(candidate)
                    if conversation is not None:
                        detected_source = "chatgpt"
                if conversation is None and source in {"auto", "claude"}:
                    conversation = _claude_conversation(candidate)
                    if conversation is not None:
                        detected_source = "claude"
                if conversation is None:
                    continue
                examined += 1
                payload = {
                    "hook_event_name": "ImportedConversation",
                    "session_id": conversation["id"],
                    "timestamp": conversation.get("created_at") or "",
                    "project": f"{detected_source}-export",
                    "conversation": conversation,
                    "export_member": filename,
                }
                envelope = normalize_event(
                    payload,
                    f"{detected_source}-export",
                    config,
                )
                _, was_inserted = store.add_event(envelope)
                inserted += int(was_inserted)
    return inserted, examined
