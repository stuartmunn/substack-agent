"""Tests for the daily backup of seed.md and the database (SA-38 criteria)."""

import asyncio
import shutil
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from humblebrag import backup as backup_module
from humblebrag.alerts import configure_alerts
from humblebrag.backup import (
    BACKUP_TIME,
    BACKUPS_DIRNAME,
    DB_FILENAME,
    KEEP_BACKUPS,
    POLL_INTERVAL_SECONDS,
    SEED_FILENAME,
    BackupError,
    BackupJob,
    BackupService,
)
from humblebrag.config import load_config
from humblebrag.drafts import DraftStore

ENV = {
    "TELEGRAM_BOT_TOKEN": "123456:TESTTOKEN",
    "TELEGRAM_CHAT_ID": "1",
    "ANTHROPIC_API_KEY": "sk-ant-key",
    "CLAUDE_MODEL": "claude-test-model",
    "SUBSTACK_PUBLICATION_URL": "https://example.substack.com",
    "SUBSTACK_COOKIE": "cookie",
}
SEEDS = "- [ ] First seed\n- [x] Old seed (used 2026-11-01)\n"
# November: London is on GMT, so local time equals UTC.
FOUR_AM = datetime(2026, 11, 18, 4, 0, tzinfo=UTC)
TWO_AM = datetime(2026, 11, 18, 2, 0, tzinfo=UTC)
DAY = date(2026, 11, 18)


@pytest.fixture(autouse=True)
def alerts() -> Iterator[list[str]]:
    """Capture alert texts, and unset the sender afterwards."""
    sent: list[str] = []

    async def sender(text: str) -> None:
        sent.append(text)

    configure_alerts(sender)
    yield sent
    configure_alerts(None)


class Rig:
    """A data directory with a real seed file and database, and a backup job."""

    def __init__(self, data_dir: Path) -> None:
        """Create the data files and the job with a settable clock."""
        self.data_dir = data_dir
        data_dir.mkdir()
        (data_dir / SEED_FILENAME).write_text(SEEDS)
        self.store = DraftStore(data_dir / DB_FILENAME)
        self.draft = self.store.create("First seed", "Title", "Sub", "Body")
        self.now = FOUR_AM
        self.service = BackupService(data_dir)
        self.job = BackupJob(load_config(ENV), self.service, clock=lambda: self.now)

    @property
    def backups(self) -> Path:
        """The backups directory."""
        return self.data_dir / BACKUPS_DIRNAME

    def folders(self) -> list[str]:
        """Names of the entries in the backups directory, sorted."""
        return sorted(path.name for path in self.backups.iterdir())


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    """A fresh rig."""
    return Rig(tmp_path / "data")


# AC1: a copy of seed.md and the DB goes to /data/backups/ daily, keeping 14 days


async def test_a_backup_holds_seed_md_and_a_usable_copy_of_the_database(
    rig: Rig,
) -> None:
    """AC1: seed.md is copied exactly and the DB copy opens with the same draft."""
    outcome = await rig.job.tick()
    assert outcome.made == rig.backups / "2026-11-18"
    assert (rig.backups / "2026-11-18" / SEED_FILENAME).read_text() == SEEDS
    copy = DraftStore(rig.backups / "2026-11-18" / DB_FILENAME)
    assert copy.get(rig.draft.id).title == "Title"


async def test_the_copy_is_independent_of_later_changes(rig: Rig) -> None:
    """The backup is a snapshot: later edits to the originals do not touch it."""
    await rig.job.tick()
    (rig.data_dir / SEED_FILENAME).write_text("changed\n")
    rig.store.mark_binned(rig.draft.id)
    folder = rig.backups / "2026-11-18"
    assert (folder / SEED_FILENAME).read_text() == SEEDS
    assert DraftStore(folder / DB_FILENAME).get(rig.draft.id).status == "pending"


async def test_no_backup_is_made_before_backup_time(rig: Rig) -> None:
    """The job waits for BACKUP_TIME (03:00)."""
    assert BACKUP_TIME.hour == 3
    rig.now = TWO_AM
    outcome = await rig.job.tick()
    assert outcome.made is None
    assert not rig.backups.exists()


async def test_one_backup_a_day_however_often_it_ticks(rig: Rig) -> None:
    """A second tick, or a restart, on the same day does not copy again."""
    first = await rig.job.tick()
    folder = rig.backups / "2026-11-18"
    marker = folder / "marker"
    marker.write_text("untouched")
    rig.now = FOUR_AM + timedelta(hours=5)
    second = await rig.job.tick()
    assert first.made == folder
    assert second.made is None
    assert marker.exists()


async def test_the_next_day_makes_a_new_backup(rig: Rig) -> None:
    """Each local day gets its own folder."""
    await rig.job.tick()
    rig.now = FOUR_AM + timedelta(days=1)
    await rig.job.tick()
    assert rig.folders() == ["2026-11-18", "2026-11-19"]


async def test_a_missing_seed_file_is_skipped_and_the_database_still_backed_up(
    rig: Rig,
) -> None:
    """A fresh install has no seed.md yet; that is not a failure."""
    (rig.data_dir / SEED_FILENAME).unlink()
    await rig.job.tick()
    folder = rig.backups / "2026-11-18"
    assert not (folder / SEED_FILENAME).exists()
    assert (folder / DB_FILENAME).exists()


async def test_fourteen_days_are_kept_and_older_ones_removed(rig: Rig) -> None:
    """AC1: after day 15 the oldest is gone and exactly 14 remain."""
    assert KEEP_BACKUPS == 14
    for offset in range(15):
        rig.now = FOUR_AM + timedelta(days=offset)
        await rig.job.tick()
    names = rig.folders()
    assert len(names) == 14
    assert names[0] == "2026-11-19"
    assert names[-1] == "2026-12-02"


async def test_fourteen_backups_are_all_kept(rig: Rig) -> None:
    """The boundary: 14 backups means nothing is removed yet."""
    for offset in range(14):
        rig.now = FOUR_AM + timedelta(days=offset)
        await rig.job.tick()
    assert len(rig.folders()) == 14


def test_pruning_only_touches_dated_folders(rig: Rig) -> None:
    """Anything else in backups/ (notes, other folders) is left alone."""
    rig.backups.mkdir()
    (rig.backups / "notes.txt").write_text("mine")
    (rig.backups / "manual-copy").mkdir()
    for offset in range(20):
        (rig.backups / (DAY + timedelta(days=offset)).isoformat()).mkdir()
    removed = rig.service.prune()
    assert len(removed) == 6
    assert (rig.backups / "notes.txt").exists()
    assert (rig.backups / "manual-copy").exists()
    assert len([p for p in rig.backups.iterdir() if p.name[0].isdigit()]) == 14


# AC2: a failed backup alerts Stuart


async def _run_one_poll(rig: Rig) -> None:
    """Run ``run_forever`` for exactly one tick by cancelling the sleep."""

    async def stop(_seconds: float) -> None:
        raise asyncio.CancelledError

    rig.job._sleep = stop
    with pytest.raises(asyncio.CancelledError):
        await rig.job.run_forever()


async def test_a_missing_database_alerts(rig: Rig, alerts: list[str]) -> None:
    """AC2: no database to copy is a failure Stuart hears about."""
    (rig.data_dir / DB_FILENAME).unlink()
    await _run_one_poll(rig)
    assert len(alerts) == 1
    assert "Backup failed" in alerts[0]
    assert DB_FILENAME in alerts[0]


async def test_a_failed_copy_alerts_and_leaves_no_folder(
    rig: Rig, alerts: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC2: a disk error alerts, and nothing half-made is left to fool the retry."""

    def broken(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(shutil, "copy2", broken)
    await _run_one_poll(rig)
    assert len(alerts) == 1
    assert "disk full" in alerts[0]
    assert rig.folders() == []


async def test_a_corrupt_database_alerts(rig: Rig, alerts: list[str]) -> None:
    """AC2: a database SQLite cannot read is alerted, not copied as if fine."""
    (rig.data_dir / DB_FILENAME).write_bytes(b"this is not a database" * 50)
    await _run_one_poll(rig)
    assert len(alerts) == 1
    assert "Backup failed" in alerts[0]
    assert rig.folders() == []


async def test_a_failed_integrity_check_alerts(
    rig: Rig, alerts: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC2: a copy that does not verify is a failure, and nothing is kept."""
    monkeypatch.setattr(backup_module, "INTEGRITY_OK", "not what SQLite says")
    await _run_one_poll(rig)
    assert len(alerts) == 1
    assert "integrity" in alerts[0]
    assert rig.folders() == []


async def test_a_failure_is_retried_and_the_retry_can_succeed(
    rig: Rig, alerts: list[str]
) -> None:
    """After a failure the next tick tries again; success is a normal backup."""
    (rig.data_dir / DB_FILENAME).rename(rig.data_dir / "db.moved")
    await _run_one_poll(rig)
    (rig.data_dir / "db.moved").rename(rig.data_dir / DB_FILENAME)
    outcome = await rig.job.tick()
    assert outcome.made == rig.backups / "2026-11-18"
    assert len(alerts) == 1


async def test_the_same_failure_alerts_once_until_it_clears(
    rig: Rig, alerts: list[str]
) -> None:
    """A fault that persists across ticks is one alert, not one per ten minutes."""
    (rig.data_dir / DB_FILENAME).unlink()
    for _ in range(3):
        await _run_one_poll(rig)
    assert len(alerts) == 1


async def test_a_new_failure_after_recovery_alerts_again(
    rig: Rig, alerts: list[str]
) -> None:
    """The once-only memory resets when a tick succeeds."""
    moved = rig.data_dir / "db.moved"
    (rig.data_dir / DB_FILENAME).rename(moved)
    await _run_one_poll(rig)
    moved.rename(rig.data_dir / DB_FILENAME)
    await _run_one_poll(rig)  # succeeds: backs up
    shutil.rmtree(rig.backups / "2026-11-18")
    (rig.data_dir / DB_FILENAME).rename(moved)
    await _run_one_poll(rig)
    assert len(alerts) == 2


async def test_a_pruning_failure_alerts_but_keeps_the_new_backup(
    rig: Rig, alerts: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """ERR-20: today's backup stands; the clean-up failure is alerted with that fact."""

    def broken(_path: Path, ignore_errors: bool = False) -> None:
        if not ignore_errors:  # only the pruning of old folders fails
            raise OSError("busy")

    for offset in range(1, 17):
        (rig.backups / (DAY - timedelta(days=offset)).isoformat()).mkdir(parents=True)
    monkeypatch.setattr(backup_module.shutil, "rmtree", broken)
    await _run_one_poll(rig)
    assert (rig.backups / "2026-11-18" / DB_FILENAME).exists()
    assert len(alerts) == 1
    assert "Backup made" in alerts[0]
    assert "busy" in alerts[0]


async def test_a_crashing_tick_never_stops_the_loop(
    rig: Rig, alerts: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """ERR-07: an unexpected error is alerted and the loop carries on."""

    def explode(_day: date) -> Path:
        raise RuntimeError("surprise")

    monkeypatch.setattr(rig.service, "back_up", explode)
    sleeps: list[float] = []

    async def counting_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 2:
            raise asyncio.CancelledError

    rig.job._sleep = counting_sleep
    with pytest.raises(asyncio.CancelledError):
        await rig.job.run_forever()
    assert sleeps == [POLL_INTERVAL_SECONDS, POLL_INTERVAL_SECONDS]
    assert len(alerts) == 1
    assert "surprise" in alerts[0]


def test_back_up_raises_the_typed_error(rig: Rig) -> None:
    """The service reports failure as BackupError, not a raw library error."""
    (rig.data_dir / DB_FILENAME).unlink()
    with pytest.raises(BackupError):
        rig.service.back_up(DAY)
