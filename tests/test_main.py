"""Tests for the container entrypoint."""

import json
import logging

import pytest

from humblebrag import __main__ as entrypoint
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

    monkeypatch.setattr(entrypoint, "heartbeat_loop", fake_heartbeat_loop)
    entrypoint.main()
    output = capsys.readouterr().out
    assert ran == [True]
    assert "claude-sonnet-5-5" in output
    assert "SECRET" not in output
