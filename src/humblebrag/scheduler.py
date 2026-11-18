"""Daily scheduler: runs the pipeline once a day at ``RUN_TIME`` in ``TZ``.

State lives in a small JSON file on the data volume:
``last_run_date`` (the local date a run was last claimed) and ``skip_next``
(suppresses the next run only, set by ``/skip``).

A run is due when today's scheduled time has passed and today has not been
claimed. So a container that was down at 08:30 runs once when it starts that
day, and never catches up for earlier days. The day is claimed *before* the
run, so a crash cannot cause a second run the same day; a failed run alerts
instead.
"""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from enum import Enum
from pathlib import Path
from zoneinfo import ZoneInfo

from humblebrag.alerts import alert, describe_error
from humblebrag.config import Config
from humblebrag.file_lock import LockTimeoutError, file_lock
from humblebrag.seed import write_atomically

logger = logging.getLogger(__name__)

DEFAULT_STATE_PATH = Path("/data/scheduler-state.json")
LOCK_SUFFIX = ".lock"
POLL_INTERVAL_SECONDS = 30.0
ALERT_EVENT = "scheduler"
JSON_INDENT = 2
ENCODING = "utf-8"

Clock = Callable[[], datetime]
Sleeper = Callable[[float], Awaitable[None]]
Pipeline = Callable[[], Awaitable[None]]


class SchedulerStateError(Exception):
    """The scheduler state file exists but cannot be understood."""


class Claim(Enum):
    """What claiming today's run decided."""

    RUN = "run"
    SKIPPED = "skipped"
    ALREADY_DONE = "already done"


@dataclass(frozen=True)
class SchedulerSnapshot:
    """The persisted scheduler state."""

    last_run_date: date | None
    skip_next: bool


def scheduled_for(day: date, run_time: time, tz: ZoneInfo) -> datetime:
    """The instant ``run_time`` occurs on local ``day`` in ``tz``.

    BST/GMT are handled by the zone rules. A time that does not exist on the
    spring-forward day moves to just after the gap; an ambiguous autumn time
    uses its first occurrence.
    """
    local = datetime.combine(day, run_time, tzinfo=tz)
    return local.astimezone(UTC).astimezone(tz)


def is_due(now: datetime, config: Config) -> bool:
    """True if today's scheduled time (in the configured zone) has passed."""
    local_now = now.astimezone(config.tz)
    return local_now >= scheduled_for(local_now.date(), config.run_time, config.tz)


class SchedulerState:
    """The state file: read, claim today's run, and set the skip flag."""

    def __init__(self, path: Path = DEFAULT_STATE_PATH) -> None:
        """Use ``path`` for state and ``path`` + ``.lock`` for the lock."""
        self._path = path
        self._lock_path = path.with_name(path.name + LOCK_SUFFIX)

    def read(self) -> SchedulerSnapshot:
        """Return the saved state (defaults if nothing is saved yet)."""
        if not self._path.exists():
            return SchedulerSnapshot(None, False)
        try:
            data = json.loads(self._path.read_text(encoding=ENCODING))
            raw_date = data["last_run_date"]
            skip_next = data["skip_next"]
            last_run = None if raw_date is None else date.fromisoformat(raw_date)
        except (OSError, ValueError, KeyError, TypeError):
            raise SchedulerStateError(
                f"{self._path.name} cannot be read as scheduler state"
            ) from None
        if not isinstance(skip_next, bool):
            raise SchedulerStateError(f"{self._path.name} has a non-boolean skip_next")
        return SchedulerSnapshot(last_run, skip_next)

    def claim(self, today: date) -> Claim:
        """Atomically decide whether today's run happens, and record the day.

        ALREADY_DONE if today was already claimed. Otherwise today is recorded
        and, if the skip flag was set, it is cleared and SKIPPED is returned.
        """
        with file_lock(self._lock_path):
            current = self.read()
            if current.last_run_date == today:
                return Claim.ALREADY_DONE
            self._write(SchedulerSnapshot(today, False))
            return Claim.SKIPPED if current.skip_next else Claim.RUN

    def set_skip_next(self, skip: bool) -> None:
        """Set or clear the flag that suppresses the next run only."""
        with file_lock(self._lock_path):
            current = self.read()
            self._write(SchedulerSnapshot(current.last_run_date, skip))

    def _write(self, snapshot: SchedulerSnapshot) -> None:
        """Atomically save ``snapshot``."""
        last = snapshot.last_run_date
        content = json.dumps(
            {
                "last_run_date": None if last is None else last.isoformat(),
                "skip_next": snapshot.skip_next,
            },
            indent=JSON_INDENT,
        )
        write_atomically(self._path, content + "\n")


class Scheduler:
    """Polls the clock and runs the pipeline once a day."""

    def __init__(
        self,
        config: Config,
        state: SchedulerState,
        run: Pipeline,
        clock: Clock | None = None,
        sleep: Sleeper = asyncio.sleep,
    ) -> None:
        """Hold the config, state file and pipeline; clock and sleep are injectable."""
        self._config = config
        self._state = state
        self._run = run
        self._clock = clock or (lambda: datetime.now(UTC))
        self._sleep = sleep
        self._last_error: str | None = None

    async def tick(self) -> Claim | None:
        """Run the pipeline if due. Returns the claim made, or None if not due."""
        now = self._clock()
        if not is_due(now, self._config):
            return None
        today = now.astimezone(self._config.tz).date()
        claim = await asyncio.to_thread(self._state.claim, today)
        if claim is Claim.SKIPPED:
            logger.info("Today's run skipped by /skip")
        elif claim is Claim.RUN:
            await self._run_pipeline()
        return claim

    async def run_forever(self) -> None:
        """Tick every ``POLL_INTERVAL_SECONDS`` until cancelled.

        A failing tick is logged and alerted, never fatal. The same error is
        only alerted once until a tick succeeds again.
        """
        while True:
            try:
                await self.tick()
            except (SchedulerStateError, LockTimeoutError, OSError) as error:
                await self._report(describe_error(error))
            else:
                self._last_error = None
            await self._sleep(POLL_INTERVAL_SECONDS)

    async def _run_pipeline(self) -> None:
        """Run the pipeline; a failure alerts Stuart and does not stop the loop."""
        try:
            await self._run()
        except Exception as error:  # the daily run must never kill the scheduler
            logger.exception("Daily run failed")
            await alert(ALERT_EVENT, f"Daily run failed: {describe_error(error)}")

    async def _report(self, description: str) -> None:
        """Log and alert a scheduler failure, once per distinct description."""
        logger.error("Scheduler tick failed: %s", description)
        if description != self._last_error:
            self._last_error = description
            await alert(
                ALERT_EVENT, f"Scheduler could not check or claim a run: {description}"
            )
