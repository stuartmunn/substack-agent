"""Telegram commands: /seed, /seeds, /skip and /now.

They let Stuart manage the queue and runs from the chat. The chat guard in
``bot`` already drops every other chat, so nothing here repeats that check.
``/now`` runs the pipeline in the background and never touches the daily
schedule's state; ``/skip`` only sets the scheduler's skip flag.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from humblebrag.alerts import alert, describe_error
from humblebrag.config import Config
from humblebrag.drafts import Draft
from humblebrag.scheduler import SchedulerState
from humblebrag.seed_queue import InvalidSeedError, SeedQueue

logger = logging.getLogger(__name__)

ALERT_EVENT = "command"
SEEDS_SHOWN = 20
SEED_PREVIEW_LENGTH = 120
ELLIPSIS = "…"

SEED_USAGE = "Say what the seed is about: /seed <text>"
SEED_ADDED = "Added to the queue ({count} unused): {text}"
SEED_REJECTED = "Not added. {reason}"
SEEDS_EMPTY = "No unused seeds. The next run will invent one."
SEEDS_HEADER = "{count} unused seeds, in order:"
SEEDS_MORE = "...and {more} more"
SKIPPED = "Skipping the run on {day}. Later runs are unaffected."
NOW_STARTED = "Starting a run now."
NOW_BUSY = "A run is already in progress."
NOW_NO_DRAFT = "That run produced no draft. Any problem has been alerted."
FAILED = "That did not work and I've alerted you."

RunPipelineOnce = Callable[[], Awaitable[Draft | None]]


class RunCoordinator:
    """Runs the pipeline one at a time, shared by the scheduler and ``/now``."""

    def __init__(self, run: RunPipelineOnce) -> None:
        """Wrap the pipeline's ``run``."""
        self._run = run
        self._lock = asyncio.Lock()

    async def run_scheduled(self) -> None:
        """Run once, waiting for any run already in progress."""
        async with self._lock:
            await self._run()

    async def try_run_now(self) -> tuple[bool, Draft | None]:
        """Run once unless a run is in progress: (started, draft)."""
        if self._lock.locked():
            return False, None
        async with self._lock:
            return True, await self._run()


def _shorten(text: str) -> str:
    """Cut a long seed so a list of them fits in one message."""
    if len(text) <= SEED_PREVIEW_LENGTH:
        return text
    return text[: SEED_PREVIEW_LENGTH - len(ELLIPSIS)] + ELLIPSIS


class CommandService:
    """Handles the four commands."""

    def __init__(
        self,
        queue: SeedQueue,
        state: SchedulerState,
        config: Config,
        coordinator: RunCoordinator,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """Hold the collaborators; ``clock`` is injectable for tests."""
        self._queue = queue
        self._state = state
        self._config = config
        self._coordinator = coordinator
        self._clock = clock or (lambda: datetime.now(UTC))

    async def on_seed(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """``/seed <text>``: append an unused seed and confirm."""
        text = " ".join(context.args or []).strip()
        if not text:
            await self._reply(update, SEED_USAGE)
            return
        try:
            added = await asyncio.to_thread(self._queue.add_seed, text)
        except InvalidSeedError as error:
            await self._reply(update, SEED_REJECTED.format(reason=error))
            return
        except Exception as error:  # a failed command must never kill the bot
            logger.exception("Command /seed failed")
            await self._fail(update, "/seed", error)
            return
        await self._reply(
            update, SEED_ADDED.format(count=added.unused_count, text=added.text)
        )

    async def on_seeds(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """``/seeds``: list the first unused seeds and the total."""
        try:
            texts = await asyncio.to_thread(self._queue.unused_texts)
        except Exception as error:  # a failed command must never kill the bot
            logger.exception("Command /seeds failed")
            await self._fail(update, "/seeds", error)
            return
        if not texts:
            await self._reply(update, SEEDS_EMPTY)
            return
        lines = [SEEDS_HEADER.format(count=len(texts))]
        lines.extend(
            f"{number}. {_shorten(text)}"
            for number, text in enumerate(texts[:SEEDS_SHOWN], start=1)
        )
        if len(texts) > SEEDS_SHOWN:
            lines.append(SEEDS_MORE.format(more=len(texts) - SEEDS_SHOWN))
        await self._reply(update, "\n".join(lines))

    async def on_skip(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """``/skip``: suppress the next scheduled run only, and say which day."""
        try:
            day = await asyncio.to_thread(self._skip_next)
        except Exception as error:  # a failed command must never kill the bot
            logger.exception("Command /skip failed")
            await self._fail(update, "/skip", error)
            return
        await self._reply(update, SKIPPED.format(day=day.isoformat()))

    async def on_now(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """``/now``: start a run in the background, leaving the schedule alone."""
        context.application.create_task(self._run_now(update))
        await self._reply(update, NOW_STARTED)

    def _skip_next(self) -> date:
        """Set the skip flag and return the date it applies to."""
        today = self._clock().astimezone(self._config.tz).date()
        return self._state.skip_next_run(today)

    async def _run_now(self, update: Update) -> None:
        """Run in the background; every outcome reaches the chat or an alert."""
        try:
            started, draft = await self._coordinator.try_run_now()
            if not started:
                await self._reply(update, NOW_BUSY)
            elif draft is None:
                await self._reply(update, NOW_NO_DRAFT)
        except Exception as error:  # nothing awaits this task (ERR-16)
            logger.exception("Command /now failed")
            await self._fail(update, "/now", error)

    async def _fail(self, update: Update, command: str, error: Exception) -> None:
        """Alert a failed command (the caller logged it), and tell the chat."""
        await alert(ALERT_EVENT, f"{command} failed: {describe_error(error)}")
        try:
            await self._reply(update, FAILED)
        except Exception:  # the alert already went out (ERR-21)
            logger.exception("Could not tell the chat that %s failed", command)

    @staticmethod
    async def _reply(update: Update, text: str) -> None:
        """Reply in the chat the command came from."""
        message = update.effective_message
        if message is not None:
            await message.reply_text(text)


def install_commands(application: Application, service: CommandService) -> None:
    """Route the four commands to the service."""
    application.add_handler(CommandHandler("seed", service.on_seed))
    application.add_handler(CommandHandler("seeds", service.on_seeds))
    application.add_handler(CommandHandler("skip", service.on_skip))
    application.add_handler(CommandHandler("now", service.on_now))
