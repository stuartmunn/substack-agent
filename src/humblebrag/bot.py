"""Telegram bot skeleton: long polling, locked to a single chat."""

import asyncio
import logging

from telegram import Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    ApplicationHandlerStop,
    CommandHandler,
    ContextTypes,
    TypeHandler,
)

from humblebrag.config import Config

logger = logging.getLogger(__name__)

GUARD_GROUP = -1
CONNECT_TIMEOUT_SECONDS = 10.0
READ_TIMEOUT_SECONDS = 20.0
WRITE_TIMEOUT_SECONDS = 20.0
POOL_TIMEOUT_SECONDS = 10.0
POLL_TIMEOUT_SECONDS = 30
# PTB adds the poll timeout to the get_updates read timeout, so the effective
# read timeout is POLL_TIMEOUT_SECONDS + GET_UPDATES_READ_TIMEOUT_SECONDS.
GET_UPDATES_READ_TIMEOUT_SECONDS = 10.0
GET_UPDATES_CONNECT_TIMEOUT_SECONDS = 10.0
GET_UPDATES_WRITE_TIMEOUT_SECONDS = 20.0
GET_UPDATES_POOL_TIMEOUT_SECONDS = 10.0
COMMANDS: dict[str, str] = {
    "start": "Show the list of commands",
    "help": "Show the list of commands",
}
ALLOWED_CHAT_KEY = "allowed_chat_id"


def help_text() -> str:
    """Return the command list shown by /start and /help."""
    lines = ["Sir Humblebrag III. Commands:"]
    lines.extend(f"/{name} - {description}" for name, description in COMMANDS.items())
    return "\n".join(lines)


async def guard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Drop and log every update that is not from the allowed chat."""
    chat = update.effective_chat
    allowed_chat_id = context.application.bot_data[ALLOWED_CHAT_KEY]
    if chat is not None and chat.id == allowed_chat_id:
        return
    logger.warning(
        "Ignored update %s from unauthorised chat %s",
        update.update_id,
        "none" if chat is None else chat.id,
    )
    raise ApplicationHandlerStop


async def show_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reply with the command list."""
    message = update.effective_message
    if message is not None:
        await message.reply_text(help_text())


def build_application(config: Config) -> Application:
    """Build the bot application with the chat guard and command handlers."""
    application = (
        ApplicationBuilder()
        .token(config.telegram_bot_token)
        .connect_timeout(CONNECT_TIMEOUT_SECONDS)
        .read_timeout(READ_TIMEOUT_SECONDS)
        .write_timeout(WRITE_TIMEOUT_SECONDS)
        .pool_timeout(POOL_TIMEOUT_SECONDS)
        .get_updates_connect_timeout(GET_UPDATES_CONNECT_TIMEOUT_SECONDS)
        .get_updates_read_timeout(GET_UPDATES_READ_TIMEOUT_SECONDS)
        .get_updates_write_timeout(GET_UPDATES_WRITE_TIMEOUT_SECONDS)
        .get_updates_pool_timeout(GET_UPDATES_POOL_TIMEOUT_SECONDS)
        .build()
    )
    application.bot_data[ALLOWED_CHAT_KEY] = config.telegram_chat_id
    application.add_handler(TypeHandler(Update, guard), group=GUARD_GROUP)
    application.add_handler(CommandHandler(list(COMMANDS), show_help))
    return application


async def run_bot(config: Config) -> None:
    """Run the bot with long polling (no inbound port) until cancelled."""
    application = build_application(config)
    async with application:
        try:
            await application.start()
            await application.updater.start_polling(timeout=POLL_TIMEOUT_SECONDS)
            logger.info("Telegram bot polling")
            await asyncio.Event().wait()
        finally:
            if application.updater.running:
                await application.updater.stop()
            if application.running:
                await application.stop()
