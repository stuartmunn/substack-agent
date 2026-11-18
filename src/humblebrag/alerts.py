"""The single failure-alert channel: every component reports failures here."""

import asyncio
import logging
from collections.abc import Awaitable, Callable

from telegram import Bot

from humblebrag.config import Config
from humblebrag.logging_setup import redactor

logger = logging.getLogger(__name__)

Sender = Callable[[str], Awaitable[None]]

ALERT_PREFIX = "⚠️"
ALERT_TIMEOUT_SECONDS = 15.0
MAX_ALERT_LENGTH = 4096
TRUNCATION_MARKER = "..."

_sender: Sender | None = None


def configure_alerts(sender: Sender | None) -> None:
    """Set the function that delivers alert text to Stuart (None to unset)."""
    global _sender
    _sender = sender


def telegram_sender(bot: Bot, chat_id: int) -> Sender:
    """Return a sender that posts plain text to ``chat_id`` via ``bot``."""

    async def send(text: str) -> None:
        await bot.send_message(chat_id=chat_id, text=text)

    return send


def describe_error(error: BaseException) -> str:
    """Describe ``error``, unwrapping exception groups to their leaf errors."""
    if isinstance(error, BaseExceptionGroup):
        return "; ".join(describe_error(inner) for inner in error.exceptions)
    return f"{type(error).__name__}: {error}"


def format_alert(event: str, detail: str) -> str:
    """Build the alert text: prefix, failed step, redacted detail, length-capped."""
    text = redactor.redact(f"{ALERT_PREFIX} {event}: {detail}")
    if len(text) > MAX_ALERT_LENGTH:
        keep = MAX_ALERT_LENGTH - len(TRUNCATION_MARKER)
        text = text[:keep] + TRUNCATION_MARKER
    return text


async def alert(event: str, detail: str) -> bool:
    """Tell Stuart on Telegram that step ``event`` failed; never raises.

    Returns True if the alert was delivered. If Telegram is unavailable the
    alert is logged at ERROR instead, so a failure is never silent.
    """
    text = format_alert(event, detail)
    if _sender is None:
        logger.error("Alert not delivered (no Telegram sender): %s", text)
        return False
    try:
        async with asyncio.timeout(ALERT_TIMEOUT_SECONDS):
            await _sender(text)
    except Exception:  # alerting must never raise and mask the original failure
        logger.exception("Alert not delivered (Telegram failed): %s", text)
        return False
    logger.warning("Alert sent: %s", text)
    return True


async def send_standalone_alert(config: Config, event: str, detail: str) -> bool:
    """Alert using a fresh bot, for failures after the main bot has stopped."""
    try:
        async with Bot(config.telegram_bot_token) as bot:
            configure_alerts(telegram_sender(bot, config.telegram_chat_id))
            return await alert(event, detail)
    except Exception:  # alerting must never raise and mask the original failure
        logger.exception("Alert not delivered (could not start bot): %s", event)
        return False
    finally:
        configure_alerts(None)
