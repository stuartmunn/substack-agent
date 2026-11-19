"""Daily backup of ``seed.md`` and the SQLite database to ``/data/backups/``.

Each day's backup is a folder named for its local date, holding a copy of
``seed.md`` and a consistent snapshot of the database (taken with SQLite's own
backup API, so a write in flight cannot tear it). The folder is built under a
temporary name and renamed into place, so a dated folder is always complete:
its existence is the record that the day was done, and a failed attempt leaves
nothing behind to hide the failure from the retry. The newest ``KEEP_BACKUPS``
folders are kept.

After 03:00 local time the job tries once per ``POLL_INTERVAL_SECONDS`` until
today's folder exists. A failure alerts Stuart once per distinct error until a
tick succeeds again.
"""

import asyncio
import logging
import os
import re
import shutil
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from pathlib import Path

from humblebrag.alerts import alert, describe_error
from humblebrag.config import Config
from humblebrag.scheduler import scheduled_for

logger = logging.getLogger(__name__)

BACKUPS_DIRNAME = "backups"
SEED_FILENAME = "seed.md"
DB_FILENAME = "humblebrag.db"
BACKUP_TIME = time(3, 0)
KEEP_BACKUPS = 14
POLL_INTERVAL_SECONDS = 600.0
SQLITE_TIMEOUT_SECONDS = 10.0
ALERT_EVENT = "backup"
INTEGRITY_OK = "ok"
_PARTIAL_SUFFIX = ".partial"
_DATED_DIR = re.compile(r"^\d{4}-\d{2}-\d{2}$")

Clock = Callable[[], datetime]
Sleeper = Callable[[float], Awaitable[None]]


class BackupError(Exception):
    """A backup or its pruning failed; the message holds no secrets."""


@dataclass(frozen=True)
class BackupOutcome:
    """What one tick did: the folder made (None if nothing was due) and any
    problem removing old backups."""

    made: Path | None
    prune_error: BackupError | None = None


class BackupService:
    """Makes and prunes the dated backup folders under the data directory."""

    def __init__(self, data_dir: Path, keep: int = KEEP_BACKUPS) -> None:
        """Back up ``data_dir``'s seed file and database, keeping ``keep`` days."""
        self._data_dir = data_dir
        self._backups_dir = data_dir / BACKUPS_DIRNAME
        self._keep = keep

    def exists_for(self, day: date) -> bool:
        """True if ``day``'s backup folder is already in place."""
        return (self._backups_dir / day.isoformat()).is_dir()

    def back_up(self, day: date) -> Path:
        """Make ``day``'s backup and return its folder. Blocking.

        Raises BackupError if the database is missing or unreadable, a copy
        fails, or the copied database does not pass SQLite's integrity check.
        A missing ``seed.md`` is skipped with a warning (a fresh install has
        none until its first seed).
        """
        final = self._backups_dir / day.isoformat()
        partial = self._backups_dir / (day.isoformat() + _PARTIAL_SUFFIX)
        try:
            self._backups_dir.mkdir(parents=True, exist_ok=True)
            shutil.rmtree(partial, ignore_errors=True)  # a crashed earlier attempt
            partial.mkdir()
            self._copy_seed(partial)
            self._copy_database(partial)
            os.replace(partial, final)
        except BackupError:
            shutil.rmtree(partial, ignore_errors=True)
            raise
        except OSError as error:
            shutil.rmtree(partial, ignore_errors=True)
            raise BackupError(f"Could not write {final.name}: {error}") from error
        logger.info("Backed up seed.md and the database to %s", final)
        return final

    def prune(self) -> list[Path]:
        """Delete all but the newest ``keep`` dated folders; return those removed.

        Only folders named like a date are touched. Raises BackupError if one
        cannot be removed (the others are still attempted).
        """
        try:
            dated = sorted(
                path
                for path in self._backups_dir.iterdir()
                if path.is_dir() and _DATED_DIR.match(path.name)
            )
        except OSError as error:
            raise BackupError(f"Could not list backups: {error}") from error
        removed: list[Path] = []
        failures: list[str] = []
        for path in dated[: max(len(dated) - self._keep, 0)]:
            try:
                shutil.rmtree(path)
                removed.append(path)
            except OSError as error:
                failures.append(f"{path.name}: {error}")
        if failures:
            raise BackupError("Could not remove old backups: " + "; ".join(failures))
        return removed

    def _copy_seed(self, destination: Path) -> None:
        """Copy ``seed.md`` into ``destination`` if there is one."""
        source = self._data_dir / SEED_FILENAME
        if not source.exists():
            logger.warning("No %s to back up", SEED_FILENAME)
            return
        shutil.copy2(source, destination / SEED_FILENAME)

    def _copy_database(self, destination: Path) -> None:
        """Snapshot the database into ``destination`` and check the copy."""
        source = self._data_dir / DB_FILENAME
        if not source.exists():
            raise BackupError(f"{DB_FILENAME} does not exist")
        target = destination / DB_FILENAME
        try:
            origin = sqlite3.connect(
                f"{source.as_uri()}?mode=ro", uri=True, timeout=SQLITE_TIMEOUT_SECONDS
            )
            try:
                copy = sqlite3.connect(target, timeout=SQLITE_TIMEOUT_SECONDS)
                try:
                    origin.backup(copy)
                    verdict = copy.execute("PRAGMA integrity_check").fetchone()
                finally:
                    copy.close()
            finally:
                origin.close()
        except sqlite3.Error as error:
            raise BackupError(f"Could not copy {DB_FILENAME}: {error}") from error
        if verdict is None or verdict[0] != INTEGRITY_OK:
            raise BackupError(f"The copy of {DB_FILENAME} failed its integrity check")


class BackupJob:
    """Runs the daily backup after ``BACKUP_TIME`` and alerts on any failure."""

    def __init__(
        self,
        config: Config,
        service: BackupService,
        clock: Clock | None = None,
        sleep: Sleeper = asyncio.sleep,
        backup_time: time = BACKUP_TIME,
    ) -> None:
        """Hold the config (for the time zone) and the service; clock and sleep
        are injectable for tests."""
        self._config = config
        self._service = service
        self._clock = clock or (lambda: datetime.now(UTC))
        self._sleep = sleep
        self._backup_time = backup_time
        self._last_error: str | None = None

    async def tick(self) -> BackupOutcome:
        """Back up and prune if it is past backup time and today has no folder.

        Raises BackupError if the backup itself failed.
        """
        local_now = self._clock().astimezone(self._config.tz)
        today = local_now.date()
        due = scheduled_for(today, self._backup_time, self._config.tz)
        if local_now < due or await asyncio.to_thread(self._service.exists_for, today):
            return BackupOutcome(None)
        made = await asyncio.to_thread(self._service.back_up, today)
        try:
            await asyncio.to_thread(self._service.prune)
        except BackupError as error:  # the backup is safe; say the clean-up failed
            logger.exception("Pruning old backups failed")
            return BackupOutcome(made, error)
        return BackupOutcome(made)

    async def run_forever(self) -> None:
        """Tick every ``POLL_INTERVAL_SECONDS`` until cancelled.

        A failing tick is logged and alerted once per distinct error until a
        tick succeeds again; it never stops the loop.
        """
        while True:
            try:
                outcome = await self.tick()
            except Exception as error:  # one bad tick must not stop the job
                logger.exception("Backup failed")
                await self._report(f"Backup failed: {describe_error(error)}")
            else:
                if outcome.prune_error is None:
                    self._last_error = None
                else:
                    await self._report(
                        "Backup made, but old backups were not cleaned: "
                        f"{describe_error(outcome.prune_error)}"
                    )
            await self._sleep(POLL_INTERVAL_SECONDS)

    async def _report(self, text: str) -> None:
        """Alert ``text`` unless it repeats the last alert."""
        if text != self._last_error:
            self._last_error = text
            await alert(ALERT_EVENT, text)
