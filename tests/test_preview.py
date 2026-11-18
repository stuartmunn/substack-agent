"""Tests for the draft preview and buttons (SA-28 acceptance criteria)."""

import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from telegram import Bot, Update, User
from telegram.ext import Application, ExtBot

from humblebrag import bot
from humblebrag.alerts import configure_alerts
from humblebrag.buttons import (
    ALREADY_HANDLED_TEXT,
    NOT_AVAILABLE_TEXT,
    UNKNOWN_BUTTON_TEXT,
    UNKNOWN_DRAFT_TEXT,
    register_action,
)
from humblebrag.config import load_config
from humblebrag.drafts import Draft, DraftStore, DraftStoreError
from humblebrag.preview import (
    ACTION_APPROVE,
    ACTION_BIN,
    ACTION_REDO,
    NO_GUARD_RESULT,
    TELEGRAM_MESSAGE_LIMIT,
    callback_data,
    format_preview,
    parse_callback_data,
    preview_keyboard,
    send_preview,
    split_for_telegram,
    telegram_length,
)

CHAT = 12345
STRANGER = 99999
ENV = {
    "TELEGRAM_BOT_TOKEN": "123456:TESTTOKEN",
    "TELEGRAM_CHAT_ID": str(CHAT),
    "ANTHROPIC_API_KEY": "key",
    "CLAUDE_MODEL": "m",
    "SUBSTACK_PUBLICATION_URL": "https://example.substack.com",
    "SUBSTACK_COOKIE": "cookie",
}
GUARD_LINE = "🛡️ Guard passed: Clearly satire."
NOW = datetime(2026, 11, 18, 8, 30, tzinfo=UTC)


def _draft(body: str = "A short body.", draft_id: int = 7, redo: int = 2) -> Draft:
    """A pending draft with the given body."""
    return Draft(
        id=draft_id,
        seed="Spilled coffee",
        title="Coffee and Me",
        subtitle="A humble brew",
        body=body,
        steer_history=(),
        redo_count=redo,
        status="pending",
        created_at=NOW,
        resolved_at=None,
        substack_url=None,
    )


def _long_body(paragraphs: int = 120) -> str:
    """A body of many ~60-char paragraphs, well over the Telegram limit."""
    return "\n\n".join(f"Paragraph {n}: " + "humbled " * 6 for n in range(paragraphs))


# AC1: what the preview shows


def test_short_preview_is_one_message_with_everything() -> None:
    """AC1: title, subtitle, body, seed, redo count and guard result."""
    [message] = format_preview(_draft(), GUARD_LINE)
    for expected in (
        "Coffee and Me",
        "A humble brew",
        "A short body.",
        "Seed: Spilled coffee",
        "Redo count: 2",
        GUARD_LINE,
    ):
        assert expected in message


def test_missing_guard_result_is_said_not_hidden() -> None:
    """AC1: a draft with no recorded guard result says so."""
    [message] = format_preview(_draft(), None)
    assert NO_GUARD_RESULT in message


def test_long_body_is_split_into_messages_within_the_limit() -> None:
    """AC1: nothing over the Telegram limit; no text lost."""
    body = _long_body()
    chunks = format_preview(_draft(body), GUARD_LINE)
    assert len(chunks) > 1
    assert all(telegram_length(chunk) <= TELEGRAM_MESSAGE_LIMIT for chunk in chunks)
    joined = "\n".join(chunks)
    assert all(f"Paragraph {n}:" in joined for n in range(120))
    assert chunks[0].startswith("Coffee and Me\nA humble brew")


def test_seed_redo_and_guard_end_up_together_in_the_last_message() -> None:
    """AC1: the footer is never split from the buttons' message."""
    chunks = format_preview(_draft(_long_body()), GUARD_LINE)
    last = chunks[-1]
    assert "Seed: Spilled coffee" in last
    assert "Redo count: 2" in last
    assert GUARD_LINE in last
    assert not any("Seed:" in chunk for chunk in chunks[:-1])


def test_footer_gets_its_own_message_when_the_body_fills_the_last_one() -> None:
    """AC1: if the footer does not fit after the body, it is a final message."""
    body = "x" * (TELEGRAM_MESSAGE_LIMIT - 40)
    chunks = format_preview(_draft(body), GUARD_LINE)
    assert len(chunks) == 2
    assert chunks[1].startswith("Seed:")
    assert all(telegram_length(chunk) <= TELEGRAM_MESSAGE_LIMIT for chunk in chunks)


def test_a_single_line_longer_than_the_limit_is_still_split() -> None:
    """AC1: no whitespace at all still splits at the limit."""
    chunks = split_for_telegram("y" * (TELEGRAM_MESSAGE_LIMIT * 2 + 5))
    assert [len(chunk) for chunk in chunks] == [
        TELEGRAM_MESSAGE_LIMIT,
        TELEGRAM_MESSAGE_LIMIT,
        5,
    ]


def test_limit_is_measured_in_utf16_units_like_telegram() -> None:
    """AC1: emoji count as two units, so emoji-heavy text still fits."""
    text = "😀" * TELEGRAM_MESSAGE_LIMIT
    chunks = split_for_telegram(text)
    assert all(telegram_length(chunk) <= TELEGRAM_MESSAGE_LIMIT for chunk in chunks)
    assert "".join(chunks) == text


def test_splitting_prefers_line_ends_over_mid_word() -> None:
    """AC1: chunks break on newlines, not inside words."""
    lines = [("word " * 40).strip() for _ in range(60)]
    chunks = split_for_telegram("\n".join(lines))
    assert all(chunk.endswith("word") for chunk in chunks)


# AC2: buttons on the final message, keyed to the draft id


class FakeBot:
    """Records send_message calls."""

    def __init__(self) -> None:
        """Start with no messages."""
        self.sent: list[dict[str, Any]] = []

    async def send_message(self, **kwargs: Any) -> None:
        """Record the call."""
        self.sent.append(kwargs)


async def test_buttons_are_on_the_final_message_only() -> None:
    """AC2: earlier parts carry no keyboard; the last one does."""
    fake = FakeBot()
    await send_preview(fake, CHAT, _draft(_long_body()), GUARD_LINE)  # type: ignore[arg-type]
    assert len(fake.sent) > 1
    assert all(call["reply_markup"] is None for call in fake.sent[:-1])
    assert fake.sent[-1]["reply_markup"] is not None
    assert all(call["chat_id"] == CHAT for call in fake.sent)


async def test_a_short_preview_is_one_message_with_buttons() -> None:
    """AC2: a single-message preview carries the buttons."""
    fake = FakeBot()
    await send_preview(fake, CHAT, _draft(), GUARD_LINE)  # type: ignore[arg-type]
    assert len(fake.sent) == 1
    assert fake.sent[0]["reply_markup"] is not None


def test_buttons_are_approve_redo_bin_keyed_to_the_draft_id() -> None:
    """AC2: three buttons whose callback data carries the draft id."""
    [row] = preview_keyboard(42).inline_keyboard
    assert [button.text for button in row] == ["✅ Approve", "🔁 Redo", "🗑️ Bin"]
    assert [button.callback_data for button in row] == ["a:42", "r:42", "b:42"]
    assert all(len(b.callback_data.encode()) <= 64 for b in row)


def test_two_drafts_have_distinct_buttons() -> None:
    """AC2: each pending draft keeps its own buttons."""
    first = [b.callback_data for b in preview_keyboard(1).inline_keyboard[0]]
    second = [b.callback_data for b in preview_keyboard(2).inline_keyboard[0]]
    assert not set(first) & set(second)


@pytest.mark.parametrize("action", [ACTION_APPROVE, ACTION_REDO, ACTION_BIN])
def test_callback_data_round_trips(action: str) -> None:
    """AC2: parsing gives back the action and the draft id."""
    assert parse_callback_data(callback_data(action, 123)) == (action, 123)


@pytest.mark.parametrize(
    "data", [None, "", "a", "a:", "a:x", "a:-1", "z:1", "a:1:2", "approve", "a:١"]
)
def test_malformed_callback_data_is_rejected(data: str | None) -> None:
    """AC2: anything but "<a|r|b>:<digits>" is not a button of ours."""
    assert parse_callback_data(data) is None


# AC3: taps on resolved drafts, through the real handler chain


@pytest.fixture
async def application(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> AsyncIterator[Application]:
    """An initialised application with a draft store and stubbed Telegram."""
    me = User(id=1, first_name="humblebrag", is_bot=True, username="humblebrag_bot")
    monkeypatch.setattr(ExtBot, "get_me", AsyncMock(return_value=me))
    monkeypatch.setattr(Bot, "bot", property(lambda _self: me))
    monkeypatch.setattr(Bot, "send_message", AsyncMock())
    monkeypatch.setattr(Bot, "answer_callback_query", AsyncMock())
    app = bot.build_application(load_config(ENV), DraftStore(tmp_path / "d.db"))
    async with app:
        yield app


def _tap(app: Application, data: str, chat: int = CHAT) -> Update:
    """A callback-query update for a tap on a button."""
    user = {"id": chat, "is_bot": False, "first_name": "X"}
    message = {
        "message_id": 1,
        "date": 0,
        "chat": {"id": chat, "type": "private"},
        "from": {"id": 1, "is_bot": True, "first_name": "bot"},
        "text": "draft",
    }
    payload = {
        "update_id": 1,
        "callback_query": {
            "id": "1",
            "from": user,
            "chat_instance": "c",
            "data": data,
            "message": message,
        },
    }
    return Update.de_json(payload, app.bot)


def _store(app: Application) -> DraftStore:
    """The application's draft store."""
    return app.bot_data["draft_store"]


def _replies() -> list[str]:
    """Texts sent through the stubbed Bot.send_message."""
    return [call.kwargs["text"] for call in Bot.send_message.await_args_list]


@pytest.mark.parametrize("resolve", ["publish", "bin"])
async def test_tap_on_a_resolved_draft_replies_already_handled(
    application: Application, resolve: str
) -> None:
    """AC3: any button on a published or binned draft gets "already handled"."""
    store = _store(application)
    draft = store.create("seed", "T", "S", "B")
    if resolve == "publish":
        store.mark_published(draft.id, "https://example.substack.com/p/x")
    else:
        store.mark_binned(draft.id)
    ran = AsyncMock()
    register_action(application, ACTION_APPROVE, ran)
    for action in (ACTION_APPROVE, ACTION_REDO, ACTION_BIN):
        await application.process_update(
            _tap(application, callback_data(action, draft.id))
        )
    assert _replies() == [ALREADY_HANDLED_TEXT] * 3
    assert "already handled" in ALREADY_HANDLED_TEXT.lower()
    ran.assert_not_awaited()


async def test_tap_on_a_pending_draft_runs_the_registered_action(
    application: Application,
) -> None:
    """AC2/3: pending drafts dispatch to the handler for that button."""
    draft = _store(application).create("seed", "T", "S", "B")
    handler = AsyncMock()
    register_action(application, ACTION_REDO, handler)
    await application.process_update(
        _tap(application, callback_data(ACTION_REDO, draft.id))
    )
    handler.assert_awaited_once()
    assert handler.await_args.args[2].id == draft.id
    assert _replies() == []
    Bot.answer_callback_query.assert_awaited()


async def test_tap_with_no_registered_action_says_so(
    application: Application, caplog: pytest.LogCaptureFixture
) -> None:
    """A pending draft whose action is not wired yet gets a clear reply."""
    draft = _store(application).create("seed", "T", "S", "B")
    with caplog.at_level(logging.WARNING):
        await application.process_update(
            _tap(application, callback_data(ACTION_BIN, draft.id))
        )
    assert _replies() == [NOT_AVAILABLE_TEXT]
    assert "No handler registered" in caplog.text


async def test_tap_on_an_unknown_draft_is_answered(application: Application) -> None:
    """A button for a draft that does not exist gets a reply, not a crash."""
    await application.process_update(_tap(application, "a:999"))
    assert _replies() == [UNKNOWN_DRAFT_TEXT]


async def test_malformed_button_data_is_answered_and_logged(
    application: Application, caplog: pytest.LogCaptureFixture
) -> None:
    """Data that is not ours gets a reply, so Telegram's spinner stops."""
    with caplog.at_level(logging.WARNING):
        await application.process_update(_tap(application, "approve"))
    assert _replies() == [UNKNOWN_BUTTON_TEXT]
    assert "malformed" in caplog.text


async def test_a_store_failure_alerts_and_replies(
    application: Application, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ERR-01: an unreadable database alerts Stuart and tells the chat."""
    sent: list[str] = []

    async def sender(text: str) -> None:
        sent.append(text)

    configure_alerts(sender)
    try:
        monkeypatch.setattr(
            _store(application),
            "get",
            lambda _id: (_ for _ in ()).throw(DraftStoreError("x")),
        )
        await application.process_update(_tap(application, "a:1"))
    finally:
        configure_alerts(None)
    assert len(sent) == 1
    assert "buttons" in sent[0]
    assert len(_replies()) == 1


async def test_a_stranger_cannot_press_the_buttons(application: Application) -> None:
    """SEC-06: taps from another chat are dropped before reaching any action."""
    draft = _store(application).create("seed", "T", "S", "B")
    handler = AsyncMock()
    register_action(application, ACTION_BIN, handler)
    await application.process_update(
        _tap(application, callback_data(ACTION_BIN, draft.id), chat=STRANGER)
    )
    handler.assert_not_awaited()
    assert _replies() == []
    assert _store(application).get(draft.id).status == "pending"


def test_application_without_a_store_has_no_button_handler() -> None:
    """The skeleton alone (no store) does not route buttons."""
    application = bot.build_application(load_config(ENV))
    assert "draft_store" not in application.bot_data
