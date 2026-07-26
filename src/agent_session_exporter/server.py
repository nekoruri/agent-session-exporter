"""Small authenticated HTTP collector for cloud and desktop hook events."""

from __future__ import annotations

import hmac
import json
import os
import sqlite3
from collections.abc import Mapping
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from .core import (
    Config,
    EventStore,
    event_fingerprint,
    normalize_event,
    now_iso,
    redact_value,
)

MAX_BODY_BYTES = 8 * 1024 * 1024


def _remote_envelope(value: Mapping[str, Any], config: Config) -> dict[str, Any]:
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
        raise ValueError(f"Missing envelope fields: {missing}")
    payload = value.get("payload")
    if not isinstance(payload, Mapping):
        raise TypeError("Envelope payload must be a JSON object.")
    cleaned_payload = redact_value(dict(payload)) if config.redact else dict(payload)
    envelope: dict[str, Any] = {
        "source": str(value["source"]),
        "device_id": str(value["device_id"]),
        "session_id": str(value["session_id"]),
        "event_name": str(value["event_name"]),
        "occurred_at": str(value["occurred_at"]),
        "cwd": str(value.get("cwd") or ""),
        "project": str(value.get("project") or "unknown"),
        "repository": str(value.get("repository") or ""),
        "branch": str(value.get("branch") or ""),
        "transcript_path": "",
        "payload": cleaned_payload,
        "received_at": str(value.get("received_at") or now_iso()),
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


def _handler(config: Config, token: str) -> type[BaseHTTPRequestHandler]:
    class CollectorHandler(BaseHTTPRequestHandler):
        server_version = "AgentSessionExporter/0.1"

        def log_message(self, format_string: str, *args: object) -> None:
            print(
                f"{self.address_string()} - {format_string % args}",
                flush=True,
            )

        def _authorized(self) -> bool:
            if not token:
                return True
            supplied = self.headers.get("Authorization", "")
            expected = f"Bearer {token}"
            return hmac.compare_digest(supplied, expected)

        def _json_response(self, status: HTTPStatus, body: object) -> None:
            encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(encoded)

        def _read_json(self) -> dict[str, Any]:
            content_length = int(self.headers.get("Content-Length", "0"))
            if content_length <= 0:
                raise ValueError("Expected a JSON request body.")
            if content_length > MAX_BODY_BYTES:
                raise OverflowError("Request body exceeds 8 MiB.")
            raw = self.rfile.read(content_length)
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise TypeError("Expected a JSON object.")
            return value

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/health":
                self._json_response(HTTPStatus.OK, {"status": "ok"})
                return
            if not self._authorized():
                self._json_response(
                    HTTPStatus.UNAUTHORIZED,
                    {"error": "unauthorized"},
                )
                return
            if parsed.path != "/v1/events":
                self._json_response(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            query = parse_qs(parsed.query)
            try:
                after = int((query.get("after") or ["0"])[0])
                limit = int((query.get("limit") or ["500"])[0])
            except ValueError:
                self._json_response(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "after and limit must be integers"},
                )
                return
            with EventStore(config.state_dir) as store:
                events = store.list_events(after=max(after, 0), limit=limit)
            items = [{"id": event.id, **event.to_envelope()} for event in events]
            next_after = items[-1]["id"] if items else max(after, 0)
            self._json_response(
                HTTPStatus.OK,
                {"events": items, "next_after": next_after},
            )

        def do_POST(self) -> None:
            if not self._authorized():
                self._json_response(
                    HTTPStatus.UNAUTHORIZED,
                    {"error": "unauthorized"},
                )
                return
            parsed = urlparse(self.path)
            try:
                body = self._read_json()
                if parsed.path.startswith("/v1/hooks/"):
                    source = unquote(parsed.path.removeprefix("/v1/hooks/"))
                    if not source or "/" in source:
                        raise ValueError("Invalid hook source.")
                    envelope = normalize_event(
                        body,
                        source,
                        config,
                        inspect_cwd=False,
                    )
                elif parsed.path == "/v1/events":
                    envelope = _remote_envelope(body, config)
                else:
                    self._json_response(
                        HTTPStatus.NOT_FOUND,
                        {"error": "not found"},
                    )
                    return
                try:
                    with EventStore(config.state_dir) as store:
                        store.add_event(envelope)
                except (OSError, sqlite3.Error) as error:
                    self.log_error("storage failure: %s", error)
                    self._json_response(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        {"error": "storage unavailable"},
                    )
                    return
                self._json_response(
                    HTTPStatus.ACCEPTED,
                    {},
                )
            except OverflowError as error:
                self._json_response(
                    HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                    {"error": str(error)},
                )
            except (ValueError, TypeError, json.JSONDecodeError) as error:
                self._json_response(
                    HTTPStatus.BAD_REQUEST,
                    {"error": str(error)},
                )

    return CollectorHandler


def serve(config: Config) -> None:
    """Run the configured collector until interrupted."""
    token = os.environ.get(config.server.token_env, "")
    loopback = config.server.listen in {"127.0.0.1", "::1", "localhost"}
    if not loopback and not token:
        raise ValueError(
            f"{config.server.token_env} must be set when listening beyond loopback."
        )
    server = ThreadingHTTPServer(
        (config.server.listen, config.server.port),
        _handler(config, token),
    )
    print(
        f"Collector listening on http://{config.server.listen}:{config.server.port}",
        flush=True,
    )
    try:
        server.serve_forever()
    finally:
        server.server_close()
