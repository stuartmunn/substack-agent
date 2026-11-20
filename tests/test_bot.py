"""Tests for the Telegram bot skeleton (SA-17 acceptance criteria)."""

import asyncio
import logging
import tomllib
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram import Bot, Update, User
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CommandHandler,
    ExtBot,
)

from humblebrag import bot
from humblebrag.config import Config, load_config

ALLOWED_CHAT = 12345
STRANGER_CHAT = 99999
ENV = {
    "TELEGRAM_BOT_TOKEN": "123456:TESTTOKEN",
    "TELEGRAM_CHAT_ID": str(ALLOWED_CHAT),
    "ANTHROPIC_API_KEY": "key",
    "CLAUDE_MODEL": "m",
    "SUBSTACK_PUBLICATION_URL": "https://example.substack.com",
    "SUBSTACK_COOKIE": "cookie",
}


@pytest.fixture
def config() -> Config:
    """A valid config for the allowed chat."""
    return load_config(ENV)


@pytest.fixture
async def offline_application(
    monkeypatch: pytest.MonkeyPatch, config: Config
) -> AsyncIterator[Application]:
    """An initialised application with Telegram network calls stubbed out."""
    me = User(id=1, first_name="humblebrag", is_bot=True, username="humblebrag_bot")
    monkeypatch.setattr(ExtBot, "get_me", AsyncMock(return_value=me))
    monkeypatch.setattr(Bot, "bot", property(lambda _self: me))
    application = bot.build_application(config)
    async with application:
        yield application


def _context(application: Application) -> Any:
    """Minimal handler context exposing the application."""
    return SimpleNamespace(application=application)


def _update(chat_id: int | None, update_id: int = 1) -> Any:
    """A stand-in update with an effective chat and a reply-capable message."""
    chat = None if chat_id is None else SimpleNamespace(id=chat_id)
    message = SimpleNamespace(reply_text=AsyncMock())
    return SimpleNamespace(
        update_id=update_id, effective_chat=chat, effective_message=message
    )


def test_application_is_built_for_long_polling(config: Config) -> None:
    """AC1: the app has an updater (long polling) and is not a webhook app."""
    application = bot.build_application(config)
    assert application.updater is not None
    assert application.bot_data[bot.ALLOWED_CHAT_KEY] == ALLOWED_CHAT


def test_pyproject_pins_python_telegram_bot() -> None:
    """AC1: built with python-telegram-bot, pinned (SEC-08)."""
    root = Path(__file__).resolve().parent.parent
    deps = tomllib.loads((root / "pyproject.toml").read_text())["project"][
        "dependencies"
    ]
    assert any(dep.startswith("python-telegram-bot==") for dep in deps)


async def test_run_bot_uses_polling_not_webhook(
    monkeypatch: pytest.MonkeyPatch, config: Config
) -> None:
    """AC1: run_bot starts polling and never sets a webhook."""
    updater, app = _fake_application(monkeypatch)

    task = asyncio.create_task(bot.run_bot(config))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    updater.start_polling.assert_awaited_once()
    updater.stop.assert_awaited_once()
    app.bot.set_webhook.assert_not_called()


def _fake_application(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[SimpleNamespace, MagicMock]:
    """Patch build_application to return a fake app with a fake updater."""
    updater = SimpleNamespace(start_polling=AsyncMock(), stop=AsyncMock(), running=True)
    app = MagicMock()
    app.updater = updater
    app.running = True
    app.start = AsyncMock()
    app.stop = AsyncMock()
    app.__aenter__ = AsyncMock(return_value=app)
    app.__aexit__ = AsyncMock(return_value=None)
    monkeypatch.setattr(bot, "build_application", lambda _config: app)
    return updater, app


async def test_run_bot_registers_the_alert_sender_and_removes_it_on_stop(
    monkeypatch: pytest.MonkeyPatch, config: Config
) -> None:
    """Components can alert while the bot runs; the sender is unset after."""
    from humblebrag import alerts

    _fake_application(monkeypatch)
    task = asyncio.create_task(bot.run_bot(config))
    await asyncio.sleep(0.05)
    assert alerts._sender is not None
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert alerts._sender is None


async def test_run_bot_cleans_up_when_polling_fails_to_start(
    monkeypatch: pytest.MonkeyPatch, config: Config
) -> None:
    """If start_polling raises, what was started is stopped and the error
    propagates (so the process exits and Docker restarts it)."""
    updater, app = _fake_application(monkeypatch)
    updater.start_polling.side_effect = RuntimeError("network down")
    with pytest.raises(RuntimeError, match="network down"):
        await bot.run_bot(config)
    updater.stop.assert_awaited_once()
    app.stop.assert_awaited_once()


async def test_run_bot_stops_nothing_that_never_started(
    monkeypatch: pytest.MonkeyPatch, config: Config
) -> None:
    """If the application fails to start, no stop calls are made on it."""
    updater, app = _fake_application(monkeypatch)
    updater.running = False
    app.running = False
    app.start.side_effect = RuntimeError("boom")
    with pytest.raises(RuntimeError, match="boom"):
        await bot.run_bot(config)
    updater.stop.assert_not_awaited()
    app.stop.assert_not_awaited()


def test_long_poll_read_timeout_exceeds_poll_window(config: Config) -> None:
    """ERR-02: the get_updates client is explicitly configured, and PTB adds
    the poll timeout to its read timeout, so reads outlast the long poll."""
    get_updates_client = bot.build_application(config).bot._request[0]
    assert get_updates_client._client.timeout.read == (
        bot.GET_UPDATES_READ_TIMEOUT_SECONDS
    )


def test_every_external_call_has_a_timeout(config: Config) -> None:
    """ERR-02: the HTTP client timeouts are configured explicitly."""
    request = bot.build_application(config).bot.request
    assert request._client.timeout.connect == bot.CONNECT_TIMEOUT_SECONDS
    assert request._client.timeout.read == bot.READ_TIMEOUT_SECONDS


@pytest.mark.parametrize("chat_id", [STRANGER_CHAT, None])
async def test_guard_ignores_and_logs_other_chats(
    config: Config, caplog: pytest.LogCaptureFixture, chat_id: int | None
) -> None:
    """AC2: an update from any other chat is stopped and logged."""
    application = bot.build_application(config)
    with caplog.at_level(logging.WARNING), pytest.raises(ApplicationHandlerStop):
        await bot.guard(_update(chat_id), _context(application))
    assert "unauthorised chat" in caplog.text
    assert str(chat_id if chat_id is not None else "none") in caplog.text


async def test_guard_lets_the_allowed_chat_through(config: Config) -> None:
    """AC2: the configured chat is not stopped."""
    application = bot.build_application(config)
    await bot.guard(_update(ALLOWED_CHAT), _context(application))


def _message_payload(chat_id: int) -> dict[str, Any]:
    """A raw Telegram /help message from ``chat_id``."""
    return {
        "message_id": 1,
        "date": 0,
        "chat": {"id": chat_id, "type": "private"},
        "from": {"id": chat_id, "is_bot": False, "first_name": "X"},
        "text": "/help",
        "entities": [{"type": "bot_command", "offset": 0, "length": 5}],
    }


async def test_stranger_message_and_button_press_are_dropped(
    monkeypatch: pytest.MonkeyPatch,
    offline_application: Application,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AC2: through the real handler chain, a stranger's message and button
    press get no reply and are logged."""
    send_message = AsyncMock()
    monkeypatch.setattr(Bot, "send_message", send_message)
    application = offline_application
    message = _message_payload(STRANGER_CHAT)
    button = {
        "id": "1",
        "from": message["from"],
        "chat_instance": "c",
        "data": "approve",
        "message": {**message, "text": "draft"},
    }
    with caplog.at_level(logging.WARNING):
        for payload in (
            {"update_id": 1, "message": message},
            {"update_id": 2, "callback_query": button},
        ):
            await application.process_update(Update.de_json(payload, application.bot))
    send_message.assert_not_called()
    assert caplog.text.count("unauthorised chat") == 2


async def test_allowed_chat_help_is_answered_through_the_real_chain(
    monkeypatch: pytest.MonkeyPatch, offline_application: Application
) -> None:
    """AC2/AC3: the same chain answers /help for the configured chat."""
    send_message = AsyncMock()
    monkeypatch.setattr(Bot, "send_message", send_message)
    application = offline_application
    payload = {"update_id": 3, "message": _message_payload(ALLOWED_CHAT)}
    await application.process_update(Update.de_json(payload, application.bot))
    send_message.assert_awaited_once()
    assert "/help" in send_message.await_args.kwargs["text"]


async def test_start_and_help_list_the_commands() -> None:
    """AC3: /start and /help reply with every command."""
    for name in ("start", "help"):
        update = _update(ALLOWED_CHAT)
        await bot.show_help(update, MagicMock())
        text = update.effective_message.reply_text.await_args.args[0]
        assert f"/{name}" in text
    assert {"start", "help"} <= set(bot.COMMANDS)


def test_start_and_help_are_registered_as_commands(config: Config) -> None:
    """AC3: both commands are wired to the help handler, and only those two."""
    application = bot.build_application(config)
    commands = {
        command
        for handler in application.handlers[0]
        if isinstance(handler, CommandHandler)
        for command in handler.commands
    }
    assert commands == {"start", "help"}


def test_help_lists_the_new_commands() -> None:
    """SA-36/SA-45: /help mentions /seed, /seeds, /unseed, /skip and /now."""
    text = bot.help_text()
    for name in ("seed", "seeds", "unseed", "skip", "now"):
        assert f"/{name} " in text
