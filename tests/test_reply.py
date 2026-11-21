"""Tests for the /reply command (SA-53 acceptance criteria)."""

import ast
import asyncio
from collections.abc import Coroutine, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from telegram.error import TelegramError
from telegram.ext import CallbackQueryHandler, CommandHandler

from humblebrag import bot as bot_module
from humblebrag import reply as reply_module
from humblebrag.alerts import configure_alerts
from humblebrag.claude_client import ClaudeAPIError
from humblebrag.drafts import MAX_REDOS
from humblebrag.fetcher import FetchResult, FetchStatus, TargetType
from humblebrag.guard import GuardError, GuardRejectedError, GuardResult
from humblebrag.preview import callback_data
from humblebrag.reply import (
    BUSY_TEXT,
    COULDNT_READ_TEXT,
    FAILED_TEXT,
    GONE_TEXT,
    NO_REDOS_TEXT,
    NOT_SAFE_TEXT,
    NOT_SUBSTACK_TEXT,
    USAGE_TEXT,
    CheckedReply,
    ReplyService,
    install_reply,
    parse_reply_message,
)
from humblebrag.reply_generator import ReplyTarget

CHAT = 12345
POST_URL = "https://example.substack.com/p/a-humble-post"
NOTE_URL = "https://substack.com/@example/note/c-987654"
PASS = GuardResult(True, "Clearly satire.", "classifier")


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
    """Records what the bot sends, deletes and edits."""

    def __init__(self) -> None:
        """Start empty."""
        self.sent: list[dict[str, Any]] = []
        self.deleted: list[int] = []
        self.edits: list[dict[str, Any]] = []
        self.actions: list[str] = []
        self.delete_error: Exception | None = None

    async def send_message(self, **kwargs: Any) -> SimpleNamespace:
        """Record a message and give it an id."""
        self.sent.append(kwargs)
        return SimpleNamespace(message_id=100 + len(self.sent))

    async def send_chat_action(self, **kwargs: Any) -> None:
        """Record a chat action."""
        self.actions.append(kwargs["action"])

    async def delete_message(self, chat_id: int, message_id: int) -> None:
        """Record a deletion, or fail."""
        if self.delete_error is not None:
            raise self.delete_error
        self.deleted.append(message_id)

    async def edit_message_reply_markup(self, **kwargs: Any) -> None:
        """Record a button removal."""
        self.edits.append(kwargs)

    @property
    def texts(self) -> list[str]:
        """The text of every message sent."""
        return [message["text"] for message in self.sent]


class Rig:
    """A ReplyService with fakes, plus a way to run its background tasks."""

    def __init__(self) -> None:
        """Wire the service to a fake fetcher and generator."""
        self.bot = FakeBot()
        self.tasks: list[Coroutine[Any, Any, None]] = []
        self.application = SimpleNamespace(create_task=self.tasks.append)
        self.context = SimpleNamespace(bot=self.bot, application=self.application)
        self.fetches: list[str] = []
        self.generated: list[tuple[ReplyTarget, str | None]] = []
        self.fetch_result = FetchResult(
            FetchStatus.OK, "", TargetType.COMMENT, "A comment.", "Parent title"
        )
        self.generate_error: Exception | None = None
        self.replies = iter(f"Reply number {n}" for n in range(1, 50))
        self.service = ReplyService(self._fetch, self._generate)

    async def _fetch(self, url: str) -> FetchResult:
        self.fetches.append(url)
        return self.fetch_result

    async def _generate(self, target: ReplyTarget, steer: str | None) -> CheckedReply:
        self.generated.append((target, steer))
        if self.generate_error is not None:
            raise self.generate_error
        return CheckedReply(next(self.replies), PASS)

    def message(self, text: str) -> SimpleNamespace:
        """An update carrying a text message."""
        message = SimpleNamespace(
            text=text, chat_id=CHAT, reply_text=AsyncMock(return_value=None)
        )
        return SimpleNamespace(effective_message=message, effective_chat=None)

    def tap(self, data: str, message_id: int = 101) -> SimpleNamespace:
        """An update carrying a button tap."""
        query = SimpleNamespace(
            data=data,
            answer=AsyncMock(),
            message=SimpleNamespace(message_id=message_id),
        )
        return SimpleNamespace(
            callback_query=query, effective_chat=SimpleNamespace(id=CHAT)
        )

    async def drain(self) -> None:
        """Run every background task started so far."""
        while self.tasks:
            await self.tasks.pop(0)

    async def send(self, text: str) -> SimpleNamespace:
        """Send a /reply message and finish its background work."""
        update = self.message(text)
        await self.service.on_reply(update, self.context)
        await self.drain()
        return update

    async def press(self, data: str, message_id: int = 101) -> SimpleNamespace:
        """Tap a button and finish its background work."""
        update = self.tap(data, message_id)
        await self.service.on_button(update, self.context)
        await self.drain()
        return update


@pytest.fixture
def rig() -> Rig:
    """A fresh rig."""
    return Rig()


def _buttons(message: dict[str, Any]) -> list[str]:
    """The callback data of the buttons on a sent message."""
    markup = message["reply_markup"]
    return [button.callback_data for row in markup.inline_keyboard for button in row]


# AC1: a good link is read and answered


async def test_a_link_is_read_and_answered(rig: Rig) -> None:
    """AC1: the link goes to the fetcher and a generated reply comes back."""
    await rig.send(f"/reply {POST_URL}")
    assert rig.fetches == [POST_URL]
    assert rig.bot.texts == ["Reply number 1"]
    ((target, steer),) = rig.generated
    assert target == ReplyTarget(TargetType.COMMENT, "A comment.", "Parent title")
    assert steer is None


async def test_the_command_works_with_a_bot_suffix(rig: Rig) -> None:
    """AC1: /reply@botname is the same command."""
    await rig.send(f"/reply@SirHumblebragBot {POST_URL}")
    assert rig.fetches == [POST_URL]


# AC2: usage text and nothing generated


@pytest.mark.parametrize(
    "text",
    [
        "/reply",
        "/reply   ",
        "/reply | more smug",
        "/reply http://example.substack.com/p/a-humble-post",
        "/reply example.substack.com/p/a-humble-post",
        "/reply https://example.substack.com/about",
        "/reply https://127.0.0.1/p/x",
        "/reply two words",
        "/reply https://example.substack.com/p/a https://example.substack.com/p/b",
    ],
)
async def test_a_missing_or_bad_url_gets_usage_and_generates_nothing(
    rig: Rig, text: str
) -> None:
    """AC2: usage text, no fetch, no generation, no Claude call."""
    update = await rig.send(text)
    update.effective_message.reply_text.assert_awaited_once_with(USAGE_TEXT)
    assert rig.fetches == []
    assert rig.generated == []
    assert rig.bot.sent == []


@pytest.mark.parametrize("status", [FetchStatus.REJECTED, FetchStatus.NOT_SUBSTACK])
async def test_a_url_the_fetcher_rejects_gets_usage_and_generates_nothing(
    rig: Rig, status: FetchStatus
) -> None:
    """AC2: a non-Substack custom domain is rejected by the fetcher."""
    rig.fetch_result = FetchResult(status, "That is not a Substack page.")
    await rig.send("/reply https://blog.example.org/p/x")
    assert rig.bot.texts == [NOT_SUBSTACK_TEXT]
    assert USAGE_TEXT in NOT_SUBSTACK_TEXT
    assert rig.generated == []


# AC3: couldn't read -> ask to paste, never guess


async def test_a_failed_read_asks_for_pasted_text_and_never_guesses(
    rig: Rig,
) -> None:
    """AC3: nothing is generated from the URL alone."""
    rig.fetch_result = FetchResult(FetchStatus.COULDNT_READ, "The fetch timed out.")
    await rig.send(f"/reply {POST_URL}")
    assert rig.bot.texts == [COULDNT_READ_TEXT]
    assert "Paste" in COULDNT_READ_TEXT
    assert rig.generated == []


# AC4: pasted text skips the fetch


async def test_pasted_text_is_used_and_the_fetch_is_skipped(rig: Rig) -> None:
    """AC4: the lines after the first are the text; no fetch."""
    await rig.send(f"/reply {POST_URL}\nFirst line.\nSecond line.")
    assert rig.fetches == []
    ((target, _),) = rig.generated
    assert target == ReplyTarget(TargetType.POST, "First line.\nSecond line.")


async def test_pasted_text_uses_the_kind_from_the_link(rig: Rig) -> None:
    """AC4/AC7: a Note link with pasted text is answered as a Note."""
    await rig.send(f"/reply {NOTE_URL}\nSome text.")
    assert rig.generated[0][0].kind is TargetType.NOTE


# AC5: steer


def test_the_steer_is_read_from_the_first_line_only() -> None:
    """AC5: a pipe in pasted text is left alone."""
    request = parse_reply_message(f"/reply {POST_URL} | more smug\nA | B | C")
    assert request is not None
    assert request.url == POST_URL
    assert request.steer == "more smug"
    assert request.pasted == "A | B | C"


def test_a_pipe_in_pasted_text_is_not_a_steer() -> None:
    """AC5: no steer on the first line means no steer at all."""
    request = parse_reply_message(f"/reply {POST_URL}\nthis | that")
    assert request is not None
    assert request.steer is None
    assert request.pasted == "this | that"


@pytest.mark.parametrize(
    ("text", "steer"),
    [
        (f"/reply {POST_URL} |", None),
        (f"/reply {POST_URL}|pretend to agree", "pretend to agree"),
        (f"/reply {POST_URL} |   spaced   ", "spaced"),
        (f"/reply {POST_URL}", None),
    ],
)
def test_steer_edge_cases(text: str, steer: str | None) -> None:
    """AC5: surrounding spaces are trimmed and an empty steer is no steer."""
    request = parse_reply_message(text)
    assert request is not None
    assert request.steer == steer


async def test_the_steer_reaches_the_generator(rig: Rig) -> None:
    """AC5: the steer is passed on, for fetched and for pasted text."""
    await rig.send(f"/reply {POST_URL} | more smug")
    await rig.send(f"/reply {POST_URL} | pretend to agree\nPasted | text")
    assert [steer for _, steer in rig.generated] == ["more smug", "pretend to agree"]
    assert rig.generated[1][0].text == "Pasted | text"


def test_a_message_that_is_not_the_command_does_not_parse() -> None:
    """The parser only accepts /reply."""
    assert parse_reply_message("/replyall https://x") is None
    assert parse_reply_message("hello") is None


# AC10: plain output with Redo and Bin


async def test_the_reply_is_plain_text_with_redo_and_bin_buttons(rig: Rig) -> None:
    """AC10: the message is the reply and nothing else, with two buttons."""
    await rig.send(f"/reply {POST_URL}")
    (message,) = rig.bot.sent
    assert message["text"] == "Reply number 1"
    assert "parse_mode" not in message
    assert _buttons(message) == ["rpr:1", "rpb:1"]
    assert [b.text for b in message["reply_markup"].inline_keyboard[0]] == [
        "🔁 Redo",
        "🗑️ Bin",
    ]


async def test_typing_is_shown_while_working(rig: Rig) -> None:
    """The chat sees a typing action rather than a framing message."""
    await rig.send(f"/reply {POST_URL}")
    assert rig.bot.actions == ["typing"]


# AC11: Redo


async def test_redo_reuses_the_text_and_steer_without_fetching_again(
    rig: Rig,
) -> None:
    """AC11: same target, same steer, no second fetch."""
    await rig.send(f"/reply {POST_URL} | more smug")
    await rig.press("rpr:1")
    assert rig.fetches == [POST_URL]
    assert rig.generated[0] == rig.generated[1]
    assert rig.generated[1][1] == "more smug"
    assert rig.bot.texts == ["Reply number 1", "Reply number 2"]


async def test_redo_replaces_the_old_buttons_with_new_ones(rig: Rig) -> None:
    """AC11: the old message loses its buttons; the new one has them."""
    await rig.send(f"/reply {POST_URL}")
    await rig.press("rpr:1", message_id=101)
    assert rig.bot.edits == [{"chat_id": CHAT, "message_id": 101, "reply_markup": None}]
    assert _buttons(rig.bot.sent[1]) == ["rpr:1", "rpb:1"]


async def test_redo_is_limited_to_three(rig: Rig) -> None:
    """AC11: the third redo removes the Redo button; a stale tap is refused."""
    await rig.send(f"/reply {POST_URL}")
    for _ in range(MAX_REDOS):
        await rig.press("rpr:1")
    assert MAX_REDOS == 3
    assert len(rig.generated) == MAX_REDOS + 1
    assert _buttons(rig.bot.sent[-1]) == ["rpb:1"]
    await rig.press("rpr:1")
    assert rig.bot.texts[-1] == NO_REDOS_TEXT
    assert len(rig.generated) == MAX_REDOS + 1


async def test_a_second_tap_while_redoing_is_refused(rig: Rig) -> None:
    """A double tap does not start two redos."""
    await rig.send(f"/reply {POST_URL}")
    await rig.service.on_button(rig.tap("rpr:1"), rig.context)
    await rig.service.on_button(rig.tap("rpr:1"), rig.context)
    assert rig.bot.texts[-1] == BUSY_TEXT
    await rig.drain()
    assert len(rig.generated) == 2


async def test_a_failed_redo_keeps_the_old_reply_and_its_buttons(rig: Rig) -> None:
    """A failed redo does not use up a redo or touch the old message."""
    await rig.send(f"/reply {POST_URL}")
    rig.generate_error = GuardRejectedError("names someone")
    await rig.press("rpr:1")
    assert rig.bot.edits == []
    assert rig.bot.texts[-1] == NOT_SAFE_TEXT
    rig.generate_error = None
    await rig.press("rpr:1")
    assert _buttons(rig.bot.sent[-1]) == ["rpr:1", "rpb:1"]
    assert rig.service._sessions[1].redo_count == 1


# AC12: Bin


async def test_bin_deletes_the_message_and_stores_nothing(rig: Rig) -> None:
    """AC12: the message goes and the session is forgotten."""
    await rig.send(f"/reply {POST_URL}")
    await rig.press("rpb:1", message_id=101)
    assert rig.bot.deleted == [101]
    assert rig.service._sessions == {}
    await rig.press("rpr:1")
    assert rig.bot.texts[-1] == GONE_TEXT
    assert len(rig.generated) == 1


async def test_bin_falls_back_to_removing_buttons_if_delete_fails(rig: Rig) -> None:
    """A message Telegram will not delete still loses its buttons."""
    await rig.send(f"/reply {POST_URL}")
    rig.bot.delete_error = TelegramError("too old")
    await rig.press("rpb:1", message_id=101)
    assert rig.bot.edits == [{"chat_id": CHAT, "message_id": 101, "reply_markup": None}]


async def test_state_is_forgotten_after_a_restart(rig: Rig) -> None:
    """Reply state is memory only: a fresh service has none."""
    fresh = Rig()
    await fresh.press("rpr:7")
    assert fresh.bot.texts == [GONE_TEXT]
    await fresh.press("rpb:7", message_id=55)
    assert fresh.bot.deleted == [55]


async def test_only_a_bounded_number_of_replies_is_remembered(rig: Rig) -> None:
    """Memory does not grow without limit: the oldest session is dropped."""
    rig.service = ReplyService(rig._fetch, rig._generate, max_sessions=2)
    for _ in range(3):
        await rig.send(f"/reply {POST_URL}")
    assert sorted(rig.service._sessions) == [2, 3]


# AC9 / AC14: guard rejection and failures alert


@pytest.mark.parametrize(
    "error", [GuardRejectedError("names someone"), GuardError("x")]
)
async def test_a_guard_rejection_shows_no_reply(
    rig: Rig, alerts: list[str], error: Exception
) -> None:
    """AC9: nothing unsafe is shown; the guard already alerted, so no second alert."""
    rig.generate_error = error
    await rig.send(f"/reply {POST_URL}")
    assert rig.bot.texts == [NOT_SAFE_TEXT]
    assert alerts == []


async def test_a_claude_failure_alerts_and_tells_the_chat(
    rig: Rig, alerts: list[str]
) -> None:
    """AC14: a Claude API failure alerts and nothing fails silently."""
    rig.generate_error = ClaudeAPIError("Claude API returned HTTP 500", 500)
    await rig.send(f"/reply {POST_URL}")
    assert len(alerts) == 1
    assert "/reply failed" in alerts[0]
    assert "HTTP 500" in alerts[0]
    assert rig.bot.texts == [FAILED_TEXT]


async def test_a_claude_failure_during_redo_alerts(rig: Rig, alerts: list[str]) -> None:
    """AC14: the same holds for a redo."""
    await rig.send(f"/reply {POST_URL}")
    rig.generate_error = ClaudeAPIError("down", 503)
    await rig.press("rpr:1")
    assert len(alerts) == 1
    assert rig.bot.texts[-1] == FAILED_TEXT
    assert rig.service._sessions[1].busy is False


async def test_a_telegram_failure_while_sending_alerts(
    rig: Rig, alerts: list[str]
) -> None:
    """AC14: even a failure to deliver the reply is alerted."""
    sends = rig.bot.send_message

    async def flaky(**kwargs: Any) -> SimpleNamespace:
        if "reply_markup" in kwargs:
            raise TelegramError("boom")
        return await sends(**kwargs)

    rig.bot.send_message = flaky
    await rig.send(f"/reply {POST_URL}")
    assert len(alerts) == 1
    assert rig.bot.texts == [FAILED_TEXT]


# AC13 / AC15 / AC16: never posts, no clash, chat id


def _imported_modules(path: Path) -> set[str]:
    """Every module a source file imports."""
    tree = ast.parse(path.read_text())
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add(node.module or "")
    return names


def test_the_reply_code_never_touches_the_publisher_or_the_run_lock() -> None:
    """AC13/AC15: no publisher, Substack library, drafts DB or RunCoordinator."""
    source_dir = Path(reply_module.__file__).parent
    for name in ("reply.py", "reply_generator.py"):
        imported = _imported_modules(source_dir / name)
        assert "humblebrag.publisher" not in imported
        assert "humblebrag.commands" not in imported
        assert not {module for module in imported if module.startswith("substack")}
    assert "Lock" not in (source_dir / "reply.py").read_text().replace("run lock", "")


def test_reply_buttons_and_draft_buttons_do_not_overlap() -> None:
    """AC15: the two patterns are exact complements: every callback matches one."""
    reply_pattern = reply_module.REPLY_CALLBACK_PATTERN
    draft_pattern = reply_module.NOT_REPLY_CALLBACK_PATTERN
    assert bot_module.NOT_REPLY_CALLBACK_PATTERN is draft_pattern
    samples = [
        f"{prefix}{reply_module.CALLBACK_SEPARATOR}{number}"
        for prefix in (reply_module.REDO_PREFIX, reply_module.BIN_PREFIX)
        for number in ("1", "42")
    ]
    samples += [callback_data(action, 1) for action in ("approve", "redo", "bin")]
    samples += ["", "junk", "rp", "rpr:", "rpr:x", "xrpr:1", "rpr:1x", "r:1", "b:1"]
    for data in samples:
        assert (reply_pattern.match(data) is None) != (
            draft_pattern.match(data) is None
        ), data
    for data in samples[:4]:
        assert reply_pattern.match(data) is not None


def test_install_reply_registers_the_command_and_its_buttons() -> None:
    """AC16/AC17: /reply is a command; handlers sit after the chat guard group."""
    handlers: list[tuple[Any, int | None]] = []
    application = SimpleNamespace(
        add_handler=lambda handler, group=0: handlers.append((handler, group))
    )
    install_reply(application, ReplyService(AsyncMock(), AsyncMock()))
    kinds = {type(handler) for handler, _ in handlers}
    assert kinds == {CommandHandler, CallbackQueryHandler}
    command = next(h for h, _ in handlers if isinstance(h, CommandHandler))
    assert "reply" in command.commands
    assert all(group >= 0 > bot_module.GUARD_GROUP for _, group in handlers)


def test_the_bot_command_list_includes_reply() -> None:
    """AC17: /reply is in the bot's command list and /help."""
    assert "reply" in bot_module.COMMANDS
    assert "/reply" in bot_module.help_text()


def test_the_runbook_covers_reply_paste_and_steer() -> None:
    """AC17: the runbook explains /reply, the paste fallback and steer."""
    runbook = (Path(__file__).parent.parent / "docs" / "runbook.md").read_text()
    section = runbook.split("## Reply to a Substack link", 1)[1].split("\n## ", 1)[0]
    for needle in ("/reply", "Paste fallback", "Steer", "Redo", "Bin"):
        assert needle in section


async def test_two_replies_at_once_do_not_interfere(rig: Rig) -> None:
    """AC15: concurrent requests each get their own session."""
    await rig.service.on_reply(rig.message(f"/reply {POST_URL}"), rig.context)
    await rig.service.on_reply(rig.message(f"/reply {NOTE_URL}"), rig.context)
    await asyncio.gather(*(task for task in rig.tasks))
    rig.tasks.clear()
    assert sorted(rig.service._sessions) == [1, 2]


async def test_a_session_with_a_redo_running_is_not_evicted(rig: Rig) -> None:
    """Eviction skips a busy session, so its redo can finish."""
    rig.service = ReplyService(rig._fetch, rig._generate, max_sessions=2)
    await rig.send(f"/reply {POST_URL}")
    await rig.service.on_button(rig.tap("rpr:1"), rig.context)  # redo of 1 pending
    for _ in range(2):  # run the new replies' tasks only, not the pending redo
        await rig.service.on_reply(rig.message(f"/reply {POST_URL}"), rig.context)
        await rig.tasks.pop(1)
    assert 1 in rig.service._sessions
    assert 2 not in rig.service._sessions
    await rig.drain()
    assert _buttons(rig.bot.sent[-1]) == ["rpr:1", "rpb:1"]


async def test_a_reply_binned_during_its_redo_says_so(rig: Rig) -> None:
    """A redo that finishes after Bin tells the chat instead of vanishing."""
    await rig.send(f"/reply {POST_URL}")
    await rig.service.on_button(rig.tap("rpr:1"), rig.context)
    await rig.service.on_button(rig.tap("rpb:1"), rig.context)
    await rig.drain()
    assert rig.bot.texts[-1] == GONE_TEXT
    assert rig.bot.texts.count("Reply number 2") == 0
