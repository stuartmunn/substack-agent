"""Approve handler: one tap publishes the draft to the web (never by email).

Order matters because publishing cannot be undone. After a successful publish
the draft is marked published *first*, so a second tap sees it resolved and
cannot publish again; only then is the seed marked used. A failed publish
leaves the draft pending with its buttons active, and the error goes to Stuart.
"""

import asyncio
import logging

from telegram import Update
from telegram.ext import Application, ContextTypes

from humblebrag.alerts import alert, describe_error
from humblebrag.buttons import ALREADY_HANDLED_TEXT, register_action
from humblebrag.drafts import STATUS_PENDING, Draft, DraftStore
from humblebrag.preview import ACTION_APPROVE
from humblebrag.publisher import PublishError, SubstackError, SubstackPublisher
from humblebrag.seed_queue import SeedQueue

logger = logging.getLogger(__name__)

ALERT_EVENT = "approve"
IN_FLIGHT_TEXT = "Already publishing this one, hold on."
PUBLISHED_TEXT = "Published: {url}"
FAILED_TEXT = "Publish failed, the draft is still pending. {detail}"
MAY_BE_LIVE_NOTE = " Check Substack before tapping Approve again."


class ApproveService:
    """Handles taps on the Approve button."""

    def __init__(
        self, store: DraftStore, queue: SeedQueue, publisher: SubstackPublisher
    ) -> None:
        """Hold the draft store, the seed queue and the Substack publisher."""
        self._store = store
        self._queue = queue
        self._publisher = publisher
        self._in_flight: set[int] = set()

    async def on_approve_tap(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, draft: Draft
    ) -> None:
        """Publish ``draft``; a double tap never publishes twice."""
        chat = update.effective_chat
        if chat is None:
            return
        if draft.id in self._in_flight:  # checked and set with no await between
            await self._say(context, chat.id, IN_FLIGHT_TEXT)
            return
        self._in_flight.add(draft.id)
        try:
            await self._approve(update, context, chat.id, draft.id)
        finally:
            self._in_flight.discard(draft.id)

    async def _approve(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        draft_id: int,
    ) -> None:
        """Re-read the draft, publish it, then record the outcome."""
        try:
            draft = await asyncio.to_thread(self._store.get, draft_id)
        except Exception as error:  # a failed read must not publish blind
            logger.exception("Reading draft %d before publishing failed", draft_id)
            await self._fail_unexpected(context, chat_id, draft_id, error)
            return
        if draft.status != STATUS_PENDING:
            await self._say(context, chat_id, ALREADY_HANDLED_TEXT)
            return
        try:
            url = await self._publisher.publish(draft.title, draft.subtitle, draft.body)
        except SubstackError as error:  # typed and already alerted by the publisher
            await self._say(context, chat_id, self._failure_text(error))
            return
        except Exception as error:  # an untyped surprise: nothing is known
            logger.exception("Publishing draft %d failed unexpectedly", draft_id)
            await self._fail_unexpected(context, chat_id, draft_id, error)
            return
        await self._record_success(context, chat_id, draft_id, url)
        await self._remove_buttons(update, context, chat_id)

    @staticmethod
    def _failure_text(error: SubstackError) -> str:
        """The message for a typed failure; warns when the post may be live."""
        live = isinstance(error, PublishError) and error.maybe_published
        note = MAY_BE_LIVE_NOTE if live else ""
        return FAILED_TEXT.format(detail=str(error)) + note

    async def _record_success(
        self, context: ContextTypes.DEFAULT_TYPE, chat_id: int, draft_id: int, url: str
    ) -> None:
        """Mark the draft published, then the seed used, then reply with the URL.

        The post is live, so a bookkeeping failure alerts but never hides the URL.
        """
        problems: list[str] = []
        try:
            await asyncio.to_thread(self._store.mark_published, draft_id, url)
        except Exception as error:  # the post is already live
            logger.exception("Marking draft %d published failed", draft_id)
            problems.append(
                f"draft {draft_id} was not marked published (do NOT tap Approve "
                f"again): {describe_error(error)}"
            )
        try:
            await asyncio.to_thread(self._queue.approve, str(draft_id))
        except Exception as error:  # the post is already live
            logger.exception("Marking the seed of draft %d used failed", draft_id)
            problems.append(
                f"the seed of draft {draft_id} was not marked used: "
                f"{describe_error(error)}"
            )
        try:
            await self._say(context, chat_id, PUBLISHED_TEXT.format(url=url))
        except Exception as error:  # the post is live: the URL must still reach Stuart
            logger.exception(
                "Could not send the published reply for draft %d", draft_id
            )
            problems.append(
                f"the reply to you could not be sent: {describe_error(error)}"
            )
        if problems:
            await alert(ALERT_EVENT, f"Published {url} but " + "; and ".join(problems))

    async def _fail_unexpected(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        draft_id: int,
        error: Exception,
    ) -> None:
        """Alert an error that was not a typed Substack failure, and say so."""
        detail = describe_error(error)
        await alert(ALERT_EVENT, f"Approving draft {draft_id} failed: {detail}")
        await self._say(context, chat_id, FAILED_TEXT.format(detail=detail))

    @staticmethod
    async def _say(context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str) -> None:
        """Send ``text`` to the chat."""
        await context.bot.send_message(chat_id=chat_id, text=text)

    @staticmethod
    async def _remove_buttons(
        update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: int
    ) -> None:
        """Best effort: take the buttons off the tapped message (ERR-15)."""
        query = update.callback_query
        message_id = getattr(query.message, "message_id", None) if query else None
        if message_id is None:
            return
        try:
            await context.bot.edit_message_reply_markup(
                chat_id=chat_id, message_id=message_id, reply_markup=None
            )
        except Exception:  # best effort: never mask a completed publish
            logger.exception("Could not remove old buttons")


def install_approve(application: Application, service: ApproveService) -> None:
    """Wire the Approve button to the service."""
    register_action(application, ACTION_APPROVE, service.on_approve_tap)
