"""Structured JSON logging to stdout with secret redaction."""

import json
import logging
import sys
from datetime import UTC, datetime

REDACTED = "[REDACTED]"
HANDLER_MARKER = "_humblebrag_handler"


class SecretRedactor:
    """Replaces registered secret values in text."""

    def __init__(self) -> None:
        """Start with no secrets registered."""
        self._secrets: set[str] = set()

    def add(self, values: list[str]) -> None:
        """Register secret values; empty strings are ignored."""
        self._secrets.update(value for value in values if value)

    def clear(self) -> None:
        """Forget all registered secrets."""
        self._secrets.clear()

    def redact(self, text: str) -> str:
        """Return ``text`` with every registered secret replaced."""
        for secret in sorted(self._secrets, key=len, reverse=True):
            text = text.replace(secret, REDACTED)
        return text


redactor = SecretRedactor()


class JsonFormatter(logging.Formatter):
    """Formats records as one JSON object per line: timestamp, level, event."""

    def format(self, record: logging.LogRecord) -> str:
        """Render ``record`` as JSON with secrets redacted."""
        entry = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": redactor.redact(record.getMessage()),
        }
        if record.exc_info:
            entry["exception"] = redactor.redact(self.formatException(record.exc_info))
        return json.dumps(entry)


def configure_logging(level: int = logging.INFO) -> None:
    """Send JSON logs to stdout, replacing any handler this function added."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, HANDLER_MARKER, False):
            root.removeHandler(handler)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    setattr(handler, HANDLER_MARKER, True)
    root.addHandler(handler)
    root.setLevel(level)


def register_secrets(values: list[str]) -> None:
    """Make the logger redact these values from every later record."""
    redactor.add(values)
