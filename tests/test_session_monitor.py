"""Tests for the daily session-expiry monitor (SA-33 acceptance criteria)."""

import asyncio
from collections.abc import Iterator
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import pytest
from substack.exceptions import SubstackAPIException

from humblebrag.alerts import configure_alerts
from humblebrag.config import load_config
from humblebrag.publisher import SESSION_EXPIRED_MESSAGE, SubstackPublisher
from humblebrag.session_monitor import (
    CHECK_TIME,
    MonitorState,
    MonitorStateError,
    SessionMonitor,
)

ENV = {
    "TELEGRAM_BOT_TOKEN": "123456:TESTTOKEN",
    "TELEGRAM_CHAT_ID": "1",
    "ANTHROPIC_API_KEY": "sk-ant-key",
    "CLAUDE_MODEL": "claude-test-model",
    "SUBSTACK_PUBLICATION_URL": "https://humblebrag.substack.com",
    "SUBSTACK_COOKIE": "s%3ASECRET-SID",
}
# November: London is on GMT, so local time equals UTC.
DAY = datetime(2026, 11, 18, tzinfo=UTC)


@pytest.fixture(autouse=True)
def alerts() -> Iterator[list[str]]:
    """Capture alert texts."""
    sent: list[str] = []

    async def sender(text: str) -> None:
        sent.append(text)

    configure_alerts(sender)
    yield sent
    configure_alerts(None)


class Clock:
    """A settable clock."""

    def __init__(self, now: datetime) -> None:
        """Start at ``now``."""
        self.now = now

    def __call__(self) -> datetime:
        """The current fake time."""
        return self.now


class Harness:
    """A monitor over a real publisher whose session can be made to expire."""

    def __init__(self, tmp_path: Path, start: datetime) -> None:
        """Healthy session, clock at ``start``."""
        self.config = load_config(ENV)
        self.clock = Clock(start)
        self.expired = False
        self.checks = 0
        self.publisher = SubstackPublisher(self.config, self._factory)
        self.state_path = tmp_path / "monitor.json"
        self.monitor = self.new_monitor()

    def _factory(self, **_kwargs: Any) -> object:
        self.checks += 1
        if self.expired:
            raise SubstackAPIException(401, "{}")
        return object()

    def new_monitor(self) -> SessionMonitor:
        """A fresh monitor sharing the same state file (a "restart")."""
        return SessionMonitor(
            self.config,
            MonitorState(self.state_path),
            self.publisher.ensure_session,
            clock=self.clock,
        )

    def at(self, hour: int, minute: int = 0, days: int = 0) -> None:
        """Move the clock to a time on day + ``days``."""
        self.clock.now = (DAY + timedelta(days=days)).replace(hour=hour, minute=minute)


@pytest.fixture
def h(tmp_path: Path) -> Harness:
    """A harness starting at 07:00 on the test day."""
    harness = Harness(tmp_path, DAY)
    harness.at(7)
    return harness


# AC1: check_session() runs at 08:00 daily, before the 08:30 run


def test_the_default_check_time_is_0800_before_the_0830_run() -> None:
    """AC1: 08:00, and earlier than the default run time."""
    assert time(8, 0) == CHECK_TIME
    assert CHECK_TIME < load_config(ENV).run_time


async def test_it_does_not_check_before_0800(h: Harness) -> None:
    """AC1: nothing happens at 07:59."""
    h.at(7, 59)
    assert await h.monitor.tick() is False
    assert h.checks == 0


async def test_it_checks_at_0800(h: Harness) -> None:
    """AC1: the session check runs at 08:00."""
    h.at(8, 0)
    assert await h.monitor.tick() is True
    assert h.checks == 1


async def test_it_checks_before_the_0830_run_is_due(h: Harness) -> None:
    """AC1: by the time the 08:30 run is due the check has already happened."""
    from humblebrag.scheduler import is_due

    h.at(8, 0)
    assert not is_due(h.clock(), h.config)
    await h.monitor.tick()
    assert h.checks == 1


async def test_it_uses_local_time_in_summer(tmp_path: Path) -> None:
    """AC1: in BST, 08:00 local is 07:00 UTC."""
    summer = datetime(2026, 6, 15, 6, 59, tzinfo=UTC)
    h = Harness(tmp_path, summer)
    assert await h.monitor.tick() is False
    h.clock.now = datetime(2026, 6, 15, 7, 0, tzinfo=UTC)
    assert await h.monitor.tick() is True


async def test_it_checks_once_a_day_and_again_the_next_day(h: Harness) -> None:
    """AC1: daily means once per local date."""
    h.at(8)
    await h.monitor.tick()
    h.at(12)
    assert await h.monitor.tick() is False
    h.at(8, days=1)
    assert await h.monitor.tick() is True
    assert h.checks == 2


async def test_a_restart_after_todays_check_does_not_check_again(h: Harness) -> None:
    """AC2: state survives a restart, so there is no repeat the same day."""
    h.at(8)
    await h.monitor.tick()
    h.at(9)
    assert await h.new_monitor().tick() is False
    assert h.checks == 1


async def test_a_container_started_after_0800_still_checks_that_day(
    h: Harness,
) -> None:
    """A late start catches up for today only."""
    h.at(10)
    assert await h.monitor.tick() is True


async def test_startup_check_counts_as_todays_check_when_past_0800(
    h: Harness,
) -> None:
    """A startup check after 08:00 is today's; before 08:00 it is not."""
    h.at(7)
    await h.monitor.mark_checked_if_due()
    h.at(8)
    assert await h.monitor.tick() is True  # 07:00 startup did not use up today
    h.at(10, days=1)
    await h.monitor.mark_checked_if_due()
    assert await h.monitor.tick() is False  # a 10:00 startup check did


# AC2: failure alerts Stuart; alerts repeat at most once a day


async def test_a_failed_check_alerts_stuart_once(h: Harness, alerts: list[str]) -> None:
    """AC2: an expired session alerts with the expiry message."""
    h.expired = True
    h.at(8)
    assert await h.monitor.tick() is True
    assert len(alerts) == 1
    assert SESSION_EXPIRED_MESSAGE in alerts[0]


async def test_repeated_ticks_the_same_day_do_not_alert_again(
    h: Harness, alerts: list[str]
) -> None:
    """AC2: at most one alert a day, even across polls and restarts."""
    h.expired = True
    h.at(8)
    await h.monitor.tick()
    for hour in (8, 9, 15, 23):
        h.at(hour, 30)
        await h.monitor.tick()
        await h.new_monitor().tick()
    assert len(alerts) == 1
    assert h.checks == 1


async def test_a_still_expired_session_alerts_again_the_next_day(
    h: Harness, alerts: list[str]
) -> None:
    """AC2: a reminder each day, never more."""
    h.expired = True
    h.at(8)
    await h.monitor.tick()
    h.at(8, days=1)
    await h.monitor.tick()
    assert len(alerts) == 2


async def test_a_healthy_check_is_silent(h: Harness, alerts: list[str]) -> None:
    """No alert when the session is fine."""
    h.at(8)
    await h.monitor.tick()
    assert alerts == []


async def test_an_unexpected_crash_in_the_check_alerts_and_survives(
    h: Harness, alerts: list[str]
) -> None:
    """ERR-01: even a surprise error is alerted and does not escape the tick."""

    async def boom() -> None:
        raise RuntimeError("kaboom")

    monitor = SessionMonitor(h.config, MonitorState(h.state_path), boom, clock=h.clock)
    h.at(8)
    assert await monitor.tick() is True
    assert len(alerts) == 1
    assert "RuntimeError: kaboom" in alerts[0]


# State file and loop behaviour


async def test_an_unreadable_state_file_is_reported_once_by_the_loop(
    h: Harness, alerts: list[str]
) -> None:
    """ERR-07: a bad state file alerts once and the loop keeps going."""
    h.state_path.write_text("not json")
    h.at(8)
    sleeps = 0

    async def sleep(_seconds: float) -> None:
        nonlocal sleeps
        sleeps += 1
        if sleeps == 3:
            raise asyncio.CancelledError

    monitor = SessionMonitor(
        h.config,
        MonitorState(h.state_path),
        h.publisher.ensure_session,
        clock=h.clock,
        sleep=sleep,
    )
    with pytest.raises(asyncio.CancelledError):
        await monitor.run_forever()
    assert sleeps == 3
    assert len(alerts) == 1
    assert "monitor state" in alerts[0]


async def test_the_loop_runs_the_daily_check(h: Harness) -> None:
    """The run_forever loop performs the check when 08:00 arrives."""
    h.at(8)

    async def sleep(_seconds: float) -> None:
        raise asyncio.CancelledError

    monitor = SessionMonitor(
        h.config,
        MonitorState(h.state_path),
        h.publisher.ensure_session,
        clock=h.clock,
        sleep=sleep,
    )
    with pytest.raises(asyncio.CancelledError):
        await monitor.run_forever()
    assert h.checks == 1


def test_state_round_trips_and_claims_only_once_per_day(tmp_path: Path) -> None:
    """The state file records the date and claims each date once."""
    state = MonitorState(tmp_path / "sub" / "s.json")
    assert state.last_checked() is None
    assert state.claim(date(2026, 11, 18)) is True
    assert state.claim(date(2026, 11, 18)) is False
    assert state.last_checked() == date(2026, 11, 18)
    assert state.claim(date(2026, 11, 19)) is True


def test_a_corrupt_state_file_is_a_typed_error(tmp_path: Path) -> None:
    """ERR-06: corrupt state raises MonitorStateError, not a raw exception."""
    path = tmp_path / "s.json"
    path.write_bytes(b"\xff\xfe")
    with pytest.raises(MonitorStateError):
        MonitorState(path).last_checked()


def test_an_unwritable_state_dir_is_a_typed_error(tmp_path: Path) -> None:
    """ERR-14: a write failure is a MonitorStateError."""
    blocker = tmp_path / "file"
    blocker.write_text("x")
    with pytest.raises(MonitorStateError):
        MonitorState(blocker / "s.json").claim(date(2026, 11, 18))


def test_main_wires_the_monitor_into_the_bot_startup() -> None:
    """The monitor runs for real: __main__ starts it from the bot's startup hook."""
    source = (
        Path(__file__).resolve().parent.parent / "src/humblebrag/__main__.py"
    ).read_text()
    assert "on_started=runtime.start" in source
    wiring = (
        Path(__file__).resolve().parent.parent / "src/humblebrag/wiring.py"
    ).read_text()
    assert "monitor.run_forever()" in wiring
    assert "SessionMonitor(" in wiring


async def test_a_bad_state_file_at_startup_does_not_stop_the_monitor(
    h: Harness, alerts: list[str]
) -> None:
    """PR-Agent: mark_checked_if_due swallows state errors so the loop starts."""
    h.state_path.write_text("not json")
    h.at(9)
    await h.monitor.mark_checked_if_due()  # must not raise
    with pytest.raises(MonitorStateError):
        await h.monitor.tick()  # the loop reports this one (alerted once there)
