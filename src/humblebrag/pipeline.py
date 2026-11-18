"""The daily run: seed -> generate -> guard -> save draft -> send preview.

Nothing here publishes: publishing only happens on Approve (SA-34). If any step
fails the seed reservation is released, Stuart is alerted with the step name,
and ``run()`` returns None instead of raising, so the scheduler (which alerts
on exceptions) never alerts a second time for the same failure.

The seed is reserved before the draft exists, under a provisional key, and
re-keyed to ``str(draft.id)`` once the draft is saved, which is the key the
Approve and Bin handlers use.
"""

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from humblebrag.alerts import alert, describe_error
from humblebrag.drafts import Draft, DraftStore, RunGate
from humblebrag.guard import CheckedPost, GuardError, GuardRejectedError
from humblebrag.seed import Seed
from humblebrag.seed_provider import SeedGenerationError
from humblebrag.seed_queue import SeedQueue

logger = logging.getLogger(__name__)

ALERT_EVENT = "run"
STEP_GATE = "check pending drafts"
STEP_SEED = "pick seed"
STEP_GENERATE = "generate post"
STEP_GUARD = "safety guard"
STEP_SAVE = "save draft"
STEP_PREVIEW = "send preview"
PROVISIONAL_PREFIX = "run-"

SeedFor = Callable[[str], Awaitable[Seed]]
GenerateChecked = Callable[[str, str | None], Awaitable[CheckedPost]]
SendPreview = Callable[[Draft, str | None], Awaitable[None]]

# These services alert for themselves, naming the step, before they raise.
_ALREADY_ALERTED = (SeedGenerationError, GuardRejectedError, GuardError)


@dataclass(frozen=True)
class _Step:
    """The step a failure happened in, and whether it was already alerted."""

    name: str
    alerted: bool = False


class RunPipeline:
    """Runs one daily (or ``/now``) run from seed to preview."""

    def __init__(
        self,
        gate: RunGate,
        store: DraftStore,
        queue: SeedQueue,
        seed_for: SeedFor,
        generate: GenerateChecked,
        send_preview: SendPreview,
        max_pending: int,
    ) -> None:
        """Hold the collaborators; all are injected so tests can mock them."""
        self._gate = gate
        self._store = store
        self._queue = queue
        self._seed_for = seed_for
        self._generate = generate
        self._send_preview = send_preview
        self._max_pending = max_pending

    async def run(self) -> Draft | None:
        """Run once. Returns the draft that was previewed, or None.

        None means nothing was sent for approval: the run was paused (too many
        pending drafts) or a step failed (already alerted, seed released).
        """
        try:
            allowed = await self._gate.allow_run()
        except Exception as error:  # a broken gate must not become a silent skip
            logger.exception("Run gate failed")
            await self._alert(STEP_GATE, error)
            return None
        if not allowed:
            return None
        key = PROVISIONAL_PREFIX + uuid.uuid4().hex
        draft: Draft | None = None
        step = _Step(STEP_SEED)
        try:
            seed = await self._seed_for(key)
            step = _Step(STEP_GENERATE)
            checked = await self._generate(seed.text, None)
            step = _Step(STEP_SAVE)
            draft = await self._save(seed.text, checked, key)
            step = _Step(STEP_PREVIEW)
            await self._send_preview(draft, checked.guard.preview_line())
        except asyncio.CancelledError:
            await self._release(key, draft)  # shutting down: do not strand the seed
            raise
        except Exception as error:
            step = self._classify(step, error)
            logger.exception("Run failed at step: %s", step.name)
            await self._release(key, draft)
            if not step.alerted:
                await self._alert(step.name, error)
            return None
        return draft

    @staticmethod
    def _classify(step: _Step, error: Exception) -> _Step:
        """Name the failing step; guard errors come out of the generate call."""
        if isinstance(error, GuardRejectedError | GuardError):
            return _Step(STEP_GUARD, alerted=True)
        if isinstance(error, SeedGenerationError):
            return _Step(STEP_SEED, alerted=True)
        return step

    async def _save(self, seed_text: str, checked: CheckedPost, key: str) -> Draft:
        """Store the draft, then move the seed reservation to the draft's id."""
        post = checked.post
        draft = await asyncio.to_thread(
            self._store.create,
            seed_text,
            post.title,
            post.subtitle,
            post.body_markdown,
            None,
            self._max_pending,
        )
        try:
            await asyncio.to_thread(self._queue.rekey, key, str(draft.id))
        except Exception:
            # Without the re-key Approve could not find the seed: discard the draft.
            await asyncio.to_thread(self._store.mark_binned, draft.id)
            raise
        return draft

    async def _release(self, key: str, draft: Draft | None) -> None:
        """Release the seed and bin any saved draft. Never raises.

        A cleanup failure is alerted too, because a stuck reservation or a
        pending draft nobody has seen needs a human.
        """
        reservation = key if draft is None else str(draft.id)
        try:
            await asyncio.to_thread(self._queue.bin_draft, reservation)
            if draft is not None:
                await asyncio.to_thread(self._store.mark_binned, draft.id)
        except Exception as error:  # cleanup must not hide the original failure
            logger.exception("Releasing the seed after a failed run failed")
            await self._alert("release seed", error)

    @staticmethod
    async def _alert(step: str, error: BaseException) -> None:
        """Alert Stuart with the step name and the error."""
        await alert(
            ALERT_EVENT, f"Run failed at step '{step}': {describe_error(error)}"
        )
