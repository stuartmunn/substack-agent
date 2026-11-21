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


@dataclass
class _RunState:
    """What one run has created so far, for cleanup."""

    key: str
    draft: Draft | None = None


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
        state = _RunState(PROVISIONAL_PREFIX + uuid.uuid4().hex)
        step = _Step(STEP_SEED)
        try:
            seed = await self._seed_for(state.key)
            step = _Step(STEP_GENERATE)
            checked = await self._generate(seed.text, None)
            step = _Step(STEP_SAVE)
            await self._save(state, seed.text, checked)
            draft = state.draft
            assert draft is not None
            step = _Step(STEP_PREVIEW)
            await self._send_preview(draft, checked.guard.preview_line())
        except asyncio.CancelledError:
            await self._release(state)  # shutting down: do not strand the seed
            raise
        except Exception as error:
            step = self._classify(step, error)
            logger.exception("Run failed at step: %s", step.name)
            await self._release(state)
            if not step.alerted:
                await self._alert(step.name, error)
            return None
        return draft

    @staticmethod
    def _classify(step: _Step, error: Exception) -> _Step:
        """Name the failing step; guard errors come out of the generate call.

        Guard rejections, guard errors and seed-generation failures were already
        alerted by their own services (naming the step), so they are not
        alerted again.
        """
        if isinstance(error, GuardRejectedError | GuardError):
            return _Step(STEP_GUARD, alerted=True)
        if isinstance(error, SeedGenerationError):
            return _Step(STEP_SEED, alerted=True)
        return step

    async def _save(
        self, state: "_RunState", seed_text: str, checked: CheckedPost
    ) -> None:
        """Store the draft, then move the seed reservation to the draft's id.

        The draft is recorded in ``state`` as soon as it exists (even if the run
        is cancelled while the write is in flight), so cleanup can always find it.
        """
        post = checked.post
        task = asyncio.ensure_future(
            asyncio.to_thread(
                self._store.create,
                seed_text,
                post.title,
                post.subtitle,
                post.body_markdown,
                None,
                self._max_pending,
                post.note_teaser,
            )
        )
        try:
            state.draft = await asyncio.shield(task)
        except asyncio.CancelledError:
            # A thread cannot be interrupted: let the write finish, then keep
            # the draft so the cleanup that follows can bin it.
            await asyncio.wait({task})
            if not task.cancelled() and task.exception() is None:
                state.draft = task.result()
            raise
        await asyncio.to_thread(self._queue.rekey, state.key, str(state.draft.id))

    async def _release(self, state: "_RunState") -> None:
        """Release the seed and bin any saved draft. Never raises.

        The reservation may sit under the provisional key or, after the re-key,
        under the draft id (a cancelled re-key can finish late), so both are
        released; the file lock orders them against any late re-key. A cleanup
        failure is alerted too, because a stuck reservation or a pending draft
        nobody has seen needs a human.
        """
        keys = [state.key]
        if state.draft is not None:
            keys.append(str(state.draft.id))
        for key in keys:  # each release is guarded alone: one failure skips none
            try:
                await asyncio.to_thread(self._queue.bin_draft, key)
            except Exception as error:  # cleanup must not hide the original failure
                logger.exception("Releasing the seed after a failed run failed")
                await self._alert("release seed", error)
        if state.draft is not None:
            try:
                await asyncio.to_thread(self._store.mark_binned, state.draft.id)
            except Exception as error:  # the draft stays pending: a human must act
                logger.exception("Could not bin draft %d", state.draft.id)
                await self._alert("discard draft", error)

    @staticmethod
    async def _alert(step: str, error: BaseException) -> None:
        """Alert Stuart with the step name and the error."""
        await alert(
            ALERT_EVENT, f"Run failed at step '{step}': {describe_error(error)}"
        )
