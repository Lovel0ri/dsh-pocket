"""Structured JSON logging with secret redaction (contract §1.5).

Nothing in this module logs secrets by itself; :func:`register_secret` lets the
auth layer add known secrets so that even an accidental log call gets masked
before the record is written by our handlers.
"""

from __future__ import annotations

import json
import logging
import sys
import threading
import time
from typing import Any

_SECRETS: set[str] = set()
_LOCK = threading.Lock()

_MASK = "***"


def register_secret(value: str | None) -> None:
    """Remember a secret string so it is masked in every formatted record."""
    if not value or len(value) < 4:
        return
    with _LOCK:
        _SECRETS.add(value)


def clear_secrets() -> None:  # pragma: no cover - test helper
    with _LOCK:
        _SECRETS.clear()


def redact(text: str) -> str:
    if not text:
        return text
    with _LOCK:
        secrets = tuple(_SECRETS)
    for secret in secrets:
        if secret in text:
            text = text.replace(secret, _MASK)
    return text


class JsonFormatter(logging.Formatter):
    """One JSON object per line; never includes request bodies or headers."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(record.created))
            + ".%03dZ" % int(record.msecs),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key in ("event", "method", "path", "status", "durationMs", "client", "mode"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            # Exception messages may contain paths but never secrets; keep type only.
            payload["exc"] = record.exc_info[0].__name__ if record.exc_info[0] else "Exception"
        return redact(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def setup_logging(level: str | int = "INFO") -> None:
    """Install a single JSON handler on the root logger.

    Idempotent: repeated calls (tests, uvicorn reload) replace our handler
    instead of stacking duplicates.
    """
    if isinstance(level, str):
        level = getattr(logging, level.upper(), logging.INFO)
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "_dsh_pocket", False):
            root.removeHandler(handler)
    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(JsonFormatter())
    handler._dsh_pocket = True  # type: ignore[attr-defined]
    root.addHandler(handler)
    root.setLevel(level)


def log_event(logger: logging.Logger, event: str, level: int = logging.INFO, **fields: Any) -> None:
    """Small helper: structured event + human message, secrets already masked."""
    logger.log(level, event, extra={"event": event, **fields})
