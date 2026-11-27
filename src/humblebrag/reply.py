"""The /reply command: read a Substack link (or pasted text) and answer in
Sir Humblebrag's voice, with Redo and Bin buttons.

Nothing here posts to Substack or touches its session. Reply state (the text
and steer, for Redo) lives in memory only: replies are throwaway. Work runs in
the background so the bot keeps answering other updates, and it never uses the
daily-run lock (``RunCoordinator``), so it cannot clash with a daily run. Its buttons use
their own callback prefix, so they never act on daily drafts.
"""

import logging
import re
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from telegram import (
    Bot,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MaybeInaccessibleMessage,
    Update,
)
from telegram.constants import ChatAction
from telegram.error import TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from humblebrag.alerts import alert, describe_error
from humblebrag.drafts import MAX_REDOS
from humblebrag.fetcher import FetchResult, FetchStatus, TargetType, classify_link
from humblebrag.guard import GuardError, GuardRejectedError, GuardResult
from humblebrag.reply_generator import ReplyTarget

logger = logging.getLogger(__name__)

COMMAND = "reply"
ALERT_EVENT = "reply"
REDO_PREFIX = "rpr"
BIN_PREFIX = "rpb"
CALLBACK_SEPARATOR = ":"
_REPLY_PREFIXES = f"(?:{REDO_PREFIX}|{BIN_PREFIX}){CALLBACK_SEPARATOR}"
_REPLY_BODY = rf"{_REPLY_PREFIXES}\d+$"
REPLY_CALLBACK_PATTERN = re.compile(rf"^{_REPLY_BODY}")
# The exact complement, for the draft button handler: every other callback.
NOT_REPLY_CALLBACK_PATTERN = re.compile(rf"^(?!{_REPLY_BODY})")
MAX_SESSIONS = 20
STEER_SEPARATOR = "|"
REDO_LABEL = "🔁 Redo"
BIN_LABEL = "🗑️ Bin"
_COMMAND_WORD = re.compile(r"^/reply(?:@\w+)?(?=\s|$)")

USAGE_TEXT = (
    "Usage: /reply <https link to a Substack post, Note or comment>\n"
    "Steer on the first line: /reply <link> | more smug\n"
    "Pasted text on the lines after the link skips the fetch."
)
NOT_SUBSTACK_TEXT = "That doesn't look like a Substack page.\n\n" + USAGE_TEXT
COULDNT_READ_TEXT = (
    "I couldn't read that link. Paste the text of it on the lines after the "
    "link and I'll use that:\n/reply <link>\n<pasted text>"
)
NOT_SAFE_TEXT = (
    "I couldn't write a reply that passed the safety check, so there is "
    "nothing to show. I've alerted you."
)
FAILED_TEXT = "That did not work and I've alerted you."
NO_REDOS_TEXT = f"No redos left: a reply gets {MAX_REDOS}."
GONE_TEXT = "That reply is no longer available. Send /reply again."
BUSY_TEXT = "That reply is already being redone."

Fetch = Callable[[str], Awaitable[FetchResult]]
Generate = Callable[[ReplyTarget, str | None], Awaitable["CheckedReply"]]


@dataclass(frozen=True)
class CheckedReply:
    """A reply that passed the guard, with the guard's verdict."""

    text: str
    guard: GuardResult


@dataclass(frozen=True)
class ReplyRequest:
    """A parsed /reply message."""

    url: str
    steer: str | None
    pasted: str | None


@dataclass
class _Session:
    """What Redo needs: the target and steer, and how many redos are used."""

    target: ReplyTarget
    steer: str | None
    redo_count: int = 0
    busy: bool = False


def parse_reply_message(text: str) -> ReplyRequest | None:
    """Parse ``/reply <url> [| steer]`` plus optional pasted lines, or None.

    The steer is read from the first line only, so a ``|`` in pasted text is
    left alone.
    """
    command = _COMMAND_WORD.match(text.strip())
    if command is None:
        return None
    first, _, rest = text.strip()[command.end() :].partition("\n")
    link, _, steer = first.partition(STEER_SEPARATOR)
    url = link.strip()
    if not url or len(url.split()) != 1:
        return None
    return ReplyRequest(url, steer.strip() or None, rest.strip() or None)


def _keyboard(session_id: int, redo_allowed: bool) -> InlineKeyboardMarkup:
    """The Redo (while any are left) and Bin buttons for one reply."""
    buttons = []
    if redo_allowed:
        buttons.append(
            InlineKeyboardButton(
                REDO_LABEL,
                callback_data=f"{REDO_PREFIX}{CALLBACK_SEPARATOR}{session_id}",
            )
        )
    buttons.append(
        InlineKeyboardButton(
            BIN_LABEL, callback_data=f"{BIN_PREFIX}{CALLBACK_SEPARATOR}{session_id}"
        )
    )
    return InlineKeyboardMarkup([buttons])


def _parse_callback(data: str | None) -> tuple[str, int] | None:
    """Return ``(prefix, session id)`` from reply button data, or None."""
    prefix, _, raw_id = (data or "").partition(CALLBACK_SEPARATOR)
    if prefix not in (REDO_PREFIX, BIN_PREFIX):
        return None
    if not (raw_id.isascii() and raw_id.isdigit()):
        return None
    return prefix, int(raw_id)


class ReplyService:
    """Handles /reply and its Redo and Bin buttons."""

    def __init__(
        self, fetch: Fetch, generate: Generate, max_sessions: int = MAX_SESSIONS
    ) -> None:
        """Hold the link reader, the guarded reply generator and the session cap."""
        self._fetch = fetch
        self._generate = generate
        self._max_sessions = max_sessions
        self._sessions: OrderedDict[int, _Session] = OrderedDict()
        self._next_id = 1

    async def on_reply(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """``/reply``: validate, then fetch and answer in the background."""
        message = update.effective_message
        if message is None or not message.text:
            return
        request = parse_reply_message(message.text)
        kind = classify_link(request.url) if request else None
        if request is None or kind is None:
            await message.reply_text(USAGE_TEXT)
            return
        context.application.create_task(
            self._answer(context.bot, message.chat_id, request, kind)
        )

    async def on_button(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """A Redo or Bin tap on a reply."""
        query = update.callback_query
        if query is None:
            return
        await query.answer()
        parsed = _parse_callback(query.data)
        chat = update.effective_chat
        if parsed is None or chat is None:
            return
        prefix, session_id = parsed
        if prefix == BIN_PREFIX:
            await self._bin(context.bot, chat.id, query.message, session_id)
            return
        session = self._sessions.get(session_id)
        if session is None:
            await context.bot.send_message(chat_id=chat.id, text=GONE_TEXT)
        elif session.redo_count >= MAX_REDOS:
            await context.bot.send_message(chat_id=chat.id, text=NO_REDOS_TEXT)
        elif session.busy:
            await context.bot.send_message(chat_id=chat.id, text=BUSY_TEXT)
        else:
            session.busy = True
            context.application.create_task(
                self._redo(context.bot, chat.id, query.message, session_id, session)
            )

    async def _answer(
        self, bot: Bot, chat_id: int, request: ReplyRequest, kind: TargetType
    ) -> None:
        """Fetch (unless text was pasted), generate and send the first reply."""
        try:
            await self._typing(bot, chat_id)
            target = await self._target(bot, chat_id, request, kind)
            if target is None:
                return
            checked = await self._generate_or_report(
                bot, chat_id, target, request.steer
            )
            if checked is None:
                return
            session = _Session(target, request.steer)
            session_id = self._remember(session)
            await bot.send_message(
                chat_id=chat_id,
                text=checked.text,
                reply_markup=_keyboard(session_id, redo_allowed=True),
            )
        except Exception as error:  # nothing awaits this task
            logger.exception("Command /reply failed")
            await self._fail(bot, chat_id, error)

    async def _target(
        self, bot: Bot, chat_id: int, request: ReplyRequest, kind: TargetType
    ) -> ReplyTarget | None:
        """The text to answer: pasted, or fetched; None after telling the chat."""
        if request.pasted:
            return ReplyTarget(kind, request.pasted)
        result = await self._fetch(request.url)
        if result.ok and result.target_type is not None and result.text.strip():
            return ReplyTarget(result.target_type, result.text, result.parent_title)
        if result.ok:
            logger.warning("Fetched text is empty; asking for pasted text")
            await bot.send_message(chat_id=chat_id, text=COULDNT_READ_TEXT)
            return None
        if result.status is FetchStatus.COULDNT_READ:
            text = COULDNT_READ_TEXT
        else:
            text = NOT_SUBSTACK_TEXT
        await bot.send_message(chat_id=chat_id, text=text)
        return None

    async def _generate_or_report(
        self, bot: Bot, chat_id: int, target: ReplyTarget, steer: str | None
    ) -> CheckedReply | None:
        """Generate a guarded reply; on failure alert (once) and tell the chat."""
        try:
            return await self._generate(target, steer)
        except (GuardRejectedError, GuardError):
            # the guard has already alerted Stuart
            await bot.send_message(chat_id=chat_id, text=NOT_SAFE_TEXT)
        except Exception as error:
            logger.exception("Reply generation failed")
            await self._fail(bot, chat_id, error)
        return None

    async def _redo(
        self,
        bot: Bot,
        chat_id: int,
        old_message: MaybeInaccessibleMessage | None,
        session_id: int,
        session: _Session,
    ) -> None:
        """Regenerate from the same text and steer, without fetching again."""
        try:
            await self._typing(bot, chat_id)
            checked = await self._generate_or_report(
                bot, chat_id, session.target, session.steer
            )
            if checked is None:
                return
            if session_id not in self._sessions:
                await bot.send_message(chat_id=chat_id, text=GONE_TEXT)
                return
            session.redo_count += 1
            await self._remove_buttons(bot, chat_id, old_message)
            await bot.send_message(
                chat_id=chat_id,
                text=checked.text,
                reply_markup=_keyboard(session_id, session.redo_count < MAX_REDOS),
            )
        except Exception as error:  # nothing awaits this task
            logger.exception("Redo of a reply failed")
            await self._fail(bot, chat_id, error)
        finally:
            session.busy = False

    async def _bin(
        self,
        bot: Bot,
        chat_id: int,
        message: MaybeInaccessibleMessage | None,
        session_id: int,
    ) -> None:
        """Drop the session and remove the reply message."""
        self._sessions.pop(session_id, None)
        message_id = getattr(message, "message_id", None)
        if message_id is None:
            return
        try:
            await bot.delete_message(chat_id=chat_id, message_id=message_id)
        except TelegramError as error:
            logger.warning("Could not delete a binned reply: %s", type(error).__name__)
            await self._remove_buttons(bot, chat_id, message)

    def _remember(self, session: _Session) -> int:
        """Store a session, forgetting the oldest idle ones beyond the cap; return its id.

        A session whose redo is running, and the one just stored, are never forgotten.
        """
        session_id = self._next_id
        self._next_id += 1
        self._sessions[session_id] = session
        idle = [
            key
            for key, value in self._sessions.items()
            if not value.busy and key != session_id
        ]
        for key in idle[: max(len(self._sessions) - self._max_sessions, 0)]:
            del self._sessions[key]
        return session_id

    @staticmethod
    async def _remove_buttons(
        bot: Bot, chat_id: int, message: MaybeInaccessibleMessage | None
    ) -> None:
        """Take the buttons off an old reply message (best effort)."""
        message_id = getattr(message, "message_id", None)
        if message_id is None:
            return
        try:
            await bot.edit_message_reply_markup(
                chat_id=chat_id, message_id=message_id, reply_markup=None
            )
        except TelegramError as error:
            logger.warning("Could not remove old buttons: %s", type(error).__name__)

    @staticmethod
    async def _typing(bot: Bot, chat_id: int) -> None:
        """Show "typing" while working (best effort)."""
        try:
            await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        except TelegramError as error:
            logger.warning("Could not show typing: %s", type(error).__name__)

    @staticmethod
    async def _fail(bot: Bot, chat_id: int, error: Exception) -> None:
        """Alert a failure (the caller logged it) and tell the chat."""
        await alert(ALERT_EVENT, f"/reply failed: {describe_error(error)}")
        try:
            await bot.send_message(chat_id=chat_id, text=FAILED_TEXT)
        except Exception:  # the alert already went out
            logger.exception("Could not tell the chat that /reply failed")


def install_reply(application: Application, service: ReplyService) -> None:
    """Route /reply and the reply Redo and Bin buttons to the service."""
    application.add_handler(CommandHandler(COMMAND, service.on_reply))
    application.add_handler(
        CallbackQueryHandler(service.on_button, pattern=REPLY_CALLBACK_PATTERN)
    )
