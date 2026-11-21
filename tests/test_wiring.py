"""Tests for the assembled runtime and the seed re-key it relies on (SA-35)."""

import asyncio
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram.ext import CommandHandler

from humblebrag import bot
from humblebrag.bot import ALLOWED_CHAT_KEY, build_application
from humblebrag.buttons import ACTIONS_KEY, STORE_KEY
from humblebrag.config import load_config
from humblebrag.preview import ACTION_APPROVE, ACTION_BIN, ACTION_REDO
from humblebrag.seed_queue import ReservationStateError, SeedQueue, UnknownDraftError
from humblebrag.wiring import Runtime

ENV = {
    "TELEGRAM_BOT_TOKEN": "123456:TESTTOKEN",
    "TELEGRAM_CHAT_ID": "12345",
    "ANTHROPIC_API_KEY": "sk-ant-key",
    "CLAUDE_MODEL": "claude-test-model",
    "SUBSTACK_PUBLICATION_URL": "https://humblebrag.substack.com",
    "SUBSTACK_COOKIE": "s%3ASECRET-SID",
}


def _queue(tmp_path: Path) -> SeedQueue:
    seed = tmp_path / "seed.md"
    seed.write_text("- [ ] First seed\n- [ ] Second seed\n")
    return SeedQueue(seed, lambda: date(2026, 11, 18))


def test_rekey_moves_a_reservation(tmp_path: Path) -> None:
    """The provisional reservation becomes the draft's, holding the same seed."""
    queue = _queue(tmp_path)
    queue.pick("run-abc")
    queue.rekey("run-abc", "7")
    assert queue._load_reservations() == {"7": "First seed"}
    queue.approve("7")  # Approve can now find it
    assert "2026-11-18" in (tmp_path / "seed.md").read_text()


def test_rekey_of_an_unknown_reservation_fails_cleanly(tmp_path: Path) -> None:
    """No reservation under the old key is a typed error."""
    with pytest.raises(UnknownDraftError):
        _queue(tmp_path).rekey("nope", "7")


def test_rekey_never_overwrites_an_existing_reservation(tmp_path: Path) -> None:
    """A clash is refused and both reservations survive."""
    queue = _queue(tmp_path)
    queue.pick("run-abc")
    queue.pick("7")
    with pytest.raises(ReservationStateError):
        queue.rekey("run-abc", "7")
    assert queue._load_reservations() == {"run-abc": "First seed", "7": "Second seed"}


def test_building_a_runtime_touches_no_files(tmp_path: Path) -> None:
    """Constructing a Runtime is free: nothing is created until setup()."""
    Runtime(load_config(ENV), data_dir=tmp_path / "data")
    assert not (tmp_path / "data").exists()


def test_setup_installs_every_handler_and_builds_the_pipeline(tmp_path: Path) -> None:
    """Approve, Redo and Bin are all registered, and the scheduler is ready."""
    config = load_config(ENV)
    application = build_application(config)
    runtime = Runtime(config, data_dir=tmp_path)
    runtime.setup(application)
    actions = application.bot_data[ACTIONS_KEY]
    assert {ACTION_APPROVE, ACTION_REDO, ACTION_BIN} <= set(actions)
    assert STORE_KEY in application.bot_data
    assert application.bot_data[ALLOWED_CHAT_KEY] == 12345
    assert runtime.pipeline is not None
    assert runtime.scheduler is not None
    assert runtime.backup is not None
    assert (tmp_path / "humblebrag.db").exists()


def test_setup_installs_the_four_commands(tmp_path: Path) -> None:
    """/seed, /seeds, /skip and /now are routed once setup() has run."""
    config = load_config(ENV)
    application = build_application(config)
    Runtime(config, data_dir=tmp_path).setup(application)
    commands = {
        command
        for handler in application.handlers[0]
        if isinstance(handler, CommandHandler)
        for command in handler.commands
    }
    assert {"start", "help", "seed", "seeds", "skip", "now"} <= commands


async def test_start_runs_the_session_checks_and_the_scheduler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """start() checks the session at startup, then runs monitor and scheduler."""
    config = load_config(ENV)
    runtime = Runtime(config, data_dir=tmp_path)
    runtime.setup(build_application(config))
    seen: list[str] = []

    async def check() -> bool:
        seen.append("startup check")
        return True

    async def mark() -> None:
        seen.append("mark")

    async def monitor_loop() -> None:
        seen.append("monitor")

    async def scheduler_loop() -> None:
        seen.append("scheduler")

    async def backup_loop() -> None:
        seen.append("backup")

    monkeypatch.setattr(runtime.publisher, "check_session_or_alert", check)
    monkeypatch.setattr(runtime.monitor, "mark_checked_if_due", mark)
    monkeypatch.setattr(runtime.monitor, "run_forever", monitor_loop)
    monkeypatch.setattr(runtime.scheduler, "run_forever", scheduler_loop)
    monkeypatch.setattr(runtime.backup, "run_forever", backup_loop)
    await asyncio.wait_for(runtime.start(), timeout=5)
    # the monitor only starts after the startup check has been made and recorded
    assert seen.index("startup check") < seen.index("mark") < seen.index("monitor")
    assert {"startup check", "mark", "monitor", "scheduler", "backup"} <= set(seen)


async def test_start_before_setup_is_an_error(tmp_path: Path) -> None:
    """A mis-wired start fails loudly instead of running nothing."""
    with pytest.raises(AssertionError):
        await Runtime(load_config(ENV), data_dir=tmp_path).start()


async def test_run_bot_calls_setup_before_polling_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Handlers must be installed before polling: setup runs first."""
    order: list[str] = []
    updater = SimpleNamespace(start_polling=AsyncMock(), stop=AsyncMock(), running=True)
    app = MagicMock()
    app.updater = updater
    app.running = True
    app.start = AsyncMock()
    app.stop = AsyncMock()
    app.__aenter__ = AsyncMock(return_value=app)
    app.__aexit__ = AsyncMock(return_value=None)
    updater.start_polling.side_effect = lambda **_k: order.append("polling")
    monkeypatch.setattr(bot, "build_application", lambda _config: app)

    def setup(application: object) -> None:
        assert application is app
        order.append("setup")

    task = asyncio.create_task(bot.run_bot(load_config(ENV), setup=setup))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert order == ["setup", "polling"]


async def test_a_failing_setup_stops_the_bot(monkeypatch: pytest.MonkeyPatch) -> None:
    """A setup error propagates (process exits, main alerts) and polling never starts."""
    app = MagicMock()
    app.__aenter__ = AsyncMock(return_value=app)
    app.__aexit__ = AsyncMock(return_value=None)
    monkeypatch.setattr(bot, "build_application", lambda _config: app)

    def setup(_application: object) -> None:
        raise RuntimeError("persona missing")

    with pytest.raises(RuntimeError, match="persona missing"):
        await bot.run_bot(load_config(ENV), setup=setup)
    app.updater.start_polling.assert_not_called()


async def test_the_reply_command_is_built_on_the_guarded_generator(
    tmp_path: Path,
) -> None:
    """SA-53 AC9: /reply is wired to the guard's reply check, with no bypass."""
    from humblebrag import wiring
    from humblebrag.fetcher import TargetType
    from humblebrag.reply import ReplyService
    from humblebrag.reply_generator import ReplyTarget

    config = load_config(ENV)
    application = build_application(config)
    seen: dict[str, object] = {}

    async def fake_checked(generate, reply_text, guard, steer):  # type: ignore[no-untyped-def]
        seen["steer"] = steer
        seen["reply"] = reply_text(await generate(steer))
        return seen["reply"], "RESULT"

    async def fake_generate(self, target, steer):  # type: ignore[no-untyped-def]
        return "A reply."

    original = wiring.generate_checked_reply
    wiring.generate_checked_reply = fake_checked
    wiring.ReplyGenerator.generate = fake_generate  # type: ignore[method-assign]
    try:
        Runtime(config, data_dir=tmp_path).setup(application)
        handlers = [h for group in application.handlers.values() for h in group]
        service = next(
            h.callback.__self__
            for h in handlers
            if isinstance(h, CommandHandler) and "reply" in h.commands
        )
        assert isinstance(service, ReplyService)
        checked = await service._generate(ReplyTarget(TargetType.NOTE, "x"), "smug")
    finally:
        wiring.generate_checked_reply = original
    assert seen == {"steer": "smug", "reply": "A reply."}
    assert checked.text == "A reply."
