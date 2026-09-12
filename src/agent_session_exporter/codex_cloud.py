"""Codex Cloud metadata ingestion through the public Codex CLI."""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Mapping
from typing import Any
from uuid import uuid4

from .core import Config, EventStore, finalize_event, now_iso
from .redaction import redact_text


def _run_codex_cloud(arguments: list[str]) -> str:
    result = subprocess.run(
        ["codex", "cloud", *arguments],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"codex cloud failed: {message}")
    return result.stdout


def _task_list(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [dict(item) for item in value if isinstance(item, Mapping)]
    if isinstance(value, Mapping):
        for key in ("tasks", "items", "data"):
            items = value.get(key)
            if isinstance(items, list):
                return [dict(item) for item in items if isinstance(item, Mapping)]
    raise RuntimeError("Unexpected `codex cloud list --json` response.")


def _first_task_value(task: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = task.get(key)
        if value not in (None, ""):
            return str(value)
    return ""


def sync_codex_cloud(
    config: Config,
    *,
    limit: int = 20,
    include_details: bool = False,
) -> int:
    """Ingest the Codex Cloud task list and optionally status and diff."""
    if not 1 <= limit <= 20:
        raise ValueError("Codex Cloud limit must be between 1 and 20.")
    raw = _run_codex_cloud(["list", "--json", "--limit", str(limit)])
    tasks = _task_list(json.loads(raw))
    inserted_count = 0
    with EventStore(config.state_dir) as store:
        for task in tasks:
            task_id = _first_task_value(task, "id", "task_id", "taskId")
            if not task_id:
                continue
            payload: dict[str, Any] = dict(task)
            if include_details:
                try:
                    status_raw = _run_codex_cloud(["status", task_id])
                    if status_raw.strip():
                        payload["status_text"] = status_raw.strip()
                except RuntimeError:
                    pass
                try:
                    diff = _run_codex_cloud(["diff", task_id])
                    if diff.strip():
                        payload["diff"] = diff
                except RuntimeError:
                    pass
            occurred_at = (
                _first_task_value(
                    payload,
                    "updated_at",
                    "updatedAt",
                    "created_at",
                    "createdAt",
                )
                or now_iso()
            )
            project = (
                _first_task_value(
                    payload,
                    "repository",
                    "repo",
                    "project",
                    "environment_label",
                )
                or "codex-cloud"
            )
            envelope: dict[str, Any] = {
                "source": "codex-cloud",
                "device_id": config.device_id,
                "session_id": task_id,
                "event_name": "CodexCloudTask",
                "occurred_at": occurred_at,
                "cwd": "",
                "project": project.rsplit("/", 1)[-1].removesuffix(".git"),
                "repository": _first_task_value(payload, "repository", "repo"),
                "branch": _first_task_value(payload, "branch"),
                "transcript_path": "",
                "payload": payload,
                "received_at": now_iso(),
            }
            _, inserted = store.add_event(finalize_event(envelope, config))
            inserted_count += int(inserted)
    return inserted_count


def exec_codex_cloud(
    config: Config,
    query: str,
    *,
    environment: str,
    branch: str = "",
) -> str:
    """Start a Codex Cloud task and retain the submitted prompt locally."""
    arguments = ["exec", "--env", environment]
    if branch:
        arguments.extend(["--branch", branch])
    arguments.append(query)
    output = _run_codex_cloud(arguments)
    task_id = ""
    try:
        parsed = json.loads(output)
        if isinstance(parsed, Mapping):
            task_id = _first_task_value(parsed, "id", "task_id", "taskId")
    except json.JSONDecodeError:
        pass
    if not task_id:
        patterns = [
            r"https?://\S+/(?:tasks?|codex)/(?:tasks?/)?([A-Za-z0-9_-]{8,})",
            r"\btask(?:_id)?\s*[:=]\s*([A-Za-z0-9_-]{8,})",
            r"\b([0-9a-f]{8}-[0-9a-f-]{27,})\b",
        ]
        for pattern in patterns:
            match = re.search(pattern, output, re.IGNORECASE)
            if match:
                task_id = match.group(1).rstrip(".,:()[]")
                break
    # Each exec starts a new task, even with identical input in the same second.
    task_id = task_id or uuid4().hex
    payload = {
        "task_id": task_id,
        "prompt": query,
        "output": output,
        "environment_id": environment,
        "branch": branch,
        "status": "submitted",
    }
    envelope = {
        "source": "codex-cloud",
        "device_id": config.device_id,
        "session_id": task_id,
        "event_name": "CodexCloudTaskSubmitted",
        "occurred_at": now_iso(),
        "cwd": "",
        "project": "codex-cloud",
        "repository": "",
        "branch": branch,
        "transcript_path": "",
        "payload": payload,
        "received_at": now_iso(),
    }
    with EventStore(config.state_dir) as store:
        store.add_event(finalize_event(envelope, config))
    return redact_text(output) if config.redact else output
