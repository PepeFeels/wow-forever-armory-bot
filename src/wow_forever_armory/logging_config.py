"""Safe terminal logging configuration for the Discord application."""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import traceback
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

_HANDLER_MARKER = "_wow_forever_armory_console_handler"
_CONFIGURED = False
_LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}
_SAFE_METADATA_TYPES: dict[str, type | tuple[type, ...]] = {
    "status": int,
    "attempt": int,
    "guild_count": int,
    "guilds_recovered": int,
    "guilds_incomplete": int,
    "guilds_deferred": int,
    "member_rows_recovered": int,
    "member_rows_created": int,
    "member_rows_removed": int,
    "members_skipped": int,
    "member_rows_deferred": int,
    "member_state_load_failures": int,
    "panels_recovered": int,
    "persistent_views_registered": int,
    "slash_commands_synced": int,
    "character_count": int,
    "outcome": str,
    "operation": str,
    "reason": str,
    "exception_type": str,
}
_URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
_AUTH_RE = re.compile(r"(?im)(authorization\s*[:=]\s*)[^\r\n]+")
_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{5,}"
    r"\.[A-Za-z0-9_-]{20,}(?![A-Za-z0-9_-])"
)
_ARMORY_PATH_RE = re.compile(
    r"(?i)(?:[a-z]{2}-[a-z]{2}/)?forever/(?:eu|us|kr|tw)/armory/"
    r"character/[^\s\"'<>]+"
)


def _redact(value: str, secrets: Sequence[str]) -> str:
    """Redact URLs and credential-shaped values before emitting log text."""
    value = _AUTH_RE.sub(r"\1[REDACTED]", value)
    value = _TOKEN_RE.sub("[REDACTED_TOKEN]", value)
    value = _URL_RE.sub("[REDACTED_URL]", value)
    value = _ARMORY_PATH_RE.sub("[REDACTED_ARMORY_PATH]", value)
    for secret in secrets:
        if secret:
            value = value.replace(secret, "[REDACTED_SECRET]")
    return value


class UTCLogFormatter(logging.Formatter):
    """Emit concise UTC text or JSON while excluding unapproved metadata."""

    def __init__(self, *, json_output: bool = False, secrets: Sequence[str] = ()):
        super().__init__()
        self.json_output = json_output
        self.secrets = tuple(secret for secret in secrets if secret)

    @staticmethod
    def _timestamp(record: logging.LogRecord) -> str:
        return (
            datetime.fromtimestamp(record.created, UTC)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        )

    def _exception(self, record: logging.LogRecord) -> str | None:
        if record.exc_info is None:
            return None
        rendered = "".join(traceback.format_exception(*record.exc_info))
        return _redact(rendered.rstrip(), self.secrets)

    def format(self, record: logging.LogRecord) -> str:
        event = _redact(record.getMessage(), self.secrets)
        timestamp = self._timestamp(record)
        exception = self._exception(record)
        if self.json_output:
            payload: dict[str, Any] = {
                "timestamp": timestamp,
                "level": record.levelname,
                "logger": record.name,
                "event": event,
            }
            for key, expected_type in _SAFE_METADATA_TYPES.items():
                value = getattr(record, key, None)
                if isinstance(value, expected_type):
                    payload[key] = (
                        _redact(value, self.secrets)
                        if isinstance(value, str)
                        else value
                    )
            if exception is not None:
                payload["exception"] = exception
            return json.dumps(payload, ensure_ascii=False)

        line = f"{timestamp} {record.levelname} {record.name} {event}"
        return f"{line}\n{exception}" if exception is not None else line


def _log_level(environ: Mapping[str, str]) -> tuple[int, bool]:
    value = environ.get("ARMORY_LOG_LEVEL", "INFO").strip().upper()
    if value in _LEVELS:
        return _LEVELS[value], False
    return logging.INFO, True


def configure_logging(
    *,
    environ: Mapping[str, str] | None = None,
    secrets: Sequence[str] = (),
) -> tuple[str, str]:
    """Configure application and discord.py loggers without touching root."""
    global _CONFIGURED

    settings = os.environ if environ is None else environ
    level, invalid_level = _log_level(settings)
    output_format = settings.get("ARMORY_LOG_FORMAT", "text").strip().lower()
    invalid_format = output_format not in {"text", "json"}
    if invalid_format:
        output_format = "text"

    formatter = UTCLogFormatter(json_output=output_format == "json", secrets=secrets)
    for logger_name in ("wow_forever_armory", "discord"):
        logger = logging.getLogger(logger_name)
        tagged = [
            handler
            for handler in logger.handlers
            if getattr(handler, _HANDLER_MARKER, False)
        ]
        handler: logging.Handler | None = tagged[0] if tagged else None
        if handler is not None:
            for duplicate in tagged[1:]:
                logger.removeHandler(duplicate)
        else:
            handler = logging.StreamHandler(sys.stderr)
            setattr(handler, _HANDLER_MARKER, True)
            logger.addHandler(handler)

        logger.setLevel(level)
        logger.propagate = False
        if handler is not None:
            handler.setLevel(logging.NOTSET)
            handler.setFormatter(formatter)

    logger = logging.getLogger("wow_forever_armory")
    if not _CONFIGURED:
        logger.info(
            "logging_configured",
            extra={
                "outcome": output_format,
                "operation": logging.getLevelName(level),
            },
        )
        _CONFIGURED = True
    if invalid_level:
        logger.warning("invalid ARMORY_LOG_LEVEL; using INFO")
    if invalid_format:
        logger.warning("invalid ARMORY_LOG_FORMAT; using text")
    return logging.getLevelName(level), output_format
