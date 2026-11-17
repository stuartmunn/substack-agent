"""Tests for the config and secrets loader (SA-14 acceptance criteria)."""

import logging
from datetime import time
from pathlib import Path

import pytest

from humblebrag.config import Config, ConfigError, load_config, load_config_or_exit

SECRETS = {
    "TELEGRAM_BOT_TOKEN": "tok-SECRET-1",
    "ANTHROPIC_API_KEY": "key-SECRET-2",
    "SUBSTACK_COOKIE": "cookie-SECRET-3",
}
BASE_ENV = {
    **SECRETS,
    "TELEGRAM_CHAT_ID": "12345",
    "CLAUDE_MODEL": "claude-sonnet-5-5",
    "SUBSTACK_PUBLICATION_URL": "https://example.substack.com",
}


def test_loads_all_values_from_env() -> None:
    """AC1: every variable is read from the environment."""
    env = {**BASE_ENV, "RUN_TIME": "07:15", "TZ": "America/New_York"}
    config = load_config(env)
    assert config.telegram_bot_token == "tok-SECRET-1"
    assert config.telegram_chat_id == 12345
    assert config.anthropic_api_key == "key-SECRET-2"
    assert config.claude_model == "claude-sonnet-5-5"
    assert config.substack_publication_url == "https://example.substack.com"
    assert config.substack_cookie == "cookie-SECRET-3"
    assert config.run_time == time(7, 15)
    assert str(config.tz) == "America/New_York"


def test_defaults_for_run_time_and_tz() -> None:
    """AC1: RUN_TIME defaults to 08:30 and TZ to Europe/London."""
    config = load_config(BASE_ENV)
    assert config.run_time == time(8, 30)
    assert str(config.tz) == "Europe/London"


def test_file_variant_is_read_from_file(tmp_path: Path) -> None:
    """AC2: a *_FILE variable makes the value come from that file."""
    secret_file = tmp_path / "token"
    secret_file.write_text("from-file-token\n")
    env = {k: v for k, v in BASE_ENV.items() if k != "TELEGRAM_BOT_TOKEN"}
    env["TELEGRAM_BOT_TOKEN_FILE"] = str(secret_file)
    assert load_config(env).telegram_bot_token == "from-file-token"


def test_file_variant_wins_over_plain_variable(tmp_path: Path) -> None:
    """AC2: when both are set, the file is used."""
    secret_file = tmp_path / "token"
    secret_file.write_text("from-file-token")
    env = {**BASE_ENV, "TELEGRAM_BOT_TOKEN_FILE": str(secret_file)}
    assert load_config(env).telegram_bot_token == "from-file-token"


def test_unreadable_file_is_a_config_error(tmp_path: Path) -> None:
    """AC2/AC3: an unreadable secret file fails clearly, without a value."""
    env = {**BASE_ENV, "TELEGRAM_BOT_TOKEN_FILE": str(tmp_path / "missing")}
    with pytest.raises(ConfigError, match="TELEGRAM_BOT_TOKEN_FILE"):
        load_config(env)


@pytest.mark.parametrize("name", sorted(BASE_ENV))
def test_missing_required_value_exits_non_zero_without_secrets(
    name: str, caplog: pytest.LogCaptureFixture
) -> None:
    """AC3: a missing required value exits non-zero naming it, no secrets."""
    env = {k: v for k, v in BASE_ENV.items() if k != name}
    with caplog.at_level(logging.ERROR), pytest.raises(SystemExit) as exit_info:
        load_config_or_exit(env)
    assert exit_info.value.code != 0
    assert name in caplog.text
    for secret in SECRETS.values():
        assert secret not in caplog.text


def test_all_missing_values_are_listed() -> None:
    """AC3: the error lists every missing variable at once."""
    with pytest.raises(ConfigError) as error:
        load_config({})
    for name in BASE_ENV:
        assert name in str(error.value)


@pytest.mark.parametrize(
    ("override", "expected"),
    [
        ({"RUN_TIME": "half past eight"}, "RUN_TIME"),
        ({"TZ": "Mars/Olympus"}, "TZ"),
        ({"TELEGRAM_CHAT_ID": "abc"}, "TELEGRAM_CHAT_ID"),
    ],
)
def test_invalid_values_are_config_errors(
    override: dict[str, str], expected: str
) -> None:
    """AC3: invalid values fail clearly, naming the variable only."""
    with pytest.raises(ConfigError, match=expected) as error:
        load_config({**BASE_ENV, **override})
    for secret in SECRETS.values():
        assert secret not in str(error.value)


def test_secrets_never_appear_in_repr_or_str() -> None:
    """AC4: repr() and str() of Config never contain secret values."""
    config: Config = load_config(BASE_ENV)
    for text in (repr(config), str(config)):
        for secret in SECRETS.values():
            assert secret not in text
    assert "claude-sonnet-5-5" in repr(config)


def test_secrets_never_appear_in_logs(caplog: pytest.LogCaptureFixture) -> None:
    """AC4: loading config logs no secret values."""
    with caplog.at_level(logging.DEBUG):
        load_config(BASE_ENV)
    for secret in SECRETS.values():
        assert secret not in caplog.text
