"""Tests for structured logging (SA-16 AC1)."""

import json
import logging
from collections.abc import Iterator
from datetime import datetime

import pytest

from humblebrag.logging_setup import (
    REDACTED,
    configure_logging,
    redactor,
    register_secrets,
)

SECRET = "tok-SECRET-123"


@pytest.fixture(autouse=True)
def _clean_redactor() -> Iterator[None]:
    """Keep registered secrets from leaking between tests."""
    redactor.clear()
    yield
    redactor.clear()


def _last_line(capsys: pytest.CaptureFixture[str]) -> dict[str, str]:
    """Parse the last stdout line as JSON."""
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def test_logs_go_to_stdout_as_json_with_timestamp_level_event(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC1: each log line is JSON on stdout with timestamp, level and event."""
    configure_logging()
    logging.getLogger("humblebrag.test").warning("hello %s", "world")
    captured = capsys.readouterr()
    entry = json.loads(captured.out.strip().splitlines()[-1])
    assert captured.err == ""
    assert entry["level"] == "WARNING"
    assert entry["event"] == "hello world"
    datetime.fromisoformat(entry["timestamp"])


def test_registered_secrets_are_redacted_from_event(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC1: a registered secret never appears in a log line."""
    configure_logging()
    register_secrets([SECRET])
    logging.getLogger("humblebrag.test").error("auth failed with %s", SECRET)
    output = capsys.readouterr().out
    assert SECRET not in output
    assert REDACTED in output


def test_secrets_are_redacted_from_exception_text(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC1: secrets inside exception messages are redacted too."""
    configure_logging()
    register_secrets([SECRET])
    try:
        raise RuntimeError(f"bad token {SECRET}")
    except RuntimeError:
        logging.getLogger("humblebrag.test").exception("call failed")
    entry = _last_line(capsys)
    assert SECRET not in json.dumps(entry)
    assert REDACTED in entry["exception"]


def test_empty_secret_does_not_mangle_output(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Registering an empty string is ignored rather than redacting everything."""
    configure_logging()
    register_secrets([""])
    logging.getLogger("humblebrag.test").info("plain message")
    assert _last_line(capsys)["event"] == "plain message"


def test_configure_logging_twice_does_not_duplicate_lines(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Calling configure_logging again replaces its handler, not adds one."""
    configure_logging()
    configure_logging()
    logging.getLogger("humblebrag.test").info("once")
    assert len(capsys.readouterr().out.strip().splitlines()) == 1
