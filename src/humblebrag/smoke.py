"""Post-deploy smoke test, run after every deploy and after any CLAUDE_MODEL change.

Run inside the deployed container (``docker compose exec``) so it uses the same
config and Docker secrets as the agent. It does two things:

1. Sends "Sir Humblebrag reporting for duty" to Telegram, which proves the token,
   chat id and network all work.
2. Makes one real, minimal Claude call per flow (post, reply, guard, seed) and
   sends one Telegram message with a pass or fail line per flow. A failure carries
   the API's error type and message. Mocked tests cannot see an API-side
   rejection of a request (SA-58), so only a real call can.

Exit code 0 means every step passed. It does nothing else: no run, no draft, no
publishing.
"""

import asyncio
import logging
import sys

from telegram import Bot

from humblebrag.alerts import MAX_ALERT_LENGTH, describe_error
from humblebrag.claude_client import ClaudeClient, Flow
from humblebrag.config import Config, load_config_or_exit
from humblebrag.logging_setup import configure_logging, redactor, register_secrets

logger = logging.getLogger(__name__)

SMOKE_MESSAGE = "Sir Humblebrag reporting for duty"
SEND_TIMEOUT_SECONDS = 20.0
CLAUDE_CHECK_TIMEOUT_SECONDS = 60.0
CLAUDE_SYSTEM = "You are a connectivity check. Answer as briefly as possible."
CLAUDE_PROMPT = "Reply with the single word: OK"
MAX_REPORT_LENGTH = MAX_ALERT_LENGTH
MAX_FAILURE_DETAIL_LENGTH = 500
REPORT_TITLE = "Claude smoke check"
PASS_MARK = "✅"
FAIL_MARK = "❌"
EXIT_OK = 0
EXIT_FAILED = 1


async def send_smoke_message(config: Config) -> None:
    """Send ``SMOKE_MESSAGE`` to the configured chat, waiting at most
    ``SEND_TIMEOUT_SECONDS``."""
    await _send(config, SMOKE_MESSAGE)


async def _send(config: Config, text: str) -> None:
    """Send ``text`` to the configured chat, waiting at most
    ``SEND_TIMEOUT_SECONDS``."""
    async with asyncio.timeout(SEND_TIMEOUT_SECONDS):
        async with Bot(config.telegram_bot_token) as bot:
            await bot.send_message(chat_id=config.telegram_chat_id, text=text)


def _failure_detail(error: Exception) -> str:
    """Describe a failed flow; a bare timeout has no message, so say what it was.

    The text is capped so one long error cannot push later flows out of the
    length-capped report.
    """
    if isinstance(error, TimeoutError) and not str(error):
        return f"TimeoutError: no answer within {CLAUDE_CHECK_TIMEOUT_SECONDS:.0f}s"
    return describe_error(error)[:MAX_FAILURE_DETAIL_LENGTH]


async def check_claude_flows(config: Config) -> dict[Flow, str | None]:
    """Make one minimal real Claude call per flow.

    Returns each flow's failure detail, or None if it passed. A failing flow
    does not stop the others, so one report shows every broken flow at once.
    """
    client = ClaudeClient(config)
    results: dict[Flow, str | None] = {}
    for flow in Flow:
        try:
            async with asyncio.timeout(CLAUDE_CHECK_TIMEOUT_SECONDS):
                await client.complete(CLAUDE_SYSTEM, CLAUDE_PROMPT, flow)
        except Exception as error:  # every failure is reported per flow
            logger.exception("Claude smoke check failed: flow=%s", flow)
            results[flow] = _failure_detail(error)
        else:
            results[flow] = None
    return results


def format_claude_report(results: dict[Flow, str | None]) -> str:
    """One line per flow: pass, or fail with the error type and message.

    Known secrets are redacted and the length is capped, as for alerts.
    """
    lines = [REPORT_TITLE + ":"]
    for flow, failure in results.items():
        if failure is None:
            lines.append(f"{PASS_MARK} {flow}: pass")
        else:
            lines.append(f"{FAIL_MARK} {flow}: FAIL - {failure}")
    return redactor.redact("\n".join(lines))[:MAX_REPORT_LENGTH]


async def run_checks(config: Config) -> int:
    """Send the duty message, run the Claude checks and report; return the exit code."""
    await send_smoke_message(config)
    results = await check_claude_flows(config)
    await _send(config, format_claude_report(results))
    if any(failure is not None for failure in results.values()):
        return EXIT_FAILED
    return EXIT_OK


def run_smoke_test(config: Config) -> int:
    """Run the smoke test and return the process exit code.

    A failure is logged (through the redacting logger) and returns non-zero for
    the deploy script to report. Telegram trouble cannot go to the alert channel
    because that is the same connection; Claude failures are reported to
    Telegram per flow.
    """
    try:
        code = asyncio.run(run_checks(config))
    except Exception:  # any failure becomes an exit code for the script
        logger.exception("Smoke test failed")
        return EXIT_FAILED
    if code == EXIT_OK:
        logger.info("Smoke test passed: Telegram and every Claude flow work")
    else:
        logger.error("Smoke test failed: at least one Claude flow failed")
    return code


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
