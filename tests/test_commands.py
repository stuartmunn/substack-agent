"""Tests for /seed, /seeds, /skip and /now (SA-36 acceptance criteria)."""

import asyncio
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from humblebrag.alerts import configure_alerts
from humblebrag.commands import (
    FAILED,
    NOW_BUSY,
    NOW_NO_DRAFT,
    NOW_STARTED,
    SEED_USAGE,
    SEEDS_EMPTY,
    SEEDS_SHOWN,
    CommandService,
    RunCoordinator,
    install_commands,
)
from humblebrag.config import load_config
from humblebrag.scheduler import Claim, SchedulerState
from humblebrag.seed_queue import MAX_SEED_LENGTH, SeedQueue

ENV = {
    "TELEGRAM_BOT_TOKEN": "123456:TESTTOKEN",
    "TELEGRAM_CHAT_ID": "1",
    "ANTHROPIC_API_KEY": "sk-ant-key",
    "CLAUDE_MODEL": "claude-test-model",
    "SUBSTACK_PUBLICATION_URL": "https://example.substack.com",
    "SUBSTACK_COOKIE": "cookie",
}
TODAY = date(2026, 11, 18)
MORNING = datetime(2026, 11, 18, 7, 0, tzinfo=UTC)  # before 08:30 in London
AFTERNOON = datetime(2026, 11, 18, 14, 0, tzinfo=UTC)
SEEDS = "- [ ] First seed\n- [x] Old seed (used 2026-11-01)\n- [ ] Second seed\n"


@pytest.fixture(autouse=True)
def alerts() -> Iterator[list[str]]:
    """Capture alert texts, and unset the sender afterwards."""
    sent: list[str] = []

    async def sender(text: str) -> None:
        sent.append(text)

    configure_alerts(sender)
    yield sent
    configure_alerts(None)


class FakeRun:
    """A stand-in for ``RunPipeline.run`` that can be held open or made to fail."""

    def __init__(self) -> None:
        """Start idle."""
        self.calls = 0
        self.gate: asyncio.Event | None = None
        self.result: Any = "draft"
        self.error: Exception | None = None

    async def __call__(self) -> Any:
        """Count the call, wait for the gate if set, then return or raise."""
        self.calls += 1
        if self.gate is not None:
            await self.gate.wait()
        if self.error is not None:
            raise self.error
        return self.result


class Rig:
    """Real queue and scheduler state in a temp dir, with fake chat and run."""

    def __init__(self, tmp_path: Path) -> None:
        """Build the service and the fake chat."""
        self.seed_path = tmp_path / "seed.md"
        self.seed_path.write_text(SEEDS)
        self.queue = SeedQueue(self.seed_path, lambda: TODAY)
        self.state = SchedulerState(tmp_path / "scheduler-state.json")
        self.run = FakeRun()
        self.now = MORNING
        self.replies: list[str] = []
        self.tasks: list[asyncio.Task[Any]] = []
        self.service = CommandService(
            self.queue,
            self.state,
            load_config(ENV),
            RunCoordinator(self.run),
            clock=lambda: self.now,
        )

    async def say(self, handler_name: str, *args: str) -> None:
        """Send a command with ``args`` through the matching handler."""

        async def reply_text(text: str) -> None:
            self.replies.append(text)

        def create_task(coro: Any) -> asyncio.Task[Any]:
            task = asyncio.create_task(coro)
            self.tasks.append(task)
            return task

        update = SimpleNamespace(
            effective_message=SimpleNamespace(reply_text=reply_text)
        )
        context = SimpleNamespace(
            args=list(args), application=SimpleNamespace(create_task=create_task)
        )
        await getattr(self.service, handler_name)(update, context)

    async def settle(self) -> None:
        """Wait for every background task started by a command."""
        await asyncio.gather(*self.tasks)


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    """A fresh rig."""
    return Rig(tmp_path)


# AC1: /seed


async def test_seed_appends_an_unused_seed_and_confirms(rig: Rig) -> None:
    """AC1: the seed lands at the end of seed.md, unused, and the reply says so."""
    await rig.say("on_seed", "A", "brand", "new", "seed")
    assert rig.seed_path.read_text() == SEEDS + "- [ ] A brand new seed\n"
    assert rig.queue.unused_texts() == ["First seed", "Second seed", "A brand new seed"]
    assert rig.replies == ["Added to the queue (3 unused): A brand new seed"]


@pytest.mark.parametrize("args", [[], [""], ["   "]])
async def test_seed_with_no_text_is_rejected(rig: Rig, args: list[str]) -> None:
    """AC1: empty text changes nothing and tells Stuart how to use it."""
    await rig.say("on_seed", *args)
    assert rig.seed_path.read_text() == SEEDS
    assert rig.replies == [SEED_USAGE]


@pytest.mark.parametrize(
    "text",
    ["x" * (MAX_SEED_LENGTH + 1), "bell\x07here", "First seed", "FIRST   SEED"],
)
async def test_seed_that_cannot_be_stored_is_rejected_and_not_added(
    rig: Rig, text: str
) -> None:
    """Too long, control characters and duplicates of unused seeds are refused."""
    await rig.say("on_seed", *text.split(" "))
    assert rig.seed_path.read_text() == SEEDS
    assert rig.replies[0].startswith("Not added.")


async def test_seed_can_repeat_a_used_seed(rig: Rig) -> None:
    """Only unused seeds count as duplicates, so a used seed can come round again."""
    await rig.say("on_seed", "Old", "seed")
    assert "- [ ] Old seed" in rig.seed_path.read_text()


async def test_seed_failure_alerts_and_tells_the_chat(
    rig: Rig, alerts: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """ERR-01: a broken queue is an alert and a reply, not a crash."""

    def broken(_text: str) -> str:
        raise RuntimeError("disk full")

    monkeypatch.setattr(rig.queue, "add_seed", broken)
    await rig.say("on_seed", "hello")
    assert rig.replies == [FAILED]
    assert len(alerts) == 1
    assert "/seed" in alerts[0]
    assert "disk full" in alerts[0]


# AC2: /seeds


async def test_seeds_lists_unused_seeds_in_order_with_a_count(rig: Rig) -> None:
    """AC2: used seeds are left out; the rest are numbered in file order."""
    await rig.say("on_seeds")
    assert rig.replies == ["2 unused seeds, in order:\n1. First seed\n2. Second seed"]


async def test_seeds_shows_the_first_twenty_plus_a_count(rig: Rig) -> None:
    """AC2: 25 unused seeds show 20, then how many more."""
    rig.seed_path.write_text("".join(f"- [ ] Seed {n}\n" for n in range(1, 26)))
    await rig.say("on_seeds")
    lines = rig.replies[0].split("\n")
    assert lines[0] == "25 unused seeds, in order:"
    assert lines[1] == "1. Seed 1"
    assert lines[SEEDS_SHOWN] == "20. Seed 20"
    assert lines[-1] == "...and 5 more"
    assert len(lines) == SEEDS_SHOWN + 2


async def test_seeds_exactly_twenty_has_no_more_line(rig: Rig) -> None:
    """AC2: the boundary, 20 seeds, shows all of them and no "more" line."""
    rig.seed_path.write_text("".join(f"- [ ] Seed {n}\n" for n in range(1, 21)))
    await rig.say("on_seeds")
    assert "more" not in rig.replies[0]


async def test_seeds_says_so_when_none_are_left(rig: Rig) -> None:
    """AC2: an empty queue gets its own message."""
    rig.seed_path.write_text("- [x] Old seed (used 2026-11-01)\n")
    await rig.say("on_seeds")
    assert rig.replies == [SEEDS_EMPTY]


async def test_seeds_shortens_very_long_hand_written_seeds(rig: Rig) -> None:
    """A huge seed cannot push the list past Telegram's message limit."""
    rig.seed_path.write_text(f"- [ ] {'y' * 1000}\n")
    await rig.say("on_seeds")
    assert len(rig.replies[0]) < 200


async def test_seeds_failure_alerts(
    rig: Rig, alerts: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """ERR-01: an unreadable seed file is alerted."""
    rig.seed_path.write_bytes(b"\xff\xfe")
    await rig.say("on_seeds")
    assert rig.replies == [FAILED]
    assert len(alerts) == 1 and "/seeds" in alerts[0]


# AC3: /skip


async def test_skip_before_todays_run_skips_today_and_says_the_date(rig: Rig) -> None:
    """AC3: with today's run still to come, today is the one skipped."""
    await rig.say("on_skip")
    assert rig.replies == ["Skipping the run on 2026-11-18. Later runs are unaffected."]
    assert rig.state.read().skip_next is True


async def test_skip_after_todays_run_skips_tomorrow(rig: Rig) -> None:
    """AC3: once today is claimed the next scheduled run is tomorrow's."""
    rig.state.claim(TODAY)
    rig.now = AFTERNOON
    await rig.say("on_skip")
    assert "2026-11-19" in rig.replies[0]


async def test_skip_uses_the_london_date_not_utc(rig: Rig) -> None:
    """The date is the local one: 23:30 UTC in summer is already tomorrow in London."""
    rig.now = datetime(2026, 6, 30, 23, 30, tzinfo=UTC)
    await rig.say("on_skip")
    assert "2026-07-01" in rig.replies[0]


async def test_skip_suppresses_exactly_one_run(rig: Rig) -> None:
    """AC3: the scheduler skips the next claim, then carries on as normal."""
    await rig.say("on_skip")
    assert rig.state.claim(TODAY) is Claim.SKIPPED
    assert rig.state.claim(date(2026, 11, 19)) is Claim.RUN


async def test_skip_failure_alerts(
    rig: Rig, alerts: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """ERR-01: an unwritable state file is alerted and reported."""

    def broken(_skip: bool) -> None:
        raise RuntimeError("read-only")

    monkeypatch.setattr(rig.state, "set_skip_next", broken)
    await rig.say("on_skip")
    assert rig.replies == [FAILED]
    assert len(alerts) == 1 and "/skip" in alerts[0]


# AC4: /now


async def test_now_runs_the_pipeline_immediately(rig: Rig) -> None:
    """AC4: the pipeline runs once, straight away, even before 08:30."""
    await rig.say("on_now")
    await rig.settle()
    assert rig.run.calls == 1
    assert rig.replies == [NOW_STARTED]


async def test_now_does_not_touch_the_daily_schedule(rig: Rig) -> None:
    """AC4: scheduler state is unchanged, so today's 08:30 run still happens."""
    rig.state.set_skip_next(True)
    before = rig.state.read()
    await rig.say("on_now")
    await rig.settle()
    assert rig.state.read() == before
    assert rig.state.claim(TODAY) is Claim.SKIPPED  # the skip is still waiting


async def test_now_leaves_a_fresh_schedule_unclaimed(rig: Rig) -> None:
    """AC4: after /now, today is still unclaimed, so the daily run still fires."""
    await rig.say("on_now")
    await rig.settle()
    assert rig.state.read().last_run_date is None
    assert rig.state.claim(TODAY) is Claim.RUN


async def test_now_replies_before_the_run_finishes(rig: Rig) -> None:
    """The handler returns at once, so the bot stays responsive during a run."""
    rig.run.gate = asyncio.Event()
    await rig.say("on_now")
    assert rig.replies == [NOW_STARTED]
    rig.run.gate.set()
    await rig.settle()


async def test_now_while_a_run_is_in_progress_is_refused(rig: Rig) -> None:
    """A second /now does not start a second run."""
    rig.run.gate = asyncio.Event()
    await rig.say("on_now")
    await asyncio.sleep(0)
    await rig.say("on_now")
    await asyncio.sleep(0)
    rig.run.gate.set()
    await rig.settle()
    assert rig.run.calls == 1
    assert rig.replies == [NOW_STARTED, NOW_STARTED, NOW_BUSY]


async def test_a_scheduled_run_waits_for_a_now_run(rig: Rig) -> None:
    """The daily run is never dropped because /now was running; it waits its turn."""
    coordinator = RunCoordinator(rig.run)
    rig.run.gate = asyncio.Event()
    now_task = asyncio.create_task(coordinator.try_run_now())
    await asyncio.sleep(0)
    scheduled = asyncio.create_task(coordinator.run_scheduled())
    await asyncio.sleep(0)
    assert rig.run.calls == 1
    rig.run.gate.set()
    await asyncio.gather(now_task, scheduled)
    assert rig.run.calls == 2


async def test_now_says_when_the_run_made_no_draft(rig: Rig) -> None:
    """A paused or failed run (already alerted) is still reported in the chat."""
    rig.run.result = None
    await rig.say("on_now")
    await rig.settle()
    assert rig.replies == [NOW_STARTED, NOW_NO_DRAFT]


async def test_now_with_an_unexpected_error_alerts_and_tells_the_chat(
    rig: Rig, alerts: list[str]
) -> None:
    """ERR-01/ERR-16: an exception in the background run is never lost."""
    rig.run.error = RuntimeError("boom")
    await rig.say("on_now")
    await rig.settle()
    assert rig.replies == [NOW_STARTED, FAILED]
    assert len(alerts) == 1 and "/now" in alerts[0] and "boom" in alerts[0]


async def test_a_failed_failure_reply_is_logged_not_raised(
    rig: Rig, alerts: list[str], caplog: pytest.LogCaptureFixture
) -> None:
    """ERR-21: if the chat cannot be told, the alert has still gone out."""
    rig.run.error = RuntimeError("boom")
    update = SimpleNamespace(
        effective_message=SimpleNamespace(
            reply_text=lambda _t: (_ for _ in ()).throw(RuntimeError("chat down"))
        )
    )
    await rig.service._fail(update, "/now", RuntimeError("boom"))  # type: ignore[arg-type]
    assert len(alerts) == 1
    assert "Could not tell the chat" in caplog.text


def test_install_registers_the_four_commands() -> None:
    """All four commands are routed to the service."""
    handlers: list[Any] = []
    application = SimpleNamespace(add_handler=handlers.append)
    service = SimpleNamespace(on_seed=1, on_seeds=2, on_skip=3, on_now=4)
    install_commands(application, service)  # type: ignore[arg-type]
    assert [min(h.commands) for h in handlers] == ["seed", "seeds", "skip", "now"]
