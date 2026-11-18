"""Redo with an optional steer: ask for a steer, regenerate from the same seed,
and replace the preview. Gives up waiting after 30 minutes and redoes unsteered."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from telegram import Bot, Update
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from humblebrag.alerts import alert, describe_error
from humblebrag.buttons import ALREADY_HANDLED_TEXT, register_action
from humblebrag.drafts import (
    MAX_REDOS,
    STATUS_PENDING,
    Draft,
    DraftError,
    DraftStateError,
    DraftStore,
    RedoLimitReachedError,
)
from humblebrag.guard import CheckedPost
from humblebrag.preview import ACTION_REDO, send_preview

logger = logging.getLogger(__name__)

STEER_TIMEOUT_SECONDS = 30 * 60
SERVICE_KEY = "redo_service"
STEER_GROUP = 1
NOSTEER_COMMAND = "nosteer"
ALERT_EVENT = "redo"
STEER_PROMPT = "Steer? Reply with text, or /nosteer."
NO_REDOS_TEXT = f"No redos left: a draft gets {MAX_REDOS}."
BUSY_TEXT = "A redo is already in progress. Reply with a steer or /nosteer first."
NOTHING_PENDING_TEXT = "No redo is waiting for a steer."
FAILED_TEXT = "The redo hit a problem and I've alerted you. The draft is unchanged."

Regenerate = Callable[[str, str | None], Awaitable[CheckedPost]]
Sleeper = Callable[[float], Awaitable[None]]


@dataclass
class _PendingRedo:
    """The one redo currently waiting for a steer or running."""

    draft_id: int
    chat_id: int
    message_id: int | None
    started: bool = False
    timeout_task: asyncio.Task[None] | None = None


class RedoService:
    """Runs redos, one at a time."""

    def __init__(
        self,
        store: DraftStore,
        regenerate: Regenerate,
        timeout: float = STEER_TIMEOUT_SECONDS,
        sleep: Sleeper = asyncio.sleep,
    ) -> None:
        """Hold the store, the guarded generator and the steer timeout."""
        self._store = store
        self._regenerate = regenerate
        self._timeout = timeout
        self._sleep = sleep
        self._pending: _PendingRedo | None = None

    async def on_redo_tap(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, draft: Draft
    ) -> None:
        """Handle a tap on Redo for a pending draft: ask for a steer."""
        chat = update.effective_chat
        query = update.callback_query
        if chat is None or query is None:
            return
        if draft.redo_count >= MAX_REDOS:
            await context.bot.send_message(chat_id=chat.id, text=NO_REDOS_TEXT)
            return
        if self._pending is not None:
            await context.bot.send_message(chat_id=chat.id, text=BUSY_TEXT)
            return
        message_id = getattr(query.message, "message_id", None)
        pending = _PendingRedo(draft.id, chat.id, message_id)
        self._pending = pending
        pending.timeout_task = asyncio.create_task(self._expire(context.bot, pending))
        await context.bot.send_message(chat_id=chat.id, text=STEER_PROMPT)

    async def on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Use a plain text message as the steer, if a redo is waiting for one."""
        message = update.effective_message
        chat = update.effective_chat
        if message is None or not message.text or chat is None:
            return
        if self._pending is None or chat.id != self._pending.chat_id:
            return
        await self._start(context.bot, message.text.strip() or None)

    async def on_nosteer(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """/nosteer: redo with no steer."""
        chat = update.effective_chat
        if chat is None:
            return
        pending = self._pending
        if pending is None or pending.started or pending.chat_id != chat.id:
            await context.bot.send_message(chat_id=chat.id, text=NOTHING_PENDING_TEXT)
            return
        await self._start(context.bot, None)

    async def _expire(self, bot: Bot, pending: _PendingRedo) -> None:
        """After the timeout, redo with no steer if nobody replied."""
        await self._sleep(self._timeout)
        if self._pending is pending and not pending.started:
            logger.info(
                "No steer for draft %d in time; redoing unsteered", pending.draft_id
            )
            await self._start(bot, None)

    async def _start(self, bot: Bot, steer: str | None) -> None:
        """Claim the waiting redo (once) and run it."""
        pending = self._pending
        if pending is None or pending.started:
            return
        pending.started = True
        task = pending.timeout_task
        if task is not None and task is not asyncio.current_task():
            task.cancel()
        try:
            await self._perform(bot, pending, steer)
        finally:
            self._pending = None

    async def _perform(
        self, bot: Bot, pending: _PendingRedo, steer: str | None
    ) -> None:
        """Regenerate, store, drop the old buttons and send the new preview."""
        try:
            draft = await asyncio.to_thread(self._store.get, pending.draft_id)
            if draft.status != STATUS_PENDING:
                await bot.send_message(
                    chat_id=pending.chat_id, text=ALREADY_HANDLED_TEXT
                )
                return
            checked = await self._regenerate(draft.seed, steer)
            updated = await asyncio.to_thread(
                self._store.apply_redo,
                draft.id,
                checked.post.title,
                checked.post.subtitle,
                checked.post.body_markdown,
                steer,
            )
        except DraftStateError:
            await bot.send_message(chat_id=pending.chat_id, text=ALREADY_HANDLED_TEXT)
            return
        except RedoLimitReachedError:
            await bot.send_message(chat_id=pending.chat_id, text=NO_REDOS_TEXT)
            return
        except Exception as error:  # a failed redo must never kill the bot
            logger.exception("Redo of draft %d failed", pending.draft_id)
            await alert(
                ALERT_EVENT,
                f"Redo of draft {pending.draft_id} failed: {describe_error(error)}",
            )
            await bot.send_message(chat_id=pending.chat_id, text=FAILED_TEXT)
            return
        await self._replace_preview(bot, pending, updated, checked)

    async def _replace_preview(
        self, bot: Bot, pending: _PendingRedo, updated: Draft, checked: CheckedPost
    ) -> None:
        """Remove the old buttons, then send the new preview."""
        if pending.message_id is not None:
            try:
                await bot.edit_message_reply_markup(
                    chat_id=pending.chat_id,
                    message_id=pending.message_id,
                    reply_markup=None,
                )
            except TelegramError as error:
                logger.warning("Could not remove old buttons: %s", type(error).__name__)
        try:
            await send_preview(
                bot, pending.chat_id, updated, checked.guard.preview_line()
            )
        except (TelegramError, DraftError) as error:
            logger.exception("Could not send the redo preview")
            await alert(
                ALERT_EVENT,
                f"Redo of draft {pending.draft_id} saved but preview failed: "
                f"{describe_error(error)}",
            )


def install_redo(application: Application, service: RedoService) -> None:
    """Wire the Redo button, plain-text steers and /nosteer into the bot."""
    application.bot_data[SERVICE_KEY] = service
    register_action(application, ACTION_REDO, service.on_redo_tap)
    application.add_handler(CommandHandler(NOSTEER_COMMAND, service.on_nosteer))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, service.on_text),
        group=STEER_GROUP,
    )
