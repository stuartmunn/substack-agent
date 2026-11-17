"""Tests for the container entrypoint."""

import asyncio
import json
import logging
import os
import signal

import pytest

from humblebrag import __main__ as entrypoint
from humblebrag.config import Config, load_config
from humblebrag.logging_setup import redactor

VALUES = {
    "TELEGRAM_BOT_TOKEN": "tok-SECRET",
    "TELEGRAM_CHAT_ID": "1",
    "ANTHROPIC_API_KEY": "key-SECRET",
    "CLAUDE_MODEL": "claude-sonnet-5-5",
    "SUBSTACK_PUBLICATION_URL": "https://example.substack.com",
    "SUBSTACK_COOKIE": "cookie-SECRET",
}


@pytest.fixture(autouse=True)
def _clean_redactor() -> None:
    """Keep registered secrets from leaking between tests."""
    redactor.clear()


def test_main_exits_non_zero_when_config_missing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Starting with no config exits non-zero and logs what is missing as JSON."""
    for name in VALUES:
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(SystemExit) as exit_info:
        entrypoint.main()
    assert exit_info.value.code != 0
    entry = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert entry["level"] == "ERROR"
    assert "TELEGRAM_BOT_TOKEN" in entry["event"]


def test_main_runs_heartbeat_and_never_logs_secrets(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """With valid config, main runs the heartbeat loop and logs no secrets."""
    for name, value in VALUES.items():
        monkeypatch.setenv(name, value)
    ran: list[bool] = []

    async def fake_heartbeat_loop() -> None:
        ran.append(True)
        logging.getLogger("humblebrag.test").info("leak check %s", "tok-SECRET")

    async def fake_run_bot(config: Config) -> None:
        ran.append(True)

    monkeypatch.setattr(entrypoint, "heartbeat_loop", fake_heartbeat_loop)
    monkeypatch.setattr(entrypoint, "run_bot", fake_run_bot)
    entrypoint.main()
    output = capsys.readouterr().out
    assert ran == [True, True]
    assert "claude-sonnet-5-5" in output
    assert "SECRET" not in output


def test_sigterm_stops_the_loop_cleanly(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """SIGTERM (docker stop) ends the run loop gracefully instead of hanging."""

    async def forever(*_args: Config) -> None:
        await asyncio.sleep(3600)

    async def run() -> None:
        asyncio.get_running_loop().call_later(
            0.05, os.kill, os.getpid(), signal.SIGTERM
        )
        await asyncio.wait_for(
            entrypoint.run_until_signalled(load_config(VALUES)), timeout=5
        )

    monkeypatch.setattr(entrypoint, "heartbeat_loop", forever)
    monkeypatch.setattr(entrypoint, "run_bot", forever)
    entrypoint.configure_logging()
    asyncio.run(run())
    assert "Shutdown signal received" in capsys.readouterr().out


def test_unhandled_error_is_logged_redacted_and_exits_non_zero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A crash never prints a raw traceback; secrets in the error are redacted."""
    for name, value in VALUES.items():
        monkeypatch.setenv(name, value)

    async def crash(_config: Config) -> None:
        raise RuntimeError("token tok-SECRET was rejected")

    monkeypatch.setattr(entrypoint, "run_until_signalled", crash)
    with pytest.raises(SystemExit) as exit_info:
        entrypoint.main()
    captured = capsys.readouterr()
    assert exit_info.value.code != 0
    assert "SECRET" not in captured.out + captured.err
    assert "unhandled error" in captured.out
