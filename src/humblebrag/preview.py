"""Telegram draft preview: text split to the message limit, plus the
Approve / Redo / Bin buttons keyed to the draft id."""

import logging

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

from humblebrag.drafts import Draft

logger = logging.getLogger(__name__)

TELEGRAM_MESSAGE_LIMIT = 4096
ACTION_APPROVE = "approve"
ACTION_REDO = "redo"
ACTION_BIN = "bin"
ACTION_CODES = {ACTION_APPROVE: "a", ACTION_REDO: "r", ACTION_BIN: "b"}
BUTTON_LABELS = {
    ACTION_APPROVE: "✅ Approve",
    ACTION_REDO: "🔁 Redo",
    ACTION_BIN: "🗑️ Bin",
}
CALLBACK_SEPARATOR = ":"
NO_GUARD_RESULT = "Guard: no result recorded"
_ACTIONS_BY_CODE = {code: action for action, code in ACTION_CODES.items()}
_SPLIT_SEPARATORS = ("\n", " ")


def callback_data(action: str, draft_id: int) -> str:
    """The callback data for a button: one letter, a colon, the draft id."""
    return f"{ACTION_CODES[action]}{CALLBACK_SEPARATOR}{draft_id}"


def parse_callback_data(data: str | None) -> tuple[str, int] | None:
    """Return ``(action, draft_id)`` from button data, or None if malformed."""
    if data is None:
        return None
    code, separator, raw_id = data.partition(CALLBACK_SEPARATOR)
    if separator != CALLBACK_SEPARATOR or code not in _ACTIONS_BY_CODE:
        return None
    if not (raw_id.isascii() and raw_id.isdigit()):
        return None
    return _ACTIONS_BY_CODE[code], int(raw_id)


def preview_keyboard(draft_id: int) -> InlineKeyboardMarkup:
    """The Approve / Redo / Bin buttons for one draft."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    BUTTON_LABELS[action], callback_data=callback_data(action, draft_id)
                )
                for action in (ACTION_APPROVE, ACTION_REDO, ACTION_BIN)
            ]
        ]
    )


def telegram_length(text: str) -> int:
    """Length as Telegram counts it: UTF-16 code units."""
    return len(text.encode("utf-16-le")) // 2


def split_for_telegram(text: str, limit: int = TELEGRAM_MESSAGE_LIMIT) -> list[str]:
    """Split ``text`` into chunks of at most ``limit`` units, breaking at line
    ends, then spaces, then (only if unavoidable) anywhere."""
    chunks: list[str] = []
    remaining = text
    while telegram_length(remaining) > limit:
        cut = _cut_point(remaining, limit)
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip("\n ")
    chunks.append(remaining)
    return [chunk for chunk in chunks if chunk]


def _cut_point(text: str, limit: int) -> int:
    """The index to cut ``text`` at so the head fits in ``limit`` units."""
    fit, units = 0, 0
    for char in text:
        units += telegram_length(char)
        if units > limit:
            break
        fit += 1
    for separator in _SPLIT_SEPARATORS:
        position = text.rfind(separator, 1, fit + 1)
        if position > 0:
            return position
    return max(fit, 1)


def format_preview(draft: Draft, guard_line: str | None) -> list[str]:
    """The preview as message texts: title, subtitle, body, then seed, redo
    count and guard result together at the end."""
    main = f"{draft.title}\n{draft.subtitle}\n\n{draft.body}"
    footer = (
        f"Seed: {draft.seed}\nRedo count: {draft.redo_count}\n"
        f"{guard_line or NO_GUARD_RESULT}"
    )
    chunks = split_for_telegram(main)
    if telegram_length(chunks[-1]) + telegram_length(f"\n\n{footer}") <= (
        TELEGRAM_MESSAGE_LIMIT
    ):
        chunks[-1] = f"{chunks[-1]}\n\n{footer}"
    else:
        chunks.extend(split_for_telegram(footer))
    return chunks


async def send_preview(
    bot: Bot, chat_id: int, draft: Draft, guard_line: str | None = None
) -> None:
    """Send the preview; the buttons go on the final message only."""
    chunks = format_preview(draft, guard_line)
    for index, chunk in enumerate(chunks):
        is_last = index == len(chunks) - 1
        await bot.send_message(
            chat_id=chat_id,
            text=chunk,
            reply_markup=preview_keyboard(draft.id) if is_last else None,
        )
