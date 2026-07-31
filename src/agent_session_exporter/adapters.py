"""Adapters for local transcripts, hook payloads, and imported conversations."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .core import StoredEvent


@dataclass(frozen=True)
class Message:
    """One human-readable conversation message."""

    role: str
    text: str
    timestamp: str = ""
    message_id: str = ""


@dataclass
class SessionDocument:
    """Normalized session ready for Markdown rendering."""

    source: str
    device_id: str
    session_id: str
    project: str
    repository: str
    branch: str
    cwd: str
    started_at: str
    ended_at: str
    status: str
    event_count: int
    revision: str
    messages: list[Message] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


def text_from_content(content: Any) -> str:
    """Extract visible text from common API message representations."""
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""

    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
            continue
        if not isinstance(block, Mapping):
            continue
        block_type = str(block.get("type") or "")
        if block_type in {"text", "input_text", "output_text"}:
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "\n".join(part for part in parts if part).strip()


def _deduplicate(messages: Iterable[Message]) -> list[Message]:
    result: list[Message] = []
    seen: set[tuple[str, str, str]] = set()
    for message in messages:
        text = message.text.strip()
        if not text:
            continue
        key = (message.role, text, message.message_id)
        if key in seen:
            continue
        if result and not message.message_id:
            previous = result[-1]
            if previous.role == message.role and previous.text == text:
                continue
        seen.add(key)
        result.append(
            Message(
                role=message.role,
                text=text,
                timestamp=message.timestamp,
                message_id=message.message_id,
            )
        )
    return result


def _session_revision(events: Iterable[StoredEvent]) -> str:
    """Hash the ordered event fingerprints for downstream change detection."""
    digest = hashlib.sha256()
    for event in events:
        digest.update(event.fingerprint.encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def parse_codex_transcript(path: Path) -> tuple[list[Message], dict[str, Any]]:
    """Parse a Codex CLI JSONL transcript without retaining tool payloads."""
    messages: list[Message] = []
    metadata: dict[str, Any] = {"transcript_path": str(path)}

    with path.open(encoding="utf-8") as stream:
        for line in stream:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, Mapping):
                continue
            row_type = str(row.get("type") or "")
            payload = row.get("payload")
            if not isinstance(payload, Mapping):
                continue
            timestamp = str(row.get("timestamp") or payload.get("timestamp") or "")

            if row_type == "session_meta":
                for key in ("id", "session_id", "cwd", "cli_version", "source"):
                    if payload.get(key) not in (None, ""):
                        metadata[key] = payload[key]
                git = payload.get("git")
                if isinstance(git, Mapping):
                    metadata["git"] = dict(git)
                continue

            if row_type == "event_msg":
                event_type = str(payload.get("type") or "")
                if event_type == "user_message":
                    text = str(payload.get("message") or "")
                    messages.append(Message("user", text, timestamp))
                elif event_type == "agent_message":
                    text = str(payload.get("message") or "")
                    messages.append(Message("assistant", text, timestamp))
                continue

            if row_type != "response_item":
                continue
            if str(payload.get("type") or "") != "message":
                continue
            role = str(payload.get("role") or "")
            if role not in {"user", "assistant"}:
                continue
            text = text_from_content(payload.get("content"))
            messages.append(Message(role, text, timestamp))

    return _deduplicate(messages), metadata


def parse_claude_transcript(path: Path) -> tuple[list[Message], dict[str, Any]]:
    """Parse a Claude Code JSONL transcript and omit tool-only records."""
    messages: list[Message] = []
    metadata: dict[str, Any] = {"transcript_path": str(path)}

    with path.open(encoding="utf-8") as stream:
        for line in stream:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, Mapping):
                continue
            if row.get("isSidechain") or row.get("isMeta"):
                continue
            if row.get("sourceToolUseID") or row.get("toolUseResult"):
                continue

            row_type = str(row.get("type") or "")
            if row_type not in {"user", "assistant"}:
                continue
            message = row.get("message")
            if not isinstance(message, Mapping):
                continue
            role = str(message.get("role") or row_type)
            if role not in {"user", "assistant"}:
                continue
            text = text_from_content(message.get("content"))
            if not text:
                continue
            timestamp = str(row.get("timestamp") or "")
            message_id = str(
                message.get("id") or row.get("uuid") or row.get("messageId") or ""
            )
            messages.append(Message(role, text, timestamp, message_id))
            for key in ("sessionId", "cwd", "version"):
                if row.get(key) not in (None, ""):
                    metadata[key] = row[key]

    return _deduplicate(messages), metadata


def messages_from_hook_events(events: list[StoredEvent]) -> list[Message]:
    """Recover conversation text from command and HTTP hook events."""
    messages: list[Message] = []
    display_chunks: dict[str, dict[int, tuple[str, str]]] = {}
    display_order: list[str] = []

    for event in events:
        payload = event.payload
        name = event.event_name
        timestamp = event.occurred_at

        if name == "UserPromptSubmit":
            text = str(
                payload.get("prompt")
                or payload.get("user_prompt")
                or payload.get("message")
                or ""
            )
            messages.append(Message("user", text, timestamp))
            continue

        if name == "MessageDisplay":
            message_id = str(
                payload.get("message_id")
                or payload.get("messageId")
                or f"display-{event.id}"
            )
            if message_id not in display_chunks:
                display_order.append(message_id)
                display_chunks[message_id] = {}
            delta = str(
                payload.get("delta")
                or payload.get("text")
                or payload.get("message")
                or ""
            )
            index_value = payload.get("index", 0)
            try:
                index = int(index_value)
            except (TypeError, ValueError):
                index = len(display_chunks[message_id])
            display_chunks[message_id][index] = (delta, timestamp)
            if bool(payload.get("final")):
                chunks = sorted(display_chunks.pop(message_id).items())
                text = "".join(chunk for _, (chunk, _) in chunks)
                messages.append(Message("assistant", text, timestamp, message_id))
                display_order.remove(message_id)
            continue

        if name in {"Stop", "TaskComplete"}:
            text = str(
                payload.get("last_assistant_message")
                or payload.get("last_agent_message")
                or payload.get("response")
                or ""
            )
            if not (
                messages
                and messages[-1].role == "assistant"
                and messages[-1].text.strip() == text.strip()
            ):
                messages.append(Message("assistant", text, timestamp))

    for message_id in display_order:
        chunks = sorted(display_chunks[message_id].items())
        text = "".join(chunk for _, (chunk, _) in chunks)
        timestamp = chunks[-1][1][1] if chunks else ""
        messages.append(Message("assistant", text, timestamp, message_id))

    return _deduplicate(messages)


def _imported_document(
    event: StoredEvent,
    base: SessionDocument,
) -> SessionDocument:
    payload = event.payload
    conversation = payload.get("conversation")
    if not isinstance(conversation, Mapping):
        return base
    messages_raw = conversation.get("messages")
    if isinstance(messages_raw, list):
        for item in messages_raw:
            if not isinstance(item, Mapping):
                continue
            role = str(item.get("role") or "")
            if role not in {"user", "assistant", "system"}:
                continue
            text = text_from_content(item.get("content") or item.get("text"))
            if text:
                base.messages.append(
                    Message(
                        role,
                        text,
                        str(item.get("timestamp") or ""),
                        str(item.get("id") or ""),
                    )
                )
    base.started_at = str(conversation.get("created_at") or base.started_at)
    base.ended_at = str(conversation.get("updated_at") or base.ended_at)
    base.status = "completed"
    title = conversation.get("title")
    if title:
        base.metadata["imported_title"] = str(title)
    return base


def _cloud_document(
    event: StoredEvent,
    base: SessionDocument,
) -> SessionDocument:
    payload = event.payload
    prompt = payload.get("prompt") or payload.get("query") or payload.get("input")
    if isinstance(prompt, str) and prompt.strip():
        base.messages.append(Message("user", prompt, event.occurred_at))
    output = payload.get("output") or payload.get("result") or payload.get("summary")
    if isinstance(output, str) and output.strip():
        base.messages.append(Message("assistant", output, event.occurred_at))
    diff = payload.get("diff")
    if isinstance(diff, str) and diff.strip():
        base.metadata["diff"] = diff
    state = payload.get("status") or payload.get("state")
    if state:
        base.status = str(state).lower()
    title = payload.get("title")
    if title:
        base.metadata["imported_title"] = str(title)
    for key in (
        "task_id",
        "environment_id",
        "environment_label",
        "url",
        "status_text",
    ):
        if payload.get(key) not in (None, ""):
            base.metadata[key] = payload[key]
    return base


def build_session_document(events: list[StoredEvent]) -> SessionDocument:
    """Build one normalized document from a session's stored events."""
    if not events:
        raise ValueError("Cannot build a session without events.")
    first = events[0]
    last = events[-1]
    document = SessionDocument(
        source=first.source,
        device_id=first.device_id,
        session_id=first.session_id,
        project=next((event.project for event in events if event.project), "unknown"),
        repository=next(
            (event.repository for event in events if event.repository),
            "",
        ),
        branch=next((event.branch for event in events if event.branch), ""),
        cwd=next((event.cwd for event in events if event.cwd), ""),
        started_at=first.occurred_at,
        ended_at=last.occurred_at,
        status="active",
        event_count=len(events),
        revision=_session_revision(events),
        metadata={},
    )

    if any(event.event_name == "SessionEnd" for event in events):
        document.status = "completed"
    elif any(event.event_name in {"StopFailure", "Error"} for event in events):
        document.status = "failed"
    elif any(event.event_name in {"Stop", "TaskComplete"} for event in events):
        document.status = "stopped"

    if first.event_name == "ImportedConversation":
        return _imported_document(first, document)
    if first.source == "codex-cloud":
        for event in events:
            _cloud_document(event, document)
        document.messages = _deduplicate(document.messages)
        return document

    transcript_candidates = [
        Path(event.transcript_path).expanduser()
        for event in reversed(events)
        if event.transcript_path
    ]
    for path in transcript_candidates:
        try:
            if first.source.startswith("codex"):
                messages, metadata = parse_codex_transcript(path)
            else:
                messages, metadata = parse_claude_transcript(path)
        except (OSError, UnicodeError):
            continue
        if messages:
            document.messages = messages
            document.metadata.update(metadata)
            break

    hook_messages = messages_from_hook_events(events)
    if not document.messages:
        document.messages = hook_messages
    elif hook_messages:
        transcript_counts = Counter(
            (message.role, message.text) for message in document.messages
        )
        seen_hook_counts: Counter[tuple[str, str]] = Counter()
        for message in hook_messages:
            key = (message.role, message.text)
            seen_hook_counts[key] += 1
            if seen_hook_counts[key] > transcript_counts[key]:
                document.messages.append(message)
    return document
