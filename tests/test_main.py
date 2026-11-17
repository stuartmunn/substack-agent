"""Tests for the container entrypoint."""

import logging

import pytest

from humblebrag.__main__ import main


def test_main_exits_non_zero_when_config_missing(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Starting with no config exits non-zero and says what is missing."""
    for name in ("TELEGRAM_BOT_TOKEN", "CLAUDE_MODEL"):
        monkeypatch.delenv(name, raising=False)
    with caplog.at_level(logging.ERROR), pytest.raises(SystemExit) as exit_info:
        main()
    assert exit_info.value.code != 0
    assert "TELEGRAM_BOT_TOKEN" in caplog.text


def test_main_loads_valid_config(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """With valid config, main logs readiness and never the secrets."""
    values = {
        "TELEGRAM_BOT_TOKEN": "tok-SECRET",
        "TELEGRAM_CHAT_ID": "1",
        "ANTHROPIC_API_KEY": "key-SECRET",
        "CLAUDE_MODEL": "claude-sonnet-5-5",
        "SUBSTACK_PUBLICATION_URL": "https://example.substack.com",
        "SUBSTACK_COOKIE": "cookie-SECRET",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    with caplog.at_level(logging.INFO):
        main()
    assert "claude-sonnet-5-5" in caplog.text
    assert "SECRET" not in caplog.text
