"""Tests for the alert channel (SA-18 acceptance criteria)."""

import asyncio
import logging
from collections.abc import Iterator
from typing import ClassVar, Self

import pytest

from humblebrag import alerts
from humblebrag.alerts import (
    ALERT_PREFIX,
    MAX_ALERT_LENGTH,
    alert,
    configure_alerts,
    format_alert,
)
from humblebrag.config import load_config
from humblebrag.logging_setup import JsonFormatter, redactor

SECRET = "tok-SECRET-123"


@pytest.fixture(autouse=True)
def _clean_state() -> Iterator[None]:
    """Reset the sender and registered secrets around every test."""
    configure_alerts(None)
    redactor.clear()
    yield
    configure_alerts(None)
    redactor.clear()


class Recorder:
    """A fake Telegram sender that records the texts it was given."""

    def __init__(self) -> None:
        """Start with nothing sent."""
        self.sent: list[str] = []

    async def __call__(self, text: str) -> None:
        """Record ``text``."""
        self.sent.append(text)


async def test_alert_sends_one_message_via_the_configured_sender() -> None:
    """AC1: alert(event, detail) delivers through the single configured sender."""
    recorder = Recorder()
    configure_alerts(recorder)
    assert await alert("publish", "Substack returned 500") is True
    assert len(recorder.sent) == 1


async def test_alert_is_prefixed_and_names_the_failed_step() -> None:
    """AC2: text starts with the warning emoji and names the step."""
    recorder = Recorder()
    configure_alerts(recorder)
    await alert("generate-post", "Anthropic timed out")
    text = recorder.sent[0]
    assert text.startswith(ALERT_PREFIX)
    assert "generate-post" in text
    assert "Anthropic timed out" in text


async def test_alert_carries_no_secrets() -> None:
    """AC2: registered secrets are redacted from the alert text."""
    redactor.add([SECRET])
    recorder = Recorder()
    configure_alerts(recorder)
    await alert("telegram", f"rejected token {SECRET}")
    assert SECRET not in recorder.sent[0]


def test_format_alert_is_capped_at_the_telegram_limit() -> None:
    """Over-long detail is truncated so Telegram does not reject the alert."""
    text = format_alert("step", "x" * (MAX_ALERT_LENGTH * 2))
    assert len(text) == MAX_ALERT_LENGTH
    assert text.endswith(alerts.TRUNCATION_MARKER)


async def test_telegram_failure_is_logged_at_error_and_does_not_raise(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AC3: when sending fails the alert is logged at ERROR, not raised."""

    async def broken(_text: str) -> None:
        raise ConnectionError("telegram is down")

    configure_alerts(broken)
    with caplog.at_level(logging.ERROR):
        delivered = await alert("publish", "Substack returned 500")
    assert delivered is False
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert errors
    assert "Substack returned 500" in errors[0].getMessage()


async def test_hung_telegram_times_out_and_is_logged_at_error(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """ERR-02: a sender that hangs is abandoned after the timeout."""

    async def hangs(_text: str) -> None:
        await asyncio.sleep(3600)

    monkeypatch.setattr(alerts, "ALERT_TIMEOUT_SECONDS", 0.01)
    configure_alerts(hangs)
    with caplog.at_level(logging.ERROR):
        assert await alert("publish", "stuck") is False
    assert any(r.levelno == logging.ERROR for r in caplog.records)


async def test_no_sender_configured_is_logged_at_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AC3: with no Telegram sender the alert still appears at ERROR."""
    with caplog.at_level(logging.ERROR):
        assert await alert("startup", "bot never started") is False
    assert any(
        r.levelno == logging.ERROR and "bot never started" in r.getMessage()
        for r in caplog.records
    )


async def test_error_log_of_undelivered_alert_has_no_secrets(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AC2/AC3: the ERROR log of a failed alert is redacted too."""
    redactor.add([SECRET])
    with caplog.at_level(logging.ERROR):
        await alert("telegram", f"token {SECRET} rejected")
    formatted = JsonFormatter().format(caplog.records[-1])
    assert SECRET not in formatted


def test_telegram_sender_posts_plain_text_to_the_chat() -> None:
    """The Telegram sender sends to the configured chat with no parse mode."""
    calls: list[dict[str, object]] = []

    class FakeBot:
        async def send_message(self, **kwargs: object) -> None:
            calls.append(kwargs)

    send = alerts.telegram_sender(FakeBot(), 42)  # type: ignore[arg-type]
    asyncio.run(send("hello"))
    assert calls == [{"chat_id": 42, "text": "hello"}]


class _FakeBot:
    """Stand-in for telegram.Bot used as an async context manager."""

    instances: ClassVar[list["_FakeBot"]] = []

    def __init__(self, token: str) -> None:
        """Record the token and the instance."""
        self.token = token
        self.sent: list[dict[str, object]] = []
        _FakeBot.instances.append(self)

    async def __aenter__(self) -> Self:
        """Enter the context."""
        return self

    async def __aexit__(self, *_exc: object) -> None:
        """Leave the context."""

    async def send_message(self, **kwargs: object) -> None:
        """Record a sent message."""
        self.sent.append(kwargs)


_ENV = {
    "TELEGRAM_BOT_TOKEN": "123456:TESTTOKEN",
    "TELEGRAM_CHAT_ID": "42",
    "ANTHROPIC_API_KEY": "key",
    "CLAUDE_MODEL": "m",
    "SUBSTACK_PUBLICATION_URL": "https://example.substack.com",
    "SUBSTACK_COOKIE": "cookie",
}


async def test_standalone_alert_uses_a_fresh_bot_and_unsets_the_sender(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Alerts after the main bot has stopped go out via a fresh bot."""
    _FakeBot.instances.clear()
    monkeypatch.setattr(alerts, "Bot", _FakeBot)
    delivered = await alerts.send_standalone_alert(
        load_config(_ENV), "agent", "crashed"
    )
    assert delivered is True
    assert _FakeBot.instances[0].token == "123456:TESTTOKEN"
    assert _FakeBot.instances[0].sent[0]["chat_id"] == 42
    assert alerts._sender is None


async def test_standalone_alert_failure_is_logged_at_error_not_raised(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """AC3: if even the fresh bot cannot start, the alert is logged at ERROR."""

    class BrokenBot(_FakeBot):
        async def __aenter__(self) -> Self:
            raise ConnectionError("telegram is down")

    monkeypatch.setattr(alerts, "Bot", BrokenBot)
    with caplog.at_level(logging.ERROR):
        delivered = await alerts.send_standalone_alert(
            load_config(_ENV), "agent", "crashed"
        )
    assert delivered is False
    assert any(r.levelno == logging.ERROR for r in caplog.records)
