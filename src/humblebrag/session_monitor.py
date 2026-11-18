"""Daily Substack session check, shortly before the daily run.

At ``CHECK_TIME`` (08:00 local by default, ahead of the 08:30 run) the session
is checked once. The day is claimed in a small JSON state file *before* the
check, so a restart or a failing check can never produce a second check, and so
a second alert, the same day. A failed check alerts Stuart through
``SubstackPublisher.ensure_session``; the monitor never alerts twice for it.
"""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, time
from pathlib import Path

from humblebrag.alerts import alert, describe_error
from humblebrag.config import Config
from humblebrag.file_lock import LockTimeoutError, file_lock
from humblebrag.publisher import SubstackError
from humblebrag.scheduler import scheduled_for
from humblebrag.seed import write_atomically

logger = logging.getLogger(__name__)

CHECK_TIME = time(8, 0)
DEFAULT_STATE_PATH = Path("/data/session-monitor-state.json")
POLL_INTERVAL_SECONDS = 30.0
ALERT_EVENT = "session monitor"
ENCODING = "utf-8"
JSON_INDENT = 2

Check = Callable[[], Awaitable[object]]
Clock = Callable[[], datetime]
Sleeper = Callable[[float], Awaitable[None]]


class MonitorStateError(Exception):
    """The monitor's state file could not be read or written."""


class MonitorState:
    """Remembers the last local date the session was checked."""

    def __init__(self, path: Path = DEFAULT_STATE_PATH) -> None:
        """Use ``path`` for state and ``path`` + ``.lock`` for the lock."""
        self._path = path
        self._lock_path = path.with_name(path.name + ".lock")

    def last_checked(self) -> date | None:
        """The saved date, or None if nothing is saved yet."""
        if not self._path.exists():
            return None
        try:
            raw = json.loads(self._path.read_text(encoding=ENCODING))["last_check_date"]
            return None if raw is None else date.fromisoformat(raw)
        except (OSError, ValueError, KeyError, TypeError):
            raise MonitorStateError(
                f"{self._path.name} cannot be read as monitor state"
            ) from None

    def claim(self, today: date) -> bool:
        """Atomically record ``today``; False if it was already recorded."""
        try:
            with file_lock(self._lock_path):
                if self.last_checked() == today:
                    return False
                content = json.dumps(
                    {"last_check_date": today.isoformat()}, indent=JSON_INDENT
                )
                write_atomically(self._path, content + "\n")
                return True
        except (OSError, LockTimeoutError):
            raise MonitorStateError(f"{self._path.name} cannot be written") from None


class SessionMonitor:
    """Checks the Substack session once a day at ``check_time``."""

    def __init__(
        self,
        config: Config,
        state: MonitorState,
        check: Check,
        clock: Clock | None = None,
        sleep: Sleeper = asyncio.sleep,
        check_time: time = CHECK_TIME,
    ) -> None:
        """``check`` is ``SubstackPublisher.ensure_session`` (it alerts on failure)."""
        self._config = config
        self._state = state
        self._check = check
        self._clock = clock or (lambda: datetime.now(UTC))
        self._sleep = sleep
        self._check_time = check_time
        self._last_error: str | None = None

    def _today_if_due(self) -> date | None:
        """Today's local date if the check time has passed, else None."""
        local_now = self._clock().astimezone(self._config.tz)
        due = scheduled_for(local_now.date(), self._check_time, self._config.tz)
        return local_now.date() if local_now >= due else None

    async def tick(self) -> bool:
        """Check the session if due and not yet checked today. True if it ran."""
        today = self._today_if_due()
        if today is None:
            return False
        if not await asyncio.to_thread(self._state.claim, today):
            return False
        try:
            await self._check()
        except SubstackError:
            logger.warning("Daily Substack session check failed (already alerted)")
        except Exception as error:  # the monitor must never die from one check
            logger.exception("Daily Substack session check crashed")
            await alert(
                ALERT_EVENT, f"Daily session check crashed: {describe_error(error)}"
            )
        return True

    async def mark_checked_if_due(self) -> None:
        """Count a check made just now (at startup) as today's, if it is past
        the check time, so the monitor does not alert a second time."""
        today = self._today_if_due()
        if today is None:
            return
        try:
            await asyncio.to_thread(self._state.claim, today)
        except MonitorStateError:
            # Not fatal: run_forever hits the same fault on its first tick and
            # alerts once, so the daily loop must still start.
            logger.exception("Could not record the startup session check")

    async def run_forever(self) -> None:
        """Tick every ``POLL_INTERVAL_SECONDS`` until cancelled.

        A failing tick (e.g. unreadable state file) is logged and alerted once
        per distinct error until a tick succeeds again.
        """
        while True:
            try:
                await self.tick()
            except Exception as error:  # one bad tick must not stop the monitor
                logger.exception("Session monitor tick failed")
                await self._report(describe_error(error))
            else:
                self._last_error = None
            await self._sleep(POLL_INTERVAL_SECONDS)

    async def _report(self, description: str) -> None:
        """Alert a monitor failure, once per distinct description."""
        if description != self._last_error:
            self._last_error = description
            await alert(
                ALERT_EVENT, f"Session monitor could not run a check: {description}"
            )
