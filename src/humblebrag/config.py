"""Configuration loader: reads env vars, or Docker secrets via ``*_FILE``."""

import logging
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger(__name__)

FILE_SUFFIX = "_FILE"
DEFAULT_RUN_TIME = "08:30"
DEFAULT_TZ = "Europe/London"
DEFAULT_MIN_POST_WORDS = 250
DEFAULT_MAX_POST_WORDS = 600
EXIT_CONFIG_ERROR = 1


class ConfigError(Exception):
    """Raised when configuration is missing or invalid.

    Messages name variables only; they never contain secret values.
    """


@dataclass(frozen=True)
class Config:
    """Validated application config. Secret fields are hidden from repr()."""

    telegram_bot_token: str = field(repr=False)
    telegram_chat_id: int
    anthropic_api_key: str = field(repr=False)
    claude_model: str
    substack_publication_url: str
    substack_cookie: str = field(repr=False)
    run_time: time
    tz: ZoneInfo
    min_post_words: int = DEFAULT_MIN_POST_WORDS
    max_post_words: int = DEFAULT_MAX_POST_WORDS


def _read_value(env: Mapping[str, str], name: str) -> str | None:
    """Return a value from ``<name>_FILE`` (preferred) or ``<name>``, else None."""
    file_var = name + FILE_SUFFIX
    path_text = env.get(file_var, "").strip()
    if path_text:
        try:
            value = Path(path_text).read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError) as error:
            raise ConfigError(
                f"{file_var} points to a file that cannot be read "
                f"({type(error).__name__})"
            ) from None
    else:
        value = env.get(name, "").strip()
    return value or None


def _require(env: Mapping[str, str], name: str, missing: list[str]) -> str:
    """Return a required value, recording its name in ``missing`` if absent."""
    value = _read_value(env, name)
    if value is None:
        missing.append(name)
        return ""
    return value


def _parse_run_time(text: str) -> time:
    """Parse ``HH:MM`` into a time."""
    try:
        return time.fromisoformat(text)
    except ValueError:
        raise ConfigError("RUN_TIME must be in HH:MM format") from None


def _parse_tz(text: str) -> ZoneInfo:
    """Parse an IANA time zone name."""
    try:
        return ZoneInfo(text)
    except (ZoneInfoNotFoundError, ValueError):
        raise ConfigError("TZ is not a known IANA time zone") from None


def _parse_chat_id(text: str) -> int:
    """Parse the Telegram chat id."""
    try:
        return int(text)
    except ValueError:
        raise ConfigError("TELEGRAM_CHAT_ID must be an integer") from None


def _parse_word_limit(env: Mapping[str, str], name: str, default: int) -> int:
    """Parse an optional positive integer word limit."""
    text = _read_value(env, name)
    if text is None:
        return default
    try:
        value = int(text)
    except ValueError:
        raise ConfigError(f"{name} must be a positive integer") from None
    if value < 1:
        raise ConfigError(f"{name} must be a positive integer")
    return value


def load_config(env: Mapping[str, str] | None = None) -> Config:
    """Build a Config from ``env`` (default: the process environment)."""
    source = os.environ if env is None else env
    missing: list[str] = []
    token = _require(source, "TELEGRAM_BOT_TOKEN", missing)
    chat_id = _require(source, "TELEGRAM_CHAT_ID", missing)
    api_key = _require(source, "ANTHROPIC_API_KEY", missing)
    model = _require(source, "CLAUDE_MODEL", missing)
    publication_url = _require(source, "SUBSTACK_PUBLICATION_URL", missing)
    cookie = _require(source, "SUBSTACK_COOKIE", missing)
    if missing:
        raise ConfigError("Missing required config: " + ", ".join(missing))
    min_words = _parse_word_limit(source, "POST_MIN_WORDS", DEFAULT_MIN_POST_WORDS)
    max_words = _parse_word_limit(source, "POST_MAX_WORDS", DEFAULT_MAX_POST_WORDS)
    if min_words > max_words:
        raise ConfigError("POST_MIN_WORDS must not exceed POST_MAX_WORDS")
    return Config(
        telegram_bot_token=token,
        telegram_chat_id=_parse_chat_id(chat_id),
        anthropic_api_key=api_key,
        claude_model=model,
        substack_publication_url=publication_url,
        substack_cookie=cookie,
        run_time=_parse_run_time(_read_value(source, "RUN_TIME") or DEFAULT_RUN_TIME),
        tz=_parse_tz(_read_value(source, "TZ") or DEFAULT_TZ),
        min_post_words=min_words,
        max_post_words=max_words,
    )


def load_config_or_exit(env: Mapping[str, str] | None = None) -> Config:
    """Load config; on failure log a clear message and exit non-zero."""
    try:
        return load_config(env)
    except ConfigError as error:
        logger.error("Configuration error: %s", error)
        sys.exit(EXIT_CONFIG_ERROR)
