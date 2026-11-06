"""Logging configuration: Rich text for people, JSON lines for machines, redaction for both."""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from enum import StrEnum
from typing import IO, Any

from rich.console import Console
from rich.logging import RichHandler

from dvf.security.redaction import Redactor, global_redactor

LOGGER_NAME = "dvf"
CONTEXT_FIELDS = ("run_id", "suite", "table", "check_id", "partition")
"""``extra=`` keys copied into JSON log records when present."""

_HANDLER_MARKER = "_dvf_handler"


class LogFormat(StrEnum):
    TEXT = "text"
    JSON = "json"


class RedactingFilter(logging.Filter):
    """Masks registered secret values in every log message."""

    def __init__(self, redactor: Redactor | None = None) -> None:
        super().__init__()
        self._redactor = redactor or global_redactor()

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self._redactor.redact_text(record.getMessage())
        record.args = None
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line, with run and check context when provided."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        for name in CONTEXT_FIELDS:
            value = getattr(record, name, None)
            if value is not None:
                payload[name] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging(
    level: str = "warning",
    fmt: LogFormat | str = LogFormat.TEXT,
    stream: IO[str] | None = None,
    redactor: Redactor | None = None,
) -> logging.Logger:
    """Configure the ``dvf`` logger. Safe to call repeatedly; earlier dvf handlers are replaced."""
    logger = logging.getLogger(LOGGER_NAME)
    for existing in [h for h in logger.handlers if getattr(h, _HANDLER_MARKER, False)]:
        logger.removeHandler(existing)
    handler: logging.Handler
    if LogFormat(fmt) is LogFormat.JSON:
        handler = logging.StreamHandler(stream or sys.stderr)
        handler.setFormatter(JsonFormatter())
    else:
        console = Console(file=stream or sys.stderr, soft_wrap=True)
        handler = RichHandler(console=console, show_path=False, rich_tracebacks=False)
    setattr(handler, _HANDLER_MARKER, True)
    handler.addFilter(RedactingFilter(redactor))
    logger.addHandler(handler)
    logger.setLevel(level.upper())
    logger.propagate = False
    return logger


def get_logger(name: str) -> logging.Logger:
    """A child of the ``dvf`` logger, for example ``get_logger(__name__)``."""
    if name == LOGGER_NAME or name.startswith(f"{LOGGER_NAME}."):
        return logging.getLogger(name)
    return logging.getLogger(f"{LOGGER_NAME}.{name}")
