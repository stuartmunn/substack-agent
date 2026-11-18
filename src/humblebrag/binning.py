"""Bin handler: discard a draft but keep its seed at the front of the queue."""

import asyncio
import logging

from telegram import Update
from telegram.ext import Application, ContextTypes

from humblebrag.alerts import alert, describe_error
from humblebrag.buttons import ALREADY_HANDLED_TEXT, register_action
from humblebrag.drafts import STATUS_BINNED, Draft, DraftStateError, DraftStore
from humblebrag.preview import ACTION_BIN
from humblebrag.seed_queue import SeedQueue

logger = logging.getLogger(__name__)

ALERT_EVENT = "bin"
BINNED_TEXT = "Binned, seed requeued"
FAILED_TEXT = "Binning hit a problem and I've alerted you."


class BinService:
    """Handles taps on the Bin button."""

    def __init__(self, store: DraftStore, queue: SeedQueue) -> None:
        """Hold the draft store and the seed queue."""
        self._store = store
        self._queue = queue

    async def on_bin_tap(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, draft: Draft
    ) -> None:
        """Mark the draft binned, release its seed, and confirm."""
        chat = update.effective_chat
        if chat is None:
            return
        try:
            already_binned = await asyncio.to_thread(self._bin_draft, draft.id)
        except Exception as error:  # a failed bin must never kill the bot
            logger.exception("Binning draft %d failed", draft.id)
            await self._fail(context, chat.id, draft.id, error)
            return
        if already_binned is None:
            await context.bot.send_message(chat_id=chat.id, text=ALREADY_HANDLED_TEXT)
            return
        try:
            released = await asyncio.to_thread(self._queue.bin_draft, str(draft.id))
        except Exception as error:  # the draft is binned but its seed is stuck
            logger.exception("Releasing the seed of draft %d failed", draft.id)
            await self._fail(context, chat.id, draft.id, error)
            return
        if already_binned and not released:
            await context.bot.send_message(chat_id=chat.id, text=ALREADY_HANDLED_TEXT)
            return
        if not released:
            logger.warning("Draft %d held no seed reservation", draft.id)
        await context.bot.send_message(chat_id=chat.id, text=BINNED_TEXT)
        await self._remove_buttons(update, context, chat.id)

    def _bin_draft(self, draft_id: int) -> bool | None:
        """Bin the draft. True if it was already binned (so a repeat tap can
        finish releasing the seed), False if just binned, None if it was
        resolved another way."""
        try:
            self._store.mark_binned(draft_id)
        except DraftStateError:
            return True if self._store.get(draft_id).status == STATUS_BINNED else None
        return False

    async def _fail(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        draft_id: int,
        error: Exception,
    ) -> None:
        """Alert Stuart and tell the chat that binning failed."""
        await alert(
            ALERT_EVENT, f"Binning draft {draft_id} failed: {describe_error(error)}"
        )
        await context.bot.send_message(chat_id=chat_id, text=FAILED_TEXT)

    @staticmethod
    async def _remove_buttons(
        update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: int
    ) -> None:
        """Best effort: take the buttons off the message that was tapped.

        Any failure is logged and swallowed, so it cannot hide the confirmation.
        """
        query = update.callback_query
        message_id = getattr(query.message, "message_id", None) if query else None
        if message_id is None:
            return
        try:
            await context.bot.edit_message_reply_markup(
                chat_id=chat_id, message_id=message_id, reply_markup=None
            )
        except Exception:  # best effort: never mask a completed bin
            logger.exception("Could not remove old buttons")


def install_bin(application: Application, service: BinService) -> None:
    """Wire the Bin button to the service."""
    register_action(application, ACTION_BIN, service.on_bin_tap)
