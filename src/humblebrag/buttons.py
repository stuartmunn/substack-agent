"""Routes Approve / Redo / Bin button taps to the right draft action."""

import asyncio
import logging
from collections.abc import Awaitable, Callable

from telegram import Update
from telegram.ext import Application, ContextTypes

from humblebrag.alerts import alert, describe_error
from humblebrag.drafts import STATUS_PENDING, Draft, DraftNotFoundError, DraftStoreError
from humblebrag.preview import parse_callback_data

logger = logging.getLogger(__name__)

STORE_KEY = "draft_store"
ACTIONS_KEY = "button_actions"
ALERT_EVENT = "buttons"
ALREADY_HANDLED_TEXT = "Already handled."
UNKNOWN_DRAFT_TEXT = "I can't find that draft."
UNKNOWN_BUTTON_TEXT = "I don't recognise that button."
NOT_AVAILABLE_TEXT = "That action isn't available yet."
STORE_ERROR_TEXT = "Something went wrong reading that draft."

ButtonAction = Callable[[Update, ContextTypes.DEFAULT_TYPE, Draft], Awaitable[None]]


def register_action(
    application: Application, action: str, handler: ButtonAction
) -> None:
    """Set the handler run when ``action`` is tapped on a pending draft."""
    application.bot_data.setdefault(ACTIONS_KEY, {})[action] = handler


async def _reply(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    """Send ``text`` to the chat the button was tapped in."""
    chat = update.effective_chat
    if chat is not None:
        await context.bot.send_message(chat_id=chat.id, text=text)


async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Answer a button tap: reject unknown or resolved drafts, else dispatch."""
    query = update.callback_query
    if query is None:
        return
    await query.answer()
    parsed = parse_callback_data(query.data)
    if parsed is None:
        logger.warning("Ignored button with malformed data")
        await _reply(update, context, UNKNOWN_BUTTON_TEXT)
        return
    action, draft_id = parsed
    store = context.application.bot_data[STORE_KEY]
    try:
        draft = await asyncio.to_thread(store.get, draft_id)
    except DraftNotFoundError:
        await _reply(update, context, UNKNOWN_DRAFT_TEXT)
        return
    except DraftStoreError as error:
        await alert(
            ALERT_EVENT, f"Could not load draft {draft_id}: {describe_error(error)}"
        )
        await _reply(update, context, STORE_ERROR_TEXT)
        return
    if draft.status != STATUS_PENDING:
        await _reply(update, context, ALREADY_HANDLED_TEXT)
        return
    handler = context.application.bot_data.get(ACTIONS_KEY, {}).get(action)
    if handler is None:
        logger.warning("No handler registered for button action %s", action)
        await _reply(update, context, NOT_AVAILABLE_TEXT)
        return
    await handler(update, context, draft)
