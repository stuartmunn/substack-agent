"""Tests for the post-deploy smoke test message (SA-39 acceptance criterion 2)."""

import asyncio
import logging
from typing import Any, ClassVar, Self

import pytest

from humblebrag import smoke
from humblebrag.config import load_config
from humblebrag.smoke import EXIT_FAILED, EXIT_OK, SMOKE_MESSAGE, run_smoke_test

ENV = {
    "TELEGRAM_BOT_TOKEN": "123456:TESTTOKEN",
    "TELEGRAM_CHAT_ID": "12345",
    "ANTHROPIC_API_KEY": "sk-ant-key",
    "CLAUDE_MODEL": "claude-test-model",
    "SUBSTACK_PUBLICATION_URL": "https://example.substack.com",
    "SUBSTACK_COOKIE": "cookie",
}


class FakeBot:
    """A Telegram bot that records messages instead of sending them."""

    sent: ClassVar[list[dict[str, Any]]] = []
    tokens: ClassVar[list[str]] = []
    error: Exception | None = None
    hang = False

    def __init__(self, token: str) -> None:
        """Record the token it was built with."""
        FakeBot.tokens.append(token)

    async def __aenter__(self) -> Self:
        """Enter the async context."""
        return self

    async def __aexit__(self, *_exc: object) -> None:
        """Leave the async context."""

    async def send_message(self, **kwargs: Any) -> None:
        """Record the message, hang, or fail as configured."""
        if FakeBot.hang:
            await asyncio.sleep(3600)
        if FakeBot.error is not None:
            raise FakeBot.error
        FakeBot.sent.append(kwargs)


@pytest.fixture(autouse=True)
def fake_bot(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace Telegram's Bot with the recording fake, reset for each test."""
    FakeBot.sent, FakeBot.tokens = [], []
    FakeBot.error, FakeBot.hang = None, False
    monkeypatch.setattr(smoke, "Bot", FakeBot)


def test_the_message_is_exactly_the_one_in_the_story() -> None:
    """AC2: the wording is "Sir Humblebrag reporting for duty"."""
    assert SMOKE_MESSAGE == "Sir Humblebrag reporting for duty"


def test_the_smoke_test_sends_the_message_to_the_configured_chat() -> None:
    """AC2: one message, to TELEGRAM_CHAT_ID, using the configured bot token."""
    assert run_smoke_test(load_config(ENV)) == EXIT_OK
    assert FakeBot.sent == [{"chat_id": 12345, "text": SMOKE_MESSAGE}]
    assert FakeBot.tokens == ["123456:TESTTOKEN"]


def test_a_send_failure_exits_non_zero_and_is_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AC2: a Telegram error fails the deploy instead of passing silently."""
    FakeBot.error = RuntimeError("network down")
    with caplog.at_level(logging.INFO):
        assert run_smoke_test(load_config(ENV)) == EXIT_FAILED
    assert "Smoke test failed" in caplog.text
    assert "RuntimeError: network down" in caplog.text


def test_a_hung_send_times_out_and_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """ERR-02: the send has a timeout, so a hung connection cannot hang the deploy."""
    FakeBot.hang = True
    monkeypatch.setattr(smoke, "SEND_TIMEOUT_SECONDS", 0.05)
    assert run_smoke_test(load_config(ENV)) == EXIT_FAILED


def test_main_exits_with_the_smoke_result(monkeypatch: pytest.MonkeyPatch) -> None:
    """The entrypoint turns the result into the process exit code."""
    monkeypatch.setattr(smoke, "load_config_or_exit", lambda: load_config(ENV))
    with pytest.raises(SystemExit) as passed:
        smoke.main()
    assert passed.value.code == EXIT_OK
    FakeBot.error = RuntimeError("nope")
    with pytest.raises(SystemExit) as failed:
        smoke.main()
    assert failed.value.code == EXIT_FAILED
