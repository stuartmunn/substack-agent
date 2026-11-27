"""Tests for the post-deploy smoke test: the Telegram message (SA-39) and the
one real Claude call per flow (SA-59 AC4)."""

import asyncio
import logging
from typing import Any, ClassVar, Self

import pytest

from humblebrag import smoke
from humblebrag.claude_client import ClaudeAPIError, Flow
from humblebrag.config import Config, load_config
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


class FakeClaude:
    """A Claude client that records calls and fails the flows listed in ``failing``."""

    calls: ClassVar[list[Flow]] = []
    failing: ClassVar[dict[Flow, Exception]] = {}

    def __init__(self, _config: Config) -> None:
        """Accept the config, as the real client does."""

    async def complete(self, system: str, prompt: str, flow: Flow) -> str:
        """Record the call, then fail or answer as configured."""
        FakeClaude.calls.append(flow)
        if flow in FakeClaude.failing:
            raise FakeClaude.failing[flow]
        return "OK"


@pytest.fixture(autouse=True)
def fakes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace Telegram's Bot and the Claude client with fakes, reset per test."""
    FakeBot.sent, FakeBot.tokens = [], []
    FakeBot.error, FakeBot.hang = None, False
    FakeClaude.calls, FakeClaude.failing = [], {}
    monkeypatch.setattr(smoke, "Bot", FakeBot)
    monkeypatch.setattr(smoke, "ClaudeClient", FakeClaude)


def test_the_message_is_exactly_the_one_in_the_story() -> None:
    """AC2: the wording is "Sir Humblebrag reporting for duty"."""
    assert SMOKE_MESSAGE == "Sir Humblebrag reporting for duty"


def test_the_smoke_test_sends_the_message_to_the_configured_chat() -> None:
    """AC2: one message, to TELEGRAM_CHAT_ID, using the configured bot token."""
    assert run_smoke_test(load_config(ENV)) == EXIT_OK
    assert FakeBot.sent[0] == {"chat_id": 12345, "text": SMOKE_MESSAGE}
    assert {message["chat_id"] for message in FakeBot.sent} == {12345}
    assert set(FakeBot.tokens) == {"123456:TESTTOKEN"}


def test_every_flow_makes_one_real_call_and_is_reported_as_pass() -> None:
    """SA-59 AC4: post, reply, guard and seed each get one call and a pass line."""
    assert run_smoke_test(load_config(ENV)) == EXIT_OK
    assert sorted(FakeClaude.calls) == sorted(Flow)
    report = FakeBot.sent[-1]["text"]
    for flow in ("post", "reply", "guard", "seed"):
        assert f"✅ {flow}: pass" in report
    assert "FAIL" not in report


def test_a_failing_flow_is_reported_with_its_error_and_fails_the_smoke_test() -> None:
    """SA-59 AC4: the failure carries the API's error type and message, the other
    flows still run and pass, and the exit code is non-zero."""
    FakeClaude.failing = {
        Flow.REPLY: ClaudeAPIError(
            "Claude API returned HTTP 400 (request req_1, flow reply, "
            "type invalid_request_error): thinking not allowed",
            status_code=400,
        )
    }
    assert run_smoke_test(load_config(ENV)) == EXIT_FAILED
    assert sorted(FakeClaude.calls) == sorted(Flow)
    report = FakeBot.sent[-1]["text"]
    assert "❌ reply: FAIL" in report
    assert "invalid_request_error" in report
    assert "thinking not allowed" in report
    assert "✅ post: pass" in report
    assert "✅ guard: pass" in report
    assert "✅ seed: pass" in report


def test_the_report_never_contains_the_api_key() -> None:
    """Hard rule: secrets are redacted from what goes to Telegram."""
    smoke.redactor.add(["sk-ant-key"])
    FakeClaude.failing = {Flow.POST: RuntimeError("bad key sk-ant-key")}
    try:
        run_smoke_test(load_config(ENV))
    finally:
        smoke.redactor.clear()
    assert "sk-ant-key" not in FakeBot.sent[-1]["text"]


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


def test_a_timed_out_flow_is_reported_with_a_readable_reason() -> None:
    """A bare TimeoutError has no message; the report must still say what happened."""
    FakeClaude.failing = {Flow.SEED: TimeoutError()}
    assert run_smoke_test(load_config(ENV)) == EXIT_FAILED
    assert "no answer within" in FakeBot.sent[-1]["text"]
