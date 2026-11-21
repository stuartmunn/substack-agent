"""Approve handler: one tap hands the post to Substack as a draft.

The agent never calls Substack's publish endpoint; Stuart publishes from the
editor link it sends. Order: create the Substack draft and save its ID straight
away (so a retry updates it instead of creating a second one), mark the local
draft ``handed_over``, mark the seed used, then reply with the editor link. A
failed create leaves the draft pending with its buttons active; once the
Substack draft exists, a failure in any later step alerts with the draft ID and
link and never hides the link. After the link, a draft's Note teaser is sent
alone in its own message so Stuart can copy it; Stuart creates the Note by hand
(the agent never touches Notes). After the teaser, the Gemini image prompt is
sent the same way. A failed teaser or prompt send alerts and rolls nothing back.
"""

import asyncio
import logging
from collections.abc import Awaitable

from telegram import Update
from telegram.ext import Application, ContextTypes

from humblebrag.alerts import alert, describe_error
from humblebrag.buttons import ALREADY_HANDLED_TEXT, register_action
from humblebrag.drafts import STATUS_PENDING, Draft, DraftStore
from humblebrag.image_prompt import build_image_prompt
from humblebrag.preview import ACTION_APPROVE
from humblebrag.publisher import DraftError, SubstackError, SubstackPublisher
from humblebrag.seed_queue import SeedQueue

logger = logging.getLogger(__name__)

ALERT_EVENT = "approve"
STEP_CREATE = "create Substack draft"
STEP_UPDATE = "update Substack draft"
STEP_SAVE_ID = "save Substack draft ID"
STEP_STATUS = "save handed-over status"
STEP_SEED = "mark seed used"
STEP_REPLY = "send reply"
STEP_TEASER = "send Note teaser"
STEP_IMAGE_PROMPT = "send image prompt"
IN_FLIGHT_TEXT = "Already handing this one to Substack, hold on."
HANDED_OVER_TEXT = (
    "Draft ready in Substack. Open it, check it and publish it yourself: {url}"
)
FAILED_TEXT = "Could not hand the draft to Substack, it is still pending. {detail}"


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
        # Substack draft IDs created by this process, by local draft id: a retry
        # updates the draft even if saving the ID or status in the store failed.
        self._created: dict[int, str] = {}
        # Drafts whose teaser this process has sent while the store still says
        # pending, so a retry cannot send it twice; cleared once handed over.
        self._teaser_sent: set[int] = set()
        self._image_prompt_sent: set[int] = set()

    async def on_approve_tap(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, draft: Draft
    ) -> None:
        """Hand ``draft`` to Substack; a double tap never creates two drafts."""
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
        """Re-read the draft, create or update its Substack draft, record it."""
        try:
            draft = await asyncio.to_thread(self._store.get, draft_id)
        except Exception as error:  # a failed read must not create a draft blind
            logger.exception("Reading draft %d before the handover failed", draft_id)
            await self._fail_unexpected(context, chat_id, draft_id, error)
            return
        if draft.status != STATUS_PENDING:
            await self._say(context, chat_id, ALREADY_HANDLED_TEXT)
            return
        known_id = draft.substack_draft_id or self._created.get(draft_id)
        substack_id = await self._create_or_update(context, chat_id, draft, known_id)
        if substack_id is None:
            return
        await self._record_handover(
            context,
            chat_id,
            draft_id,
            substack_id,
            new=known_id is None,
            teaser=draft.note_teaser,
            image_scene=draft.image_scene,
        )
        await self._remove_buttons(update, context, chat_id)

    async def _create_or_update(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        draft: Draft,
        known_id: str | None,
    ) -> str | None:
        """Create the Substack draft (or update ``known_id``); None on failure.

        A failure alerts with the step name and cause and tells Stuart; the
        local draft stays pending with its buttons active.
        """
        step = STEP_CREATE if known_id is None else STEP_UPDATE
        try:
            if known_id is None:
                substack_id = await self._publisher.create_draft(
                    draft.title, draft.subtitle, draft.body
                )
                self._created[draft.id] = substack_id  # remember at once (IDEM-01)
            else:
                await self._publisher.update_draft(
                    known_id, draft.title, draft.subtitle, draft.body
                )
                substack_id = known_id
        except Exception as error:
            logger.exception("Step '%s' failed for draft %d", step, draft.id)
            if isinstance(error, DraftError) or not isinstance(error, SubstackError):
                where = "" if known_id is None else self._where(known_id)
                await alert(
                    ALERT_EVENT,
                    f"Step '{step}' failed for draft {draft.id}: "
                    f"{describe_error(error)}{where}",
                )  # session errors were already alerted by the publisher
            await self._say(
                context, chat_id, FAILED_TEXT.format(detail=describe_error(error))
            )
            return None
        return substack_id

    def _where(self, substack_id: str) -> str:
        """The draft ID and editor link, for alerts."""
        return (
            f" (Substack draft {substack_id}, "
            f"{self._publisher.editor_url(substack_id)})"
        )

    async def _record_handover(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        draft_id: int,
        substack_id: str,
        *,
        new: bool,
        teaser: str,
        image_scene: str,
    ) -> None:
        """Save the ID (if new), mark the draft handed over, mark the seed used,
        reply with the editor link, then send the Note teaser and the image prompt
        (each only if the draft has one).

        The Substack draft exists, so a bookkeeping failure alerts with the step,
        draft ID and link but never hides the link or skips the later steps.
        """
        url = self._publisher.editor_url(substack_id)
        problems: list[str] = []
        if new:
            await self._step(
                problems,
                STEP_SAVE_ID,
                asyncio.to_thread(
                    self._store.set_substack_draft_id, draft_id, substack_id
                ),
            )
        stored = await self._step(
            problems,
            STEP_STATUS,
            asyncio.to_thread(self._store.mark_handed_over, draft_id, substack_id, url),
        )
        if stored:
            self._created.pop(draft_id, None)  # the store now records it
        await self._step(
            problems, STEP_SEED, asyncio.to_thread(self._queue.approve, str(draft_id))
        )
        await self._step(
            problems,
            STEP_REPLY,
            self._say(context, chat_id, HANDED_OVER_TEXT.format(url=url)),
        )
        if teaser and draft_id not in self._teaser_sent:
            await self._step(
                problems,
                STEP_TEASER,
                self._send_teaser(context, chat_id, draft_id, teaser),
            )
        if image_scene and draft_id not in self._image_prompt_sent:
            await self._step(
                problems,
                STEP_IMAGE_PROMPT,
                self._send_image_prompt(context, chat_id, draft_id, image_scene),
            )
        if stored:  # the status now blocks a repeat
            self._teaser_sent.discard(draft_id)
            self._image_prompt_sent.discard(draft_id)
        if problems:
            await alert(
                ALERT_EVENT,
                f"Substack draft {substack_id} exists ({url}) for draft {draft_id}, "
                "but " + "; and ".join(problems),
            )

    async def _send_teaser(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        draft_id: int,
        teaser: str,
    ) -> None:
        """Send the teaser alone, and remember it was sent."""
        await self._say(context, chat_id, teaser)
        self._teaser_sent.add(draft_id)

    async def _send_image_prompt(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        draft_id: int,
        image_scene: str,
    ) -> None:
        """Send the Gemini prompt alone, and remember it was sent."""
        text = await asyncio.to_thread(build_image_prompt, image_scene)
        sent = await context.bot.send_message(chat_id=chat_id, text=text)
        self._image_prompt_sent.add(draft_id)
        # So a reply to this message can be matched to the draft, even after a restart.
        await asyncio.to_thread(
            self._store.set_image_prompt_message_id, draft_id, sent.message_id
        )

    @staticmethod
    async def _step(problems: list[str], step: str, work: Awaitable[object]) -> bool:
        """Await ``work``; on failure log it and add "step 'x' failed: cause"."""
        try:
            await work
        except Exception as error:  # the Substack draft exists: keep going
            logger.exception("Step '%s' failed after the Substack draft was made", step)
            problems.append(f"step '{step}' failed: {describe_error(error)}")
            return False
        return True

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
        except Exception:  # best effort: never mask a completed handover
            logger.exception("Could not remove old buttons")


def install_approve(application: Application, service: ApproveService) -> None:
    """Wire the Approve button to the service."""
    register_action(application, ACTION_APPROVE, service.on_approve_tap)
