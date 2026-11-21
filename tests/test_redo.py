"""Tests for Redo with a steer (SA-29 acceptance criteria)."""

import asyncio
import logging
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from telegram.error import TelegramError

from humblebrag import redo as redo_module
from humblebrag.alerts import configure_alerts
from humblebrag.claude_client import ClaudeAPIError
from humblebrag.drafts import (
    MAX_REDOS,
    Draft,
    DraftStateError,
    DraftStore,
    RedoLimitReachedError,
)
from humblebrag.generator import GeneratedPost
from humblebrag.guard import CheckedPost, GuardResult
from humblebrag.preview import ACTION_APPROVE, ACTION_BIN, ACTION_REDO, preview_keyboard
from humblebrag.redo import (
    FAILED_TEXT,
    NO_REDOS_TEXT,
    NOTHING_PENDING_TEXT,
    STEER_PROMPT,
    STEER_TIMEOUT_SECONDS,
    RedoService,
)

CHAT = 12345
GUARD = GuardResult(True, "Clearly satire.", "classifier")


@pytest.fixture(autouse=True)
def alerts() -> Iterator[list[str]]:
    """Capture alert texts, and unset the sender afterwards."""
    sent: list[str] = []

    async def sender(text: str) -> None:
        sent.append(text)

    configure_alerts(sender)
    yield sent
    configure_alerts(None)


class FakeBot:
    """Records messages and button removals."""

    def __init__(self) -> None:
        """Start empty."""
        self.sent: list[dict[str, Any]] = []
        self.edits: list[dict[str, Any]] = []
        self.edit_error: Exception | None = None

    async def send_message(self, **kwargs: Any) -> None:
        """Record a sent message."""
        self.sent.append(kwargs)

    async def edit_message_reply_markup(self, **kwargs: Any) -> None:
        """Record a button removal (or raise the configured error)."""
        if self.edit_error is not None:
            raise self.edit_error
        self.edits.append(kwargs)

    def texts(self) -> list[str]:
        """The texts of the messages sent so far."""
        return [call["text"] for call in self.sent]


class Regenerator:
    """A fake guarded generator recording its calls."""

    def __init__(self) -> None:
        """Start with no calls."""
        self.calls: list[tuple[str, str | None]] = []
        self.error: Exception | None = None
        self.gate: asyncio.Event | None = None

    async def __call__(self, seed: str, steer: str | None) -> CheckedPost:
        """Record the call; wait on the gate and/or raise if configured."""
        self.calls.append((seed, steer))
        if self.gate is not None:
            await self.gate.wait()
        if self.error is not None:
            raise self.error
        post = GeneratedPost(
            f"New title {len(self.calls)}",
            "New sub",
            "New body",
            "New teaser",
            "New scene",
        )
        return CheckedPost(post, GUARD)


class Clock:
    """A sleep replacement the test releases by hand."""

    def __init__(self) -> None:
        """Start unreleased."""
        self.release = asyncio.Event()
        self.requested: list[float] = []

    async def sleep(self, seconds: float) -> None:
        """Record the requested delay and wait for the release."""
        self.requested.append(seconds)
        await self.release.wait()


def _context(bot: FakeBot) -> Any:
    """A handler context exposing the bot."""
    return SimpleNamespace(bot=bot)


def _tap_update(message_id: int = 50) -> Any:
    """An update for a tap on a button in message ``message_id``."""
    return SimpleNamespace(
        effective_chat=SimpleNamespace(id=CHAT),
        callback_query=SimpleNamespace(message=SimpleNamespace(message_id=message_id)),
    )


def _text_update(text: str) -> Any:
    """An update carrying a plain text message."""
    return SimpleNamespace(
        effective_chat=SimpleNamespace(id=CHAT),
        effective_message=SimpleNamespace(text=text),
    )


class Harness:
    """A service wired to a real temporary store and fakes."""

    def __init__(self, tmp_path: Path) -> None:
        """Build the store, fakes and service."""
        self.store = DraftStore(tmp_path / "d.db")
        self.bot = FakeBot()
        self.regen = Regenerator()
        self.clock = Clock()
        self.service = RedoService(
            self.store, self.regen, STEER_TIMEOUT_SECONDS, self.clock.sleep
        )
        self.draft = self.store.create(
            "Spilled coffee", "Old", "Old sub", "Old body", note_teaser="Old teaser"
        )

    async def tap(self, draft: Draft | None = None, message_id: int = 50) -> None:
        """Tap Redo on the draft."""
        await self.service.on_redo_tap(
            _tap_update(message_id), _context(self.bot), draft or self.draft
        )

    async def reply(self, text: str) -> None:
        """Send a plain text reply."""
        await self.service.on_text(_text_update(text), _context(self.bot))

    async def nosteer(self) -> None:
        """Send /nosteer."""
        await self.service.on_nosteer(_text_update("/nosteer"), _context(self.bot))

    async def settled(self) -> None:
        """Wait until no redo is pending."""
        async with asyncio.timeout(5):
            while self.service._pending is not None:
                await asyncio.sleep(0.005)


@pytest.fixture
def h(tmp_path: Path) -> Harness:
    """A fresh harness."""
    return Harness(tmp_path)


# AC1: the prompt


async def test_tapping_redo_asks_for_a_steer(h: Harness) -> None:
    """AC1: the exact prompt, and nothing is regenerated yet."""
    await h.tap()
    assert h.bot.texts() == [STEER_PROMPT]
    assert STEER_PROMPT == "Steer? Reply with text, or /nosteer."
    assert h.regen.calls == []
    h.service._pending.timeout_task.cancel()


# AC2: a reply regenerates from the same seed with that steer


@pytest.mark.parametrize("steer", ["more trains", None])
async def test_redo_replaces_the_stored_teaser_with_the_new_one(
    h: Harness, steer: str | None
) -> None:
    """SA-47 AC7: with or without a steer, the redo's teaser replaces the old one."""
    before = h.store.get(h.draft.id).note_teaser
    await h.tap()
    if steer is None:
        await h.nosteer()
    else:
        await h.reply(steer)
    await h.settled()
    after = h.store.get(h.draft.id).note_teaser
    assert (before, after) == ("Old teaser", "New teaser")


async def test_reply_regenerates_from_the_same_seed_with_the_steer(h: Harness) -> None:
    """AC2: seed unchanged, steer passed through."""
    await h.tap()
    await h.reply("make it about trains")
    await h.settled()
    assert h.regen.calls == [("Spilled coffee", "make it about trains")]


async def test_the_new_preview_replaces_the_old_one(h: Harness) -> None:
    """AC2: stored content is replaced, old buttons removed, new preview sent."""
    await h.tap(message_id=77)
    await h.reply("more trains")
    await h.settled()
    stored = h.store.get(h.draft.id)
    assert (stored.title, stored.body) == ("New title 1", "New body")
    assert stored.steer_history == ("more trains",)
    assert h.bot.edits == [{"chat_id": CHAT, "message_id": 77, "reply_markup": None}]
    preview = h.bot.sent[-1]
    assert "New title 1" in preview["text"]
    assert "Guard passed: Clearly satire." in preview["text"]
    assert preview["reply_markup"] is not None


async def test_failing_to_remove_old_buttons_does_not_fail_the_redo(
    h: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    """AC2: a Telegram error on the old message is logged; the new preview still goes."""
    h.bot.edit_error = TelegramError("message too old")
    await h.tap()
    with caplog.at_level(logging.WARNING):
        await h.reply("x")
        await h.settled()
    assert "Could not remove old buttons" in caplog.text
    assert "New title 1" in h.bot.sent[-1]["text"]


async def test_nosteer_redoes_with_no_steer(h: Harness) -> None:
    """AC2: /nosteer regenerates with steer None and records no steer."""
    await h.tap()
    await h.nosteer()
    await h.settled()
    assert h.regen.calls == [("Spilled coffee", None)]
    assert h.store.get(h.draft.id).steer_history == ()


async def test_text_with_nothing_pending_is_ignored(h: Harness) -> None:
    """A stray text message does not start a redo."""
    await h.reply("hello")
    assert h.regen.calls == []
    assert h.bot.sent == []


async def test_text_from_another_chat_is_not_taken_as_the_steer(h: Harness) -> None:
    """SEC-14: a reply must come from the chat that tapped Redo."""
    await h.tap()
    stranger = SimpleNamespace(
        effective_chat=SimpleNamespace(id=CHAT + 1),
        effective_message=SimpleNamespace(text="hijack"),
    )
    await h.service.on_text(stranger, _context(h.bot))
    await h.service.on_nosteer(stranger, _context(h.bot))
    assert h.regen.calls == []
    assert h.service._pending is not None
    assert h.bot.sent[-1]["chat_id"] == CHAT + 1
    assert h.bot.sent[-1]["text"] == NOTHING_PENDING_TEXT
    h.service._pending.timeout_task.cancel()


async def test_updates_without_a_chat_are_ignored(h: Harness) -> None:
    """Updates with no chat neither start a redo nor raise."""
    await h.tap()
    nochat = SimpleNamespace(
        effective_chat=None, effective_message=SimpleNamespace(text="x")
    )
    await h.service.on_text(nochat, _context(h.bot))
    await h.service.on_nosteer(nochat, _context(h.bot))
    assert h.regen.calls == []
    h.service._pending.timeout_task.cancel()


async def test_nosteer_with_nothing_pending_says_so(h: Harness) -> None:
    """/nosteer outside a redo gets a clear reply."""
    await h.nosteer()
    assert h.bot.texts() == [NOTHING_PENDING_TEXT]


# AC3: count and the three-redo limit


async def test_redo_count_goes_up_each_time(h: Harness) -> None:
    """AC3: the preview and store show the new count."""
    for expected in (1, 2):
        draft = h.store.get(h.draft.id)
        await h.tap(draft)
        await h.nosteer()
        await h.settled()
        assert h.store.get(h.draft.id).redo_count == expected
        assert f"Redo count: {expected}" in h.bot.sent[-1]["text"]


async def test_after_three_redos_only_approve_and_bin_remain(h: Harness) -> None:
    """AC3: the third redo's preview has no Redo button."""
    for _ in range(MAX_REDOS):
        await h.tap(h.store.get(h.draft.id))
        await h.nosteer()
        await h.settled()
    final = h.bot.sent[-1]["reply_markup"].inline_keyboard[0]
    assert [b.callback_data.split(":")[0] for b in final] == ["a", "b"]
    assert h.store.get(h.draft.id).redo_count == 3


def test_keyboard_hides_redo_only_when_none_are_left() -> None:
    """AC3: with redos left all three buttons show; without, two."""
    full = preview_keyboard(1).inline_keyboard[0]
    assert len(full) == 3
    reduced = preview_keyboard(1, redo_allowed=False).inline_keyboard[0]
    assert [b.text for b in reduced] == ["✅ Approve", "🗑️ Bin"]
    assert ACTION_REDO not in [b.callback_data for b in reduced]
    assert {ACTION_APPROVE, ACTION_BIN} <= {"approve", "bin"}


async def test_a_stale_redo_tap_after_the_limit_is_refused(h: Harness) -> None:
    """AC3: tapping an old Redo button with none left regenerates nothing."""
    for _ in range(MAX_REDOS):
        await h.tap(h.store.get(h.draft.id))
        await h.nosteer()
        await h.settled()
    before = len(h.regen.calls)
    await h.tap(h.store.get(h.draft.id))
    assert h.bot.texts()[-1] == NO_REDOS_TEXT
    assert len(h.regen.calls) == before
    assert h.service._pending is None


def test_store_refuses_a_fourth_redo(tmp_path: Path) -> None:
    """AC3: the limit is enforced by the store, not only the buttons."""
    store = DraftStore(tmp_path / "d.db")
    draft = store.create("s", "T", "S", "B")
    for n in range(MAX_REDOS):
        store.apply_redo(draft.id, f"T{n}", "S", "B", None)
    with pytest.raises(RedoLimitReachedError):
        store.apply_redo(draft.id, "T", "S", "B", None)
    assert store.get(draft.id).redo_count == MAX_REDOS


def test_store_refuses_a_redo_on_a_resolved_draft(tmp_path: Path) -> None:
    """A binned draft cannot be redone, and the refusal releases the write lock."""
    store = DraftStore(tmp_path / "d.db")
    draft = store.create("s", "T", "S", "B")
    store.mark_binned(draft.id)
    with pytest.raises(DraftStateError):
        store.apply_redo(draft.id, "T2", "S", "B", None)
    assert store.get(draft.id).title == "T"
    other = store.create("s2", "T2", "S2", "B2")  # would block if a lock leaked
    assert other.id == draft.id + 1


# AC4: 30 minutes without a reply


async def test_no_reply_in_30_minutes_redoes_with_no_steer(h: Harness) -> None:
    """AC4: after the timeout the redo goes ahead unsteered."""
    await h.tap()
    await asyncio.sleep(0)
    assert h.clock.requested == [30 * 60]
    assert h.regen.calls == []
    h.clock.release.set()
    await h.settled()
    assert h.regen.calls == [("Spilled coffee", None)]
    assert h.store.get(h.draft.id).redo_count == 1
    assert "New title 1" in h.bot.sent[-1]["text"]


async def test_a_reply_cancels_the_timeout(h: Harness) -> None:
    """AC4: replying in time means the timeout never triggers a second redo."""
    await h.tap()
    await asyncio.sleep(0)
    await h.reply("trains")
    await h.settled()
    h.clock.release.set()
    await asyncio.sleep(0.05)
    assert len(h.regen.calls) == 1
    assert h.store.get(h.draft.id).redo_count == 1


async def test_reply_racing_the_timeout_runs_one_redo(h: Harness) -> None:
    """AC4: a reply that arrives as the timeout fires still yields one redo."""
    await h.tap()
    await asyncio.sleep(0)
    h.clock.release.set()
    await h.reply("trains")
    await h.settled()
    assert len(h.regen.calls) == 1


# Concurrency, failure paths


async def test_a_second_redo_tap_while_one_is_waiting_is_refused(h: Harness) -> None:
    """Only one redo at a time; the second tap is told to finish the first."""
    other = h.store.create("seed2", "T2", "S2", "B2")
    await h.tap()
    await h.tap(other)
    assert h.bot.texts()[-1] == redo_module.BUSY_TEXT
    assert h.service._pending.draft_id == h.draft.id
    h.service._pending.timeout_task.cancel()


async def test_text_during_regeneration_does_not_start_another(h: Harness) -> None:
    """While Claude is working, extra replies are ignored."""
    h.regen.gate = asyncio.Event()
    await h.tap()
    first = asyncio.create_task(h.reply("first"))
    await asyncio.sleep(0.02)
    await h.reply("second")
    h.regen.gate.set()
    await first
    await h.settled()
    assert [steer for _, steer in h.regen.calls] == ["first"]


async def test_generation_failure_alerts_and_leaves_the_draft_alone(
    h: Harness, alerts: list[str]
) -> None:
    """ERR-01: a failed regeneration alerts, tells the chat, changes nothing."""
    h.regen.error = ClaudeAPIError("boom", status_code=500)
    await h.tap()
    await h.nosteer()
    await h.settled()
    assert len(alerts) == 1
    assert "Redo of draft 1 failed" in alerts[0]
    assert h.bot.texts()[-1] == FAILED_TEXT
    stored = h.store.get(h.draft.id)
    assert (stored.title, stored.redo_count) == ("Old", 0)
    assert h.bot.edits == []
    assert h.service._pending is None


async def test_redo_of_a_draft_binned_while_waiting_is_already_handled(
    h: Harness,
) -> None:
    """A draft resolved during the steer wait is not regenerated."""
    await h.tap()
    h.store.mark_binned(h.draft.id)
    await h.nosteer()
    await h.settled()
    assert h.regen.calls == []
    assert h.bot.texts()[-1] == "Already handled."


async def test_preview_send_failure_after_saving_alerts(
    h: Harness, alerts: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """ERR-01: if the new preview cannot be sent, Stuart is alerted."""

    async def broken(*_args: Any, **_kwargs: Any) -> None:
        raise TelegramError("network down")

    monkeypatch.setattr(redo_module, "send_preview", broken)
    await h.tap()
    await h.nosteer()
    await h.settled()
    assert len(alerts) == 1
    assert "preview failed" in alerts[0]
    assert h.store.get(h.draft.id).redo_count == 1


def test_default_timeout_is_thirty_minutes() -> None:
    """AC4: the constant is 30 minutes."""
    assert STEER_TIMEOUT_SECONDS == 30 * 60


# Wiring through the real handler chain


async def test_install_redo_wires_tap_reply_and_nosteer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Tap -> prompt -> text steer -> new preview, through the real bot chain."""
    from unittest.mock import AsyncMock

    from telegram import Bot, Update, User
    from telegram.ext import ExtBot

    from humblebrag import bot as bot_module
    from humblebrag.config import load_config
    from humblebrag.redo import install_redo

    me = User(id=1, first_name="hb", is_bot=True, username="hb_bot")
    monkeypatch.setattr(ExtBot, "get_me", AsyncMock(return_value=me))
    monkeypatch.setattr(Bot, "bot", property(lambda _self: me))
    send_message = AsyncMock()
    monkeypatch.setattr(Bot, "send_message", send_message)
    monkeypatch.setattr(Bot, "answer_callback_query", AsyncMock())
    monkeypatch.setattr(Bot, "edit_message_reply_markup", AsyncMock())
    env = {
        "TELEGRAM_BOT_TOKEN": "123456:TESTTOKEN",
        "TELEGRAM_CHAT_ID": str(CHAT),
        "ANTHROPIC_API_KEY": "key",
        "CLAUDE_MODEL": "m",
        "SUBSTACK_PUBLICATION_URL": "https://example.substack.com",
        "SUBSTACK_COOKIE": "cookie",
    }
    store = DraftStore(tmp_path / "d.db")
    draft = store.create("Spilled coffee", "Old", "Old sub", "Old body")
    regen = Regenerator()
    application = bot_module.build_application(load_config(env), store)
    install_redo(application, RedoService(store, regen))
    groups = {
        group
        for group, handlers in application.handlers.items()
        for handler in handlers
        if getattr(handler, "callback", None)
        == application.bot_data["redo_service"].on_text
    }
    assert groups == {redo_module.STEER_GROUP}
    user = {"id": CHAT, "is_bot": False, "first_name": "S"}
    chat = {"id": CHAT, "type": "private"}
    tap = {
        "update_id": 1,
        "callback_query": {
            "id": "1",
            "from": user,
            "chat_instance": "c",
            "data": f"r:{draft.id}",
            "message": {"message_id": 9, "date": 0, "chat": chat, "text": "draft"},
        },
    }
    reply = {
        "update_id": 2,
        "message": {
            "message_id": 10,
            "date": 0,
            "chat": chat,
            "from": user,
            "text": "make it about trains",
        },
    }
    async with application:
        await application.process_update(Update.de_json(tap, application.bot))
        assert send_message.await_args.kwargs["text"] == STEER_PROMPT
        await application.process_update(Update.de_json(reply, application.bot))
        service = application.bot_data["redo_service"]
        async with asyncio.timeout(5):
            while service._pending is not None:
                await asyncio.sleep(0.005)
    assert regen.calls == [("Spilled coffee", "make it about trains")]
    assert "New title 1" in send_message.await_args.kwargs["text"]
