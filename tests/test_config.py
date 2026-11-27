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


def test_undecodable_file_is_a_config_error(tmp_path: Path) -> None:
    """AC2/AC3: a non-UTF-8 secret file is a clean ConfigError."""
    bad_file = tmp_path / "token"
    bad_file.write_bytes(b"\xff\xfe\x00bad")
    env = {**BASE_ENV, "TELEGRAM_BOT_TOKEN_FILE": str(bad_file)}
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


def test_post_word_range_defaults_to_250_600() -> None:
    """SA-23 AC4: the default range is 250 to 600 words."""
    config = load_config(BASE_ENV)
    assert (config.min_post_words, config.max_post_words) == (250, 600)


def test_post_word_range_is_configurable() -> None:
    """SA-23 AC4: POST_MIN_WORDS and POST_MAX_WORDS override the default."""
    config = load_config({**BASE_ENV, "POST_MIN_WORDS": "100", "POST_MAX_WORDS": "200"})
    assert (config.min_post_words, config.max_post_words) == (100, 200)


@pytest.mark.parametrize(
    "overrides",
    [
        {"POST_MIN_WORDS": "abc"},
        {"POST_MAX_WORDS": "0"},
        {"POST_MIN_WORDS": "700"},
    ],
)
def test_invalid_post_word_range_is_a_config_error(overrides: dict[str, str]) -> None:
    """SA-23 AC4: non-numeric, non-positive or inverted limits are rejected."""
    with pytest.raises(ConfigError):
        load_config({**BASE_ENV, **overrides})


# SA-43: the publication URL is validated when the config loads


@pytest.mark.parametrize(
    "url",
    [
        "https://substack.com/@name",
        "https://substack.com/@name/",
        "https://www.substack.com/@name",
        "https://substack.com",
        "https://humblebrag.substack.com/p/some-post",
        "http://humblebrag.substack.com",
        "humblebrag.substack.com",
        "https://",
        "https://[bad",
        "@name",
        "https://user@example.com",
        "https://:secret@example.com",
        "https://user:secret@example.com",
        "https://@example.com",
        "https://substack.com./@name",
        "https://www.substack.com./",
        "https://example.com:abc",
        "https://example.com:99999",
    ],
)
def test_a_wrong_shaped_publication_url_is_rejected_naming_the_setting(
    url: str,
) -> None:
    """SA-43 AC1: profile-style and other wrong URLs fail with the format."""
    with pytest.raises(ConfigError) as raised:
        load_config({**BASE_ENV, "SUBSTACK_PUBLICATION_URL": url})
    message = str(raised.value)
    assert "SUBSTACK_PUBLICATION_URL" in message
    assert "https://<subdomain>.substack.com" in message
    assert "custom domain" in message


def test_a_rejected_publication_url_exits_with_a_clear_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """SA-43 AC1: the process exits non-zero and logs the reason."""
    env = {**BASE_ENV, "SUBSTACK_PUBLICATION_URL": "https://substack.com/@name"}
    with pytest.raises(SystemExit) as raised, caplog.at_level(logging.ERROR):
        load_config_or_exit(env)
    assert raised.value.code != 0
    assert "SUBSTACK_PUBLICATION_URL" in caplog.text


@pytest.mark.parametrize(
    "url",
    [
        "https://humblebrag.substack.com",
        "https://humblebrag.substack.com/",
        "https://example.com",
        "https://blog.example.co.uk/",
        "https://example.com:8443",
    ],
)
def test_subdomain_and_custom_domain_publication_urls_are_accepted(url: str) -> None:
    """SA-43 AC3: real publication addresses still load, unchanged."""
    config = load_config({**BASE_ENV, "SUBSTACK_PUBLICATION_URL": url})
    assert config.substack_publication_url == url


def test_token_limits_have_the_previous_values_as_defaults() -> None:
    """SA-56 AC3/AC4: no new settings means the old limits and an 8192 ceiling."""
    config = load_config(BASE_ENV)
    assert config.post_max_tokens == 4096
    assert config.reply_max_tokens == 1024
    assert config.guard_max_tokens == 512
    assert config.seed_max_tokens == 128
    assert config.max_tokens_ceiling == 8192


def test_post_token_default_grows_with_the_word_range() -> None:
    """The derived POST_MAX_TOKENS default still scales with POST_MAX_WORDS."""
    config = load_config(
        {
            **BASE_ENV,
            "POST_MIN_WORDS": "10",
            "POST_MAX_WORDS": "5000",
            "CLAUDE_MAX_TOKENS_CEILING": "20000",
        }
    )
    assert config.post_max_tokens == 5000 * 3 + 512
    explicit = load_config({**BASE_ENV, "POST_MAX_TOKENS": "999"})
    assert explicit.post_max_tokens == 999


@pytest.mark.parametrize(
    "name",
    [
        "POST_MAX_TOKENS",
        "REPLY_MAX_TOKENS",
        "GUARD_MAX_TOKENS",
        "SEED_MAX_TOKENS",
        "CLAUDE_MAX_TOKENS_CEILING",
    ],
)
@pytest.mark.parametrize("bad", ["0", "-5", "lots"])
def test_bad_token_settings_are_rejected(name: str, bad: str) -> None:
    """SA-56 AC3: token settings must be positive integers, naming the variable."""
    with pytest.raises(ConfigError, match=name):
        load_config({**BASE_ENV, name: bad})


def test_derived_post_limit_above_the_ceiling_is_rejected() -> None:
    """A long word range with no room under the ceiling fails fast, since a
    cut-off post could not be retried; an explicit POST_MAX_TOKENS is allowed."""
    env = {**BASE_ENV, "POST_MIN_WORDS": "10", "POST_MAX_WORDS": "3000"}
    with pytest.raises(ConfigError, match="CLAUDE_MAX_TOKENS_CEILING"):
        load_config(env)
    assert load_config({**env, "POST_MAX_TOKENS": "4000"}).post_max_tokens == 4000
