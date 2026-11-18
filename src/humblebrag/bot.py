"""Telegram bot skeleton: long polling, locked to a single chat."""

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable

from telegram import Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    TypeHandler,
)

from humblebrag.alerts import alert, configure_alerts, describe_error, telegram_sender
from humblebrag.buttons import STORE_KEY, handle_button
from humblebrag.config import Config
from humblebrag.drafts import DraftStore

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


def build_application(config: Config, store: DraftStore | None = None) -> Application:
    """Build the bot application with the chat guard and command handlers.

    With a draft ``store`` it also routes the Approve / Redo / Bin buttons.
    """
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
    if store is not None:
        application.bot_data[STORE_KEY] = store
        application.add_handler(CallbackQueryHandler(handle_button))
    return application


async def _run_startup(on_started: Callable[[], Awaitable[object]]) -> None:
    """Run the startup hook; a failure is logged and alerted, never lost."""
    try:
        await on_started()
    except Exception as error:  # the task is never awaited until shutdown
        logger.exception("Startup task failed")
        await alert("startup", f"Startup task failed: {describe_error(error)}")


async def run_bot(
    config: Config, on_started: Callable[[], Awaitable[object]] | None = None
) -> None:
    """Run the bot with long polling (no inbound port) until cancelled.

    ``on_started`` runs once as a background task after polling has started and
    alerts work, e.g. the Substack session check. It is cancelled on shutdown.
    """
    application = build_application(config)
    startup_task: asyncio.Task[object] | None = None
    async with application:
        try:
            await application.start()
            await application.updater.start_polling(timeout=POLL_TIMEOUT_SECONDS)
            configure_alerts(telegram_sender(application.bot, config.telegram_chat_id))
            logger.info("Telegram bot polling")
            if on_started is not None:
                startup_task = asyncio.create_task(_run_startup(on_started))
            await asyncio.Event().wait()
        finally:
            if startup_task is not None and not startup_task.done():
                startup_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await startup_task
            configure_alerts(None)
            if application.updater.running:
                await application.updater.stop()
            if application.running:
                await application.stop()
