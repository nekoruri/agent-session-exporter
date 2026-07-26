"""Remote collector transport and Codex Cloud metadata ingestion."""

from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from typing import Any

from .core import Config, EventStore, event_fingerprint, now_iso


def _parse_cursor(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise TypeError("Cursor must be an integer.")
    try:
        cursor = int(value)
    except ValueError as error:
        raise ValueError("Cursor must be an integer.") from error
    if cursor < 0:
        raise ValueError("Cursor must not be negative.")
    return cursor


def _request_json(
    url: str,
    *,
    method: str = "GET",
    token: str = "",
    payload: Mapping[str, Any] | None = None,
    timeout: float = 10.0,
) -> dict[str, Any]:
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        url,
        data=data,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result = json.loads(response.read())
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Collector returned HTTP {error.code}: {detail}") from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"Collector request failed: {error.reason}") from error
    if not isinstance(result, dict):
        raise TypeError("Collector returned an unexpected response.")
    return result


def push_event(config: Config, envelope: Mapping[str, Any]) -> bool:
    """Push an already normalized event when a collector is configured."""
    if not config.collector.url:
        return False
    token = os.environ.get(config.collector.token_env, "")
    _request_json(
        f"{config.collector.url}/v1/events",
        method="POST",
        token=token,
        payload=envelope,
        timeout=config.collector.timeout_seconds,
    )
    return True


def pull_events(config: Config, *, limit: int = 500) -> tuple[int, int]:
    """Pull new remote events into the local store."""
    if not config.collector.url:
        raise ValueError("collector.url is not configured.")
    token = os.environ.get(config.collector.token_env, "")
    limit = min(max(limit, 1), 5000)
    cursor_key = f"remote-cursor:{config.collector.url}"
    imported = 0
    cursor = 0
    with EventStore(config.state_dir) as store:
        try:
            cursor = _parse_cursor(store.get_metadata(cursor_key, "0"))
        except (TypeError, ValueError):
            cursor = 0
        while True:
            query = urllib.parse.urlencode({"after": cursor, "limit": limit})
            response = _request_json(
                f"{config.collector.url}/v1/events?{query}",
                token=token,
                timeout=max(config.collector.timeout_seconds, 10.0),
            )
            items = response.get("events")
            if not isinstance(items, list):
                raise TypeError("Collector response does not contain events.")
            for item in items:
                if not isinstance(item, dict):
                    continue
                item.pop("id", None)
                _, inserted = store.add_event(item)
                imported += int(inserted)
            try:
                next_cursor = _parse_cursor(response.get("next_after"))
            except (TypeError, ValueError) as error:
                raise RuntimeError(
                    "Collector returned an invalid next_after cursor."
                ) from error
            if next_cursor < cursor:
                raise RuntimeError("Collector cursor moved backwards.")
            if items and next_cursor == cursor:
                raise RuntimeError("Collector cursor did not advance.")
            cursor = next_cursor
            store.set_metadata(cursor_key, str(cursor))
            if len(items) < limit:
                break
    return imported, cursor


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
            _, inserted = store.add_event(envelope)
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
    task_id = (
        task_id
        or event_fingerprint({"query": query, "output": output, "time": now_iso()})[:24]
    )
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
    envelope["fingerprint"] = event_fingerprint(envelope)
    with EventStore(config.state_dir) as store:
        store.add_event(envelope)
    return output
