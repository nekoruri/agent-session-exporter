"""Pull Claude Cloud hook events from a Cloudflare Worker inbox."""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from typing import Any

from . import __version__
from .core import (
    Config,
    EventStore,
    canonical_event_name,
    finalize_event,
    now_iso,
)

CLAUDE_CLOUD_EVENTS = {
    "UserPromptSubmit",
    "MessageDisplay",
    "Stop",
    "StopFailure",
    "SessionEnd",
}


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


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    """Let urllib raise HTTPError without forwarding inbox credentials."""

    def redirect_request(self, req, fp, code, msg, headers, newurl) -> None:
        return None


def _request_json(
    url: str,
    *,
    token: str = "",
    timeout: float = 10.0,
) -> dict[str, Any]:
    headers = {
        "Accept": "application/json",
        "User-Agent": f"agent-session-exporter/{__version__}",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers, method="GET")
    opener = urllib.request.build_opener(_RejectRedirects())
    try:
        with opener.open(request, timeout=timeout) as response:
            result = json.loads(response.read())
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Claude Cloud inbox returned HTTP {error.code}: {detail}"
        ) from error
    except urllib.error.URLError as error:
        raise RuntimeError(
            f"Claude Cloud inbox request failed: {error.reason}"
        ) from error
    if not isinstance(result, dict):
        raise TypeError("Claude Cloud inbox returned an unexpected response.")
    return result


def _remote_envelope(config: Config, value: Mapping[str, Any]) -> dict[str, Any]:
    required = {
        "source",
        "device_id",
        "session_id",
        "event_name",
        "occurred_at",
        "payload",
    }
    if not required.issubset(value):
        missing = ", ".join(sorted(required - set(value)))
        raise ValueError(f"Remote event is missing fields: {missing}")
    if str(value["source"]) != "claude-cloud":
        raise ValueError("Remote event source must be claude-cloud.")
    event_name = canonical_event_name(value["event_name"])
    if event_name not in CLAUDE_CLOUD_EVENTS:
        raise ValueError(f"Unsupported Claude Cloud event: {event_name}")
    payload = value["payload"]
    if not isinstance(payload, Mapping):
        raise TypeError("Remote event payload must be a JSON object.")
    cleaned_payload = dict(payload)
    cleaned_payload.pop("transcript_path", None)
    cleaned_payload.pop("transcriptPath", None)
    for event_key in ("hook_event_name", "event_name", "event", "type"):
        if canonical_event_name(cleaned_payload.get(event_key)) == event_name:
            cleaned_payload[event_key] = event_name
            break
    envelope: dict[str, Any] = {
        "source": "claude-cloud",
        "device_id": str(value["device_id"]),
        "session_id": str(value["session_id"]),
        "event_name": event_name,
        "occurred_at": str(value["occurred_at"]),
        "cwd": str(value.get("cwd") or ""),
        "project": str(value.get("project") or "unknown"),
        "repository": str(value.get("repository") or ""),
        "branch": str(value.get("branch") or ""),
        "transcript_path": "",
        "payload": cleaned_payload,
        "received_at": str(value.get("received_at") or now_iso()),
    }
    identity_key = value.get("identity_key", "")
    if not isinstance(identity_key, str) or (identity_key and not re.fullmatch(r"[0-9a-f]{64}", identity_key)):
        raise ValueError("Remote event has an invalid identity_key.")
    return finalize_event(envelope, config, identity_key=identity_key)


def pull_events(config: Config, *, limit: int = 500) -> tuple[int, int]:
    """Pull new Claude Cloud events into the local store."""
    remote = config.claude_cloud
    if not remote.url:
        raise ValueError("claude_cloud.url is not configured.")
    parsed_url = urllib.parse.urlparse(remote.url)
    if (
        parsed_url.scheme != "https"
        or not parsed_url.hostname
        or parsed_url.username
        or parsed_url.password
        or parsed_url.path not in {"", "/"}
        or parsed_url.params
        or parsed_url.query
        or parsed_url.fragment
    ):
        raise ValueError("claude_cloud.url must be an HTTPS origin.")
    token = os.environ.get(remote.token_env, "")
    if not token:
        raise ValueError(f"{remote.token_env} is not set.")
    limit = min(max(limit, 1), 500)
    cursor_key = f"remote-cursor:{remote.url}"
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
                f"{remote.url}/v1/events?{query}",
                token=token,
                timeout=max(remote.timeout_seconds, 0.1),
            )
            items = response.get("events")
            if not isinstance(items, list):
                raise TypeError("Claude Cloud inbox response does not contain events.")
            for item in items:
                if not isinstance(item, Mapping):
                    raise TypeError("Claude Cloud inbox returned an invalid event.")
                _, inserted = store.add_event(_remote_envelope(config, item))
                imported += int(inserted)
            try:
                next_cursor = _parse_cursor(response.get("next_after"))
            except (TypeError, ValueError) as error:
                raise RuntimeError(
                    "Claude Cloud inbox returned an invalid next_after cursor."
                ) from error
            if next_cursor < cursor:
                raise RuntimeError("Claude Cloud inbox cursor moved backwards.")
            if items and next_cursor == cursor:
                raise RuntimeError("Claude Cloud inbox cursor did not advance.")
            cursor = next_cursor
            store.set_metadata(cursor_key, str(cursor))
            if len(items) < limit:
                break
    return imported, cursor
