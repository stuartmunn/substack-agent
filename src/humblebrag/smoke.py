"""Post-deploy smoke test: send "Sir Humblebrag reporting for duty" to Telegram.

Run inside the deployed container (``docker compose exec``) so it uses the same
config and Docker secrets as the agent. Exit code 0 means the message was sent
to the configured chat, which proves the token, chat id and network all work.
It sends one message and does nothing else: no run, no draft, no publishing.
"""

import asyncio
import logging
import sys

from telegram import Bot

from humblebrag.config import Config, load_config_or_exit
from humblebrag.logging_setup import configure_logging, register_secrets

logger = logging.getLogger(__name__)

SMOKE_MESSAGE = "Sir Humblebrag reporting for duty"
SEND_TIMEOUT_SECONDS = 20.0
EXIT_OK = 0
EXIT_FAILED = 1


async def send_smoke_message(config: Config) -> None:
    """Send ``SMOKE_MESSAGE`` to the configured chat, waiting at most
    ``SEND_TIMEOUT_SECONDS``."""
    async with asyncio.timeout(SEND_TIMEOUT_SECONDS):
        async with Bot(config.telegram_bot_token) as bot:
            await bot.send_message(chat_id=config.telegram_chat_id, text=SMOKE_MESSAGE)


def run_smoke_test(config: Config) -> int:
    """Run the smoke test and return the process exit code.

    A failure is logged (through the redacting logger) and returns non-zero for
    the deploy script to report. It cannot go to the alert channel because
    that is the same Telegram connection that just failed.
    """
    try:
        asyncio.run(send_smoke_message(config))
    except Exception:  # any failure becomes an exit code for the script
        logger.exception("Smoke test failed")
        return EXIT_FAILED
    logger.info("Smoke test passed: sent %r to the configured chat", SMOKE_MESSAGE)
    return EXIT_OK


def main() -> None:
    """Entrypoint: load config, then run the smoke test."""
    configure_logging()
    config = load_config_or_exit()
    register_secrets(
        [config.telegram_bot_token, config.anthropic_api_key, config.substack_cookie]
    )
    sys.exit(run_smoke_test(config))


if __name__ == "__main__":
    main()
