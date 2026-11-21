"""Tests for /seed, /seedtop, /seeds, /unseed, /skip and /now (SA-36, SA-45, SA-46)."""

import asyncio
import fcntl
import threading
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from humblebrag import scheduler as scheduler_module
from humblebrag.alerts import configure_alerts
from humblebrag.commands import (
    FAILED,
    NOW_BUSY,
    NOW_NO_DRAFT,
    NOW_STARTED,
    SEED_USAGE,
    SEEDS_EMPTY,
    SEEDS_SHOWN,
    SEEDTOP_USAGE,
    UNSEED_USAGE,
    CommandService,
    RunCoordinator,
    install_commands,
)
from humblebrag.config import load_config
from humblebrag.file_lock import LockTimeoutError, file_lock
from humblebrag.scheduler import Claim, SchedulerState
from humblebrag.seed_queue import MAX_SEED_LENGTH, AddedSeed, SeedQueue

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

    def broken(_text: str) -> AddedSeed:
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

    def broken(_today: date) -> date:
        raise RuntimeError("read-only")

    monkeypatch.setattr(rig.state, "skip_next_run", broken)
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


def test_install_registers_the_six_commands() -> None:
    """All six commands are routed to the service."""
    handlers: list[Any] = []
    application = SimpleNamespace(add_handler=handlers.append)
    service = SimpleNamespace(
        on_seed=1, on_seedtop=2, on_seeds=3, on_unseed=4, on_skip=5, on_now=6
    )
    install_commands(application, service)  # type: ignore[arg-type]
    assert [min(h.commands) for h in handlers] == [
        "seed",
        "seedtop",
        "seeds",
        "unseed",
        "skip",
        "now",
    ]


def test_the_count_is_taken_under_the_same_lock_as_the_append(rig: Rig) -> None:
    """The count in the confirmation is the queue size at the moment of the append."""
    assert rig.queue.add_seed("Third") == AddedSeed("Third", 3)
    assert rig.queue.add_seed("Fourth") == AddedSeed("Fourth", 4)


def test_reading_the_claim_and_setting_the_skip_share_one_lock(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CON-05: a held state lock blocks /skip, so it cannot race a claim."""
    monkeypatch.setattr(
        scheduler_module, "file_lock", lambda path: file_lock(path, timeout=0.05)
    )
    with rig.state._lock_path.open("a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        with pytest.raises(LockTimeoutError):
            rig.state.skip_next_run(TODAY)
    assert rig.state.read().skip_next is False


def test_skip_next_run_names_today_until_it_is_claimed(rig: Rig) -> None:
    """The returned day is today before the claim and tomorrow after it."""
    assert rig.state.skip_next_run(TODAY) == TODAY
    rig.state.claim(TODAY)
    assert rig.state.skip_next_run(TODAY) == date(2026, 11, 19)


# SA-45: /unseed


async def test_unseed_skips_the_numbered_seed_and_confirms_its_text(rig: Rig) -> None:
    """SA-45 AC1/AC2: /unseed 2 skips the 2nd listed seed and echoes its text."""
    await rig.say("on_seeds")
    assert "2. Second seed" in rig.replies[0]
    await rig.say("on_unseed", "2")
    assert rig.replies[1].startswith("Skipped, so it won't be used: Second seed")
    assert rig.seed_path.read_text() == (
        "- [ ] First seed\n- [x] Old seed (used 2026-11-01)\n"
        "- [x] Second seed (skipped 2026-11-18)\n"
    )


async def test_a_skipped_seed_is_no_longer_listed_by_seeds(rig: Rig) -> None:
    """SA-45 AC3: /seeds after /unseed shows the shorter list, renumbered."""
    await rig.say("on_unseed", "1")
    await rig.say("on_seeds")
    assert rig.replies[1] == "1 unused seeds, in order:\n1. Second seed"


@pytest.mark.parametrize(
    "args",
    [[], ["abc"], ["1", "2"], ["-1"], ["1.5"], ["\u00b2"], ["\u0663"], [""], ["1e3"]],
)
async def test_unseed_with_a_missing_or_non_numeric_argument_is_refused(
    rig: Rig, args: list[str]
) -> None:
    """SA-45 AC4: bad arguments get the usage line and change nothing."""
    await rig.say("on_unseed", *args)
    assert rig.replies == [UNSEED_USAGE]
    assert rig.seed_path.read_text() == SEEDS


@pytest.mark.parametrize("number", ["0", "3", "99", "9" * 9])
async def test_unseed_with_a_number_outside_the_list_is_refused(
    rig: Rig, number: str
) -> None:
    """SA-45 AC4: out of range says which numbers are valid; nothing changes."""
    await rig.say("on_unseed", number)
    assert rig.replies == [
        "Not skipped. Pick a number from 1 to 2, as listed by /seeds."
    ]
    assert rig.seed_path.read_text() == SEEDS


@pytest.mark.parametrize("number", ["9" * 10, "9" * 5000])
async def test_unseed_with_an_absurdly_long_number_is_refused_not_a_crash(
    rig: Rig, number: str
) -> None:
    """SA-45 AC4: int() would raise on thousands of digits; the usage line is
    sent instead and nothing changes."""
    await rig.say("on_unseed", number)
    assert rig.replies == [UNSEED_USAGE]
    assert rig.seed_path.read_text() == SEEDS


async def test_unseed_of_a_seed_reserved_by_a_pending_draft_is_refused(
    rig: Rig, alerts: list[str]
) -> None:
    """SA-45 AC5: the reply tells Stuart to Bin or Approve the draft first; it
    is a refusal, not a failure, so no alert."""
    rig.queue.pick("draft-1")
    await rig.say("on_unseed", "1")
    reply = (
        "Not skipped. That seed is reserved by a pending draft. "
        "Bin or Approve that draft first, then try again."
    )
    assert rig.replies == [reply]
    assert rig.seed_path.read_text() == SEEDS
    assert alerts == []


async def test_seed_can_add_a_skipped_seed_again(rig: Rig) -> None:
    """SA-45 AC7: after /unseed, /seed with the same text adds a fresh unused seed."""
    await rig.say("on_unseed", "1")
    await rig.say("on_seed", "First", "seed")
    assert rig.replies[1] == "Added to the queue (2 unused): First seed"
    assert rig.queue.unused_texts() == ["Second seed", "First seed"]
    assert "- [x] First seed (skipped 2026-11-18)" in rig.seed_path.read_text()


async def test_unseed_failure_alerts_and_tells_the_chat(
    rig: Rig, alerts: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """ERR-01/TST-04: a broken queue is an alert and a reply, not a crash."""

    def broken(_number: int) -> str:
        raise RuntimeError("disk full")

    monkeypatch.setattr(rig.queue, "skip_seed", broken)
    await rig.say("on_unseed", "1")
    assert rig.replies == [FAILED]
    assert len(alerts) == 1
    assert "/unseed" in alerts[0]
    assert "disk full" in alerts[0]


async def test_unseed_waits_for_the_lock_in_a_worker_thread(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ERR-13: the blocking queue call runs off the event loop."""
    threads: list[int] = []
    real = rig.queue.skip_seed

    def spy(number: int) -> str:
        threads.append(threading.get_ident())
        return real(number)

    monkeypatch.setattr(rig.queue, "skip_seed", spy)
    await rig.say("on_unseed", "1")
    assert threads
    assert threads[0] != threading.get_ident()


# SA-46: /seedtop


async def test_seedtop_puts_the_seed_first_and_confirms(rig: Rig) -> None:
    """SA-46 AC1/AC4/AC11: it lands above the first unused seed, the reply gives
    position 1 and the count, and /seeds then lists it as number 1."""
    await rig.say("on_seedtop", "Timely", "idea")
    assert rig.replies == ["Added at position 1 (3 unused): Timely idea"]
    assert rig.seed_path.read_text() == (
        "- [ ] Timely idea\n"
        "- [ ] First seed\n- [x] Old seed (used 2026-11-01)\n- [ ] Second seed\n"
    )
    await rig.say("on_seeds")
    assert "1. Timely idea" in rig.replies[1]


@pytest.mark.parametrize("args", [[], [""], ["   "]])
async def test_seedtop_with_no_text_shows_the_usage(rig: Rig, args: list[str]) -> None:
    """SA-46 AC10: no text at all replies with the usage and changes nothing."""
    await rig.say("on_seedtop", *args)
    assert rig.replies == [SEEDTOP_USAGE]
    assert SEEDTOP_USAGE == "Say what the seed is about: /seedtop <text>"
    assert rig.seed_path.read_text() == SEEDS


async def test_seedtop_rejects_bad_text_like_seed_does(rig: Rig) -> None:
    """SA-46 AC10: over-long and control-character text get /seed's reasons."""
    await rig.say("on_seedtop", "x" * (MAX_SEED_LENGTH + 1))
    await rig.say("on_seedtop", "bad\x07text")
    assert rig.replies == [
        f"Not added. The seed is over {MAX_SEED_LENGTH} characters.",
        "Not added. The seed has control characters.",
    ]
    assert rig.seed_path.read_text() == SEEDS


async def test_seedtop_of_an_unused_duplicate_says_it_moved(rig: Rig) -> None:
    """SA-46 AC7: the old line is skipped, the new one is on top, the reply says moved."""
    await rig.say("on_seedtop", "SECOND", "seed")
    assert rig.replies == ["Moved to position 1 (2 unused): SECOND seed"]
    assert rig.queue.unused_texts() == ["SECOND seed", "First seed"]
    assert "- [x] Second seed (skipped 2026-11-18)" in rig.seed_path.read_text()


async def test_seedtop_of_a_reserved_seed_is_refused_without_an_alert(
    rig: Rig, alerts: list[str]
) -> None:
    """SA-46 AC8: the exact refusal text, nothing changes, no alert."""
    rig.queue.pick("draft-1")
    await rig.say("on_seedtop", "first seed")
    reply = (
        "Not added. That seed is reserved by a pending draft. "
        "Bin or Approve that draft first, then try again."
    )
    assert rig.replies == [reply]
    assert rig.seed_path.read_text() == SEEDS
    assert alerts == []


async def test_seedtop_does_not_start_a_run(rig: Rig) -> None:
    """SA-46 AC13: /seedtop only edits the queue; /now stays separate."""
    await rig.say("on_seedtop", "Timely")
    await rig.settle()
    assert rig.run.calls == 0


async def test_seedtop_failure_alerts_and_tells_the_chat(
    rig: Rig, alerts: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """SA-46 AC12/TST-04: a broken queue is an alert and a reply, not a crash."""

    def broken(_text: str) -> None:
        raise RuntimeError("disk full")

    monkeypatch.setattr(rig.queue, "add_seed_top", broken)
    await rig.say("on_seedtop", "Timely")
    assert rig.replies == [FAILED]
    assert len(alerts) == 1
    assert "/seedtop" in alerts[0]
    assert "disk full" in alerts[0]


async def test_seedtop_runs_the_queue_call_in_a_worker_thread(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ERR-13: the blocking queue call runs off the event loop."""
    threads: list[int] = []
    real = rig.queue.add_seed_top

    def spy(text: str) -> Any:
        threads.append(threading.get_ident())
        return real(text)

    monkeypatch.setattr(rig.queue, "add_seed_top", spy)
    await rig.say("on_seedtop", "Timely")
    assert threads
    assert threads[0] != threading.get_ident()
