"""Tests for the daily scheduler (SA-26 acceptance criteria)."""

import asyncio
import fcntl
import json
from collections.abc import Iterator
from datetime import UTC, date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from humblebrag import scheduler as scheduler_module
from humblebrag.alerts import configure_alerts
from humblebrag.config import Config, load_config
from humblebrag.file_lock import LockTimeoutError, file_lock
from humblebrag.scheduler import (
    ALERT_EVENT,
    POLL_INTERVAL_SECONDS,
    Claim,
    Scheduler,
    SchedulerSnapshot,
    SchedulerState,
    SchedulerStateError,
    is_due,
    scheduled_for,
)

LONDON = ZoneInfo("Europe/London")
ENV = {
    "TELEGRAM_BOT_TOKEN": "123456:TESTTOKEN",
    "TELEGRAM_CHAT_ID": "1",
    "ANTHROPIC_API_KEY": "sk-ant-SECRET-key",
    "CLAUDE_MODEL": "claude-test-model",
    "SUBSTACK_PUBLICATION_URL": "https://example.substack.com",
    "SUBSTACK_COOKIE": "cookie",
}


@pytest.fixture(autouse=True)
def alerts() -> Iterator[list[str]]:
    """Capture alert texts, and unset the sender afterwards."""
    sent: list[str] = []

    async def sender(text: str) -> None:
        sent.append(text)

    configure_alerts(sender)
    yield sent
    configure_alerts(None)


class FakeClock:
    """A settable UTC clock."""

    def __init__(self, now: datetime) -> None:
        """Start at ``now``."""
        self.now = now

    def __call__(self) -> datetime:
        """The current fake time."""
        return self.now


class Pipeline:
    """A fake pipeline that counts runs and can be told to fail."""

    def __init__(self) -> None:
        """Start with no runs and no failure."""
        self.runs = 0
        self.error: Exception | None = None

    async def __call__(self) -> None:
        """Count the run, then raise the configured error if any."""
        self.runs += 1
        if self.error is not None:
            raise self.error


def _utc(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    """A UTC datetime."""
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def _config(**overrides: str) -> Config:
    """A config with default RUN_TIME and TZ unless overridden."""
    return load_config({**ENV, **overrides})


def _build(
    tmp_path: Path, now: datetime, config: Config | None = None
) -> tuple[Scheduler, Pipeline, FakeClock, SchedulerState]:
    """A scheduler over a temporary state file with a fake clock and pipeline."""
    state = SchedulerState(tmp_path / "scheduler-state.json")
    pipeline, clock = Pipeline(), FakeClock(now)
    scheduler = Scheduler(config or _config(), state, pipeline, clock=clock)
    return scheduler, pipeline, clock, state


# AC1: fires at RUN_TIME in TZ, correct across BST/GMT


@pytest.mark.parametrize(
    ("before", "at"),
    [
        (_utc(2026, 1, 15, 8, 29), _utc(2026, 1, 15, 8, 30)),  # GMT
        (_utc(2026, 7, 15, 7, 29), _utc(2026, 7, 15, 7, 30)),  # BST
        (_utc(2026, 3, 29, 7, 29), _utc(2026, 3, 29, 7, 30)),  # day clocks go forward
        (_utc(2026, 10, 25, 8, 29), _utc(2026, 10, 25, 8, 30)),  # day clocks go back
    ],
)
def test_default_run_time_is_08_30_london_year_round(
    before: datetime, at: datetime
) -> None:
    """AC1: 08:30 local, whether the zone is on GMT or BST."""
    config = _config()
    assert (config.run_time, config.tz.key) == (time(8, 30), "Europe/London")
    assert not is_due(before, config)
    assert is_due(at, config)


def test_the_utc_instant_moves_by_an_hour_across_the_changes() -> None:
    """AC1: 08:30 London is 08:30 UTC in winter and 07:30 UTC in summer."""
    winter = scheduled_for(date(2026, 3, 28), time(8, 30), LONDON)
    summer = scheduled_for(date(2026, 3, 29), time(8, 30), LONDON)
    assert winter.astimezone(UTC) == _utc(2026, 3, 28, 8, 30)
    assert summer.astimezone(UTC) == _utc(2026, 3, 29, 7, 30)


def test_a_time_inside_the_spring_gap_runs_just_after_it() -> None:
    """AC1: 01:30 does not exist on 29 March; it moves to 02:30 BST."""
    moved = scheduled_for(date(2026, 3, 29), time(1, 30), LONDON)
    assert moved.astimezone(UTC) == _utc(2026, 3, 29, 1, 30)
    assert (moved.hour, moved.minute) == (2, 30)


def test_an_ambiguous_autumn_time_uses_its_first_occurrence() -> None:
    """AC1: 01:30 happens twice on 25 October; the first (BST) counts."""
    first = scheduled_for(date(2026, 10, 25), time(1, 30), LONDON)
    assert first.astimezone(UTC) == _utc(2026, 10, 25, 0, 30)


async def test_ambiguous_time_still_runs_once_that_day(tmp_path: Path) -> None:
    """AC1: the repeated hour cannot trigger a second run."""
    config = _config(RUN_TIME="01:30")
    scheduler, pipeline, clock, _ = _build(tmp_path, _utc(2026, 10, 25, 0, 30), config)
    await scheduler.tick()
    clock.now = _utc(2026, 10, 25, 1, 30)
    await scheduler.tick()
    assert pipeline.runs == 1


def test_run_time_and_tz_come_from_config() -> None:
    """AC1: RUN_TIME and TZ are honoured, not hard-coded."""
    config = _config(RUN_TIME="09:00", TZ="America/New_York")
    assert not is_due(_utc(2026, 1, 15, 13, 59), config)  # 08:59 EST
    assert is_due(_utc(2026, 1, 15, 14, 0), config)  # 09:00 EST


async def test_not_due_before_the_time_does_nothing(tmp_path: Path) -> None:
    """AC1: before 08:30 nothing runs and no state is written."""
    scheduler, pipeline, _, state = _build(tmp_path, _utc(2026, 1, 15, 8, 29))
    assert await scheduler.tick() is None
    assert pipeline.runs == 0
    assert state.read() == SchedulerSnapshot(None, False)


async def test_runs_once_a_day_and_again_the_next_day(tmp_path: Path) -> None:
    """AC1: repeated ticks the same day run once; the next day runs again."""
    scheduler, pipeline, clock, _ = _build(tmp_path, _utc(2026, 1, 15, 8, 30))
    assert await scheduler.tick() is Claim.RUN
    clock.now = _utc(2026, 1, 15, 12, 0)
    assert await scheduler.tick() is Claim.ALREADY_DONE
    assert pipeline.runs == 1
    clock.now = _utc(2026, 1, 16, 8, 30)
    assert await scheduler.tick() is Claim.RUN
    assert pipeline.runs == 2


async def test_the_local_date_decides_the_day_not_the_utc_date(tmp_path: Path) -> None:
    """AC1: a late-evening run time in New York is still that local day."""
    config = _config(RUN_TIME="22:00", TZ="America/New_York")
    scheduler, pipeline, clock, state = _build(
        tmp_path, _utc(2026, 1, 16, 3, 0), config
    )
    await scheduler.tick()  # 22:00 EST on the 15th, 03:00 UTC on the 16th
    assert state.read().last_run_date == date(2026, 1, 15)
    clock.now = _utc(2026, 1, 16, 4, 0)  # still the 15th in New York
    assert await scheduler.tick() is Claim.ALREADY_DONE
    assert pipeline.runs == 1


# AC2: the skip flag suppresses the next run only


async def test_skip_suppresses_the_next_run_only(tmp_path: Path) -> None:
    """AC2: one skipped day, then the following day runs normally."""
    scheduler, pipeline, clock, state = _build(tmp_path, _utc(2026, 1, 15, 8, 30))
    state.set_skip_next(True)
    assert await scheduler.tick() is Claim.SKIPPED
    assert pipeline.runs == 0
    assert state.read() == SchedulerSnapshot(date(2026, 1, 15), False)
    clock.now = _utc(2026, 1, 16, 8, 30)
    assert await scheduler.tick() is Claim.RUN
    assert pipeline.runs == 1


async def test_skip_set_after_todays_run_suppresses_tomorrow(tmp_path: Path) -> None:
    """AC2: "next run" means the next one that would fire."""
    scheduler, pipeline, clock, state = _build(tmp_path, _utc(2026, 1, 15, 8, 30))
    await scheduler.tick()
    state.set_skip_next(True)
    clock.now = _utc(2026, 1, 15, 20, 0)
    assert await scheduler.tick() is Claim.ALREADY_DONE
    assert state.read().skip_next
    clock.now = _utc(2026, 1, 16, 8, 30)
    assert await scheduler.tick() is Claim.SKIPPED
    assert pipeline.runs == 1


def test_skip_flag_can_be_cleared_and_survives_a_restart(tmp_path: Path) -> None:
    """AC2: the flag is persisted, and /skip can undo itself."""
    path = tmp_path / "state.json"
    SchedulerState(path).set_skip_next(True)
    assert SchedulerState(path).read().skip_next
    SchedulerState(path).set_skip_next(False)
    assert not SchedulerState(path).read().skip_next


# AC3: down at 08:30 -> one run on start, no catch-up


async def test_started_after_08_30_runs_once_with_no_catch_up(tmp_path: Path) -> None:
    """AC3: three missed days produce one run today, not four."""
    scheduler, pipeline, _, state = _build(tmp_path, _utc(2026, 1, 15, 10, 0))
    state.claim(date(2026, 1, 12))
    assert await scheduler.tick() is Claim.RUN
    assert await scheduler.tick() is Claim.ALREADY_DONE
    assert pipeline.runs == 1
    assert state.read().last_run_date == date(2026, 1, 15)


async def test_restart_after_todays_run_does_not_run_again(tmp_path: Path) -> None:
    """AC3: it runs "if it hasnae already": a restart later that day is a no-op."""
    scheduler, pipeline, clock, _ = _build(tmp_path, _utc(2026, 1, 15, 8, 30))
    await scheduler.tick()
    clock.now = _utc(2026, 1, 15, 14, 0)
    fresh_state = SchedulerState(tmp_path / "scheduler-state.json")
    restarted = Scheduler(_config(), fresh_state, pipeline, clock=clock)
    assert await restarted.tick() is Claim.ALREADY_DONE
    assert pipeline.runs == 1


async def test_started_before_08_30_waits_for_the_time(tmp_path: Path) -> None:
    """AC3: a start before the time does not run early."""
    scheduler, pipeline, clock, _ = _build(tmp_path, _utc(2026, 1, 15, 7, 0))
    assert await scheduler.tick() is None
    clock.now = _utc(2026, 1, 15, 8, 30)
    assert await scheduler.tick() is Claim.RUN
    assert pipeline.runs == 1


async def test_start_late_in_the_evening_still_runs_that_day(tmp_path: Path) -> None:
    """AC3: "on start that day" holds however late the container comes up."""
    scheduler, pipeline, _, _ = _build(tmp_path, _utc(2026, 1, 15, 23, 30))
    assert await scheduler.tick() is Claim.RUN
    assert pipeline.runs == 1


# Loop and failure paths


async def test_run_forever_polls_on_the_interval(tmp_path: Path) -> None:
    """The loop ticks, then sleeps POLL_INTERVAL_SECONDS between ticks."""
    state = SchedulerState(tmp_path / "state.json")
    pipeline = Pipeline()
    sleeps: list[float] = []

    async def stop_after_three(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 3:
            raise asyncio.CancelledError

    scheduler = Scheduler(
        _config(),
        state,
        pipeline,
        clock=FakeClock(_utc(2026, 1, 15, 9, 0)),
        sleep=stop_after_three,
    )
    with pytest.raises(asyncio.CancelledError):
        await scheduler.run_forever()
    assert sleeps == [POLL_INTERVAL_SECONDS] * 3
    assert pipeline.runs == 1


async def test_a_failing_run_alerts_and_the_scheduler_carries_on(
    tmp_path: Path, alerts: list[str]
) -> None:
    """ERR-01/07: a pipeline failure alerts Stuart; the next day still runs."""
    scheduler, pipeline, clock, _ = _build(tmp_path, _utc(2026, 1, 15, 8, 30))
    pipeline.error = RuntimeError("boom")
    assert await scheduler.tick() is Claim.RUN
    assert len(alerts) == 1
    assert ALERT_EVENT in alerts[0]
    assert "RuntimeError: boom" in alerts[0]
    assert await scheduler.tick() is Claim.ALREADY_DONE
    assert pipeline.runs == 1
    clock.now = _utc(2026, 1, 16, 8, 30)
    pipeline.error = None
    assert await scheduler.tick() is Claim.RUN


async def test_a_failing_run_is_not_retried_the_same_day(tmp_path: Path) -> None:
    """The day is claimed before the run, so a crash cannot double-run."""
    scheduler, pipeline, _, _ = _build(tmp_path, _utc(2026, 1, 15, 8, 30))
    pipeline.error = RuntimeError("boom")
    await scheduler.tick()
    await scheduler.tick()
    assert pipeline.runs == 1


async def test_exception_groups_are_unwrapped_in_the_alert(
    tmp_path: Path, alerts: list[str]
) -> None:
    """ERR-09: the alert names the leaf cause of a grouped failure."""
    scheduler, pipeline, _, _ = _build(tmp_path, _utc(2026, 1, 15, 8, 30))
    pipeline.error = ExceptionGroup("tasks", [ValueError("the real cause")])
    await scheduler.tick()
    assert "ValueError: the real cause" in alerts[0]


async def _ticks(scheduler: Scheduler, count: int) -> None:
    """Run ``count`` loop iterations of a scheduler's run_forever."""
    seen = 0

    async def counting_sleep(_seconds: float) -> None:
        nonlocal seen
        seen += 1
        if seen == count:
            raise asyncio.CancelledError

    scheduler._sleep = counting_sleep
    with pytest.raises(asyncio.CancelledError):
        await scheduler.run_forever()


async def test_corrupt_state_alerts_once_not_every_poll(
    tmp_path: Path, alerts: list[str]
) -> None:
    """ERR-01/07: an unreadable state file alerts, without spamming each tick."""
    scheduler, pipeline, _, state = _build(tmp_path, _utc(2026, 1, 15, 9, 0))
    state._path.write_text("{not json")
    await _ticks(scheduler, 5)
    assert pipeline.runs == 0
    assert len(alerts) == 1
    assert "scheduler-state.json" in alerts[0]


async def test_an_unexpected_tick_error_alerts_and_the_loop_survives(
    tmp_path: Path, alerts: list[str]
) -> None:
    """ERR-07: any error in a tick is alerted, and later ticks still run."""
    scheduler, pipeline, clock, _ = _build(tmp_path, _utc(2026, 1, 15, 9, 0))
    calls = 0
    real_now = clock.now

    def flaky_clock() -> datetime:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("clock exploded")
        return real_now

    scheduler._clock = flaky_clock
    await _ticks(scheduler, 2)
    assert len(alerts) == 1
    assert "RuntimeError: clock exploded" in alerts[0]
    assert pipeline.runs == 1


async def test_a_recovered_error_alerts_again_if_it_returns(
    tmp_path: Path, alerts: list[str]
) -> None:
    """The de-duplication resets after a successful tick."""
    scheduler, _, clock, state = _build(tmp_path, _utc(2026, 1, 15, 9, 0))
    state._path.write_text("{not json")
    await _ticks(scheduler, 2)
    state._path.unlink()
    clock.now = _utc(2026, 1, 15, 7, 0)
    await _ticks(scheduler, 1)  # healthy (not due) tick clears the error
    state._path.write_text("{not json")
    clock.now = _utc(2026, 1, 16, 9, 0)
    await _ticks(scheduler, 2)
    assert len(alerts) == 2


async def test_lock_timeout_alerts_and_does_not_run(
    tmp_path: Path, alerts: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """ERR-11: a lock held too long is a typed error, alerted, with no run."""
    monkeypatch.setattr(
        scheduler_module, "file_lock", lambda path: file_lock(path, timeout=0.05)
    )
    scheduler, pipeline, _, state = _build(tmp_path, _utc(2026, 1, 15, 9, 0))
    lock_path = state._lock_path
    with lock_path.open("a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        with pytest.raises(LockTimeoutError):
            await scheduler.tick()
        await _ticks(scheduler, 1)
    assert pipeline.runs == 0
    assert len(alerts) == 1


async def test_waiting_for_the_lock_does_not_block_the_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ERR-13: a contended lock stalls the claim, not the shared event loop."""
    monkeypatch.setattr(
        scheduler_module, "file_lock", lambda path: file_lock(path, timeout=0.3)
    )
    scheduler, pipeline, _, state = _build(tmp_path, _utc(2026, 1, 15, 9, 0))
    beats = 0

    async def other_task() -> None:
        nonlocal beats
        while True:
            beats += 1
            await asyncio.sleep(0.01)

    with state._lock_path.open("a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        background = asyncio.create_task(other_task())
        with pytest.raises(LockTimeoutError):
            await scheduler.tick()
        background.cancel()
    assert beats >= 10
    assert pipeline.runs == 0


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        "[]",
        '{"last_run_date": "2026-01-15"}',
        '{"last_run_date": "yesterday", "skip_next": false}',
        '{"last_run_date": null, "skip_next": "yes"}',
        '{"last_run_date": 5, "skip_next": false}',
    ],
)
def test_unreadable_state_is_a_typed_error(tmp_path: Path, content: str) -> None:
    """State files that cannot be understood raise SchedulerStateError."""
    path = tmp_path / "state.json"
    path.write_text(content)
    with pytest.raises(SchedulerStateError):
        SchedulerState(path).read()


def test_state_file_format_is_plain_json(tmp_path: Path) -> None:
    """The state file is human-readable JSON with the two documented keys."""
    path = tmp_path / "state.json"
    state = SchedulerState(path)
    state.claim(date(2026, 1, 15))
    assert json.loads(path.read_text()) == {
        "last_run_date": "2026-01-15",
        "skip_next": False,
    }


def test_claim_is_idempotent_for_the_same_day(tmp_path: Path) -> None:
    """ERR-12: check-and-record is one locked step, so a day is claimed once."""
    state = SchedulerState(tmp_path / "state.json")
    assert state.claim(date(2026, 1, 15)) is Claim.RUN
    assert state.claim(date(2026, 1, 15)) is Claim.ALREADY_DONE


def test_file_lock_times_out_with_a_typed_error(tmp_path: Path) -> None:
    """ERR-11: the shared lock helper waits a bounded time, then raises."""
    lock_path = tmp_path / "x.lock"
    with lock_path.open("a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        with (
            pytest.raises(LockTimeoutError, match="x.lock"),
            file_lock(lock_path, timeout=0.05),
        ):
            pass
    with file_lock(lock_path, timeout=0.05):
        pass
