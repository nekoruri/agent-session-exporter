"""Offline credential detection backed by detect-secrets' maintained rules."""

from __future__ import annotations

import hashlib
import json
import re
from functools import lru_cache
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from detect_secrets.plugins.base import BasePlugin, RegexBasedDetector
from detect_secrets.plugins.high_entropy_strings import (
    Base64HighEntropyString,
    HighEntropyStringsPlugin,
)
from detect_secrets.plugins.keyword import KeywordDetector
from detect_secrets.plugins.private_key import PrivateKeyDetector
from detect_secrets.settings import default_settings, get_plugins

REDACTED = "[REDACTED]"
IDENTITY_KEYS = {"id", "session_id", "sessionId", "task_id", "taskId", "device_id", "deviceId", "message_id", "messageId"}
# Field names and protocol syntax are application policy, not provider token formats.
SECRET_KEY_RE = re.compile(
    r"(?:^|[_-])(?:api[_-]?key|access[_-]?token|client[_-]?secret|"
    r"secret|token|password|passwd|authorization|cookie)(?:$|[_-])",
    re.IGNORECASE,
)
AUTH_RE = re.compile(r"\b(?:Bearer|Basic)\s+[^\s\"'`<>]+", re.IGNORECASE)
URL_RE = re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.-]*://[^\s\"`<>]+")


@lru_cache(maxsize=1)
def _detectors() -> tuple[BasePlugin, ...]:
    with default_settings() as settings:
        # Public IP addresses are metadata, not credentials.
        settings.disable_plugins("IPPublicDetector")
        return tuple(get_plugins())


def _secret_key(key: str) -> bool:
    normalized = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", key)
    return bool(SECRET_KEY_RE.search(normalized)) or bool(
        list(KeywordDetector().analyze_string(f'{json.dumps(key)}: "credential"'))
    )


def _clean_url(match: re.Match[str]) -> str:
    value = match.group()
    try:
        parts = urlsplit(value)
        netloc = parts.netloc.rsplit("@", 1)[-1]
        query = parse_qsl(parts.query, keep_blank_values=True)
        if any(_secret_key(key) for key, _ in query):
            query_text = urlencode(
                [(key, REDACTED if _secret_key(key) else val) for key, val in query]
            )
        else:
            query_text = parts.query
        if netloc == parts.netloc and query_text == parts.query:
            return value
        return urlunsplit(parts._replace(netloc=netloc, query=query_text))
    except ValueError:
        return REDACTED


def redact_text(value: str) -> str:
    """Mask detected credentials without verification requests or user allowlists."""
    result = AUTH_RE.sub(REDACTED, URL_RE.sub(_clean_url, value))
    for plugin in _detectors():
        if isinstance(plugin, PrivateKeyDetector) and any(plugin.analyze_string(result)):
            # The library reports a key header; removing only that leaves the key usable.
            return REDACTED
    while True:
        secrets: set[str] = set()
        for plugin in _detectors():
            if isinstance(plugin, Base64HighEntropyString):
                # Chat text also contains bare tokens; use the library's unquoted mode.
                with plugin.non_quoted_string_regex(is_exact_match=False):
                    secrets.update(
                        secret for secret in plugin.analyze_string(result)
                        if plugin.calculate_shannon_entropy(secret) > plugin.entropy_limit
                    )
            elif isinstance(plugin, RegexBasedDetector):
                # Some detectors return only a capture group (e.g. a token prefix).
                # Use the full library match so no credential suffix is left behind.
                for pattern in plugin.denylist:
                    secrets.update(match.group() for match in pattern.finditer(result))
            else:
                for line in result.splitlines():
                    for secret in plugin.analyze_string(line):
                        if isinstance(plugin, HighEntropyStringsPlugin) and (
                            plugin.calculate_shannon_entropy(secret) <= plugin.entropy_limit
                        ):
                            continue
                        secrets.add(secret)
        secrets.discard("")
        secrets.discard(REDACTED)
        if not secrets:
            return result
        pattern = "|".join(
            re.escape(secret) for secret in sorted(secrets, key=len, reverse=True)
        )
        cleaned = re.sub(pattern, lambda _match: REDACTED, result)
        if cleaned == result:
            return result
        result = cleaned


def canonical_identity(value: str) -> str:
    """Hash a raw ID; a raw string can itself look like a generated pseudonym."""
    return "redacted-" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def session_identity(device_id: str, session_id: str) -> str:
    """Persist the identity before redaction, independently of its display strings."""
    raw = json.dumps([device_id, session_id], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def redact_value(value: Any, key: str = "") -> Any:
    """Mask credential fields in full and scan text while preserving JSON types."""
    if key and _secret_key(key):
        return REDACTED
    if isinstance(value, str):
        masked = redact_text(value)
        if key in IDENTITY_KEYS and masked != value:
            # A shared marker would merge unrelated sessions/devices during storage or rendering.
            return canonical_identity(value)
        return masked
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, dict):
        return {str(k): redact_value(v, str(k)) for k, v in value.items()}
    return value
