"""Container entrypoint: validates config, then runs the agent's event loop."""

import asyncio
import logging
import signal
import sys

from humblebrag.alerts import describe_error, send_standalone_alert
from humblebrag.bot import run_bot
from humblebrag.config import Config, load_config_or_exit
from humblebrag.heartbeat import heartbeat_loop
from humblebrag.logging_setup import configure_logging, register_secrets
from humblebrag.publisher import SubstackPublisher
from humblebrag.session_monitor import MonitorState, SessionMonitor

logger = logging.getLogger(__name__)

EXIT_CRASHED = 1


def _secret_values(config: Config) -> list[str]:
    """Return the secret values held in ``config``."""
    return [
        config.telegram_bot_token,
        config.anthropic_api_key,
        config.substack_cookie,
    ]


def _request_stop(tasks: list[asyncio.Task[None]]) -> None:
    """Cancel every task after a shutdown signal."""
    logger.info("Shutdown signal received; stopping")
    for task in tasks:
        task.cancel()


async def run_session_checks(
    publisher: SubstackPublisher, monitor: SessionMonitor
) -> None:
    """Check the session at startup, then daily (counting the startup check)."""
    await publisher.check_session_or_alert()
    await monitor.mark_checked_if_due()
    await monitor.run_forever()


async def run_until_signalled(config: Config) -> None:
    """Run the heartbeat and bot until SIGTERM or SIGINT, then stop cleanly.

    Both run on this one event loop, so if the loop hangs the heartbeat stops.
    If either task fails, the other is cancelled and the error propagates.
    """
    loop = asyncio.get_running_loop()
    publisher = SubstackPublisher(config)
    monitor = SessionMonitor(config, MonitorState(), publisher.ensure_session)
    async with asyncio.TaskGroup() as group:
        tasks = [
            group.create_task(heartbeat_loop()),
            group.create_task(
                run_bot(
                    config,
                    on_started=lambda: run_session_checks(publisher, monitor),
                )
            ),
        ]
        for stop_signal in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(stop_signal, _request_stop, tasks)


def main() -> None:
    """Load config (exiting non-zero if invalid) and run until stopped."""
    configure_logging()
    config = load_config_or_exit()
    register_secrets(_secret_values(config))
    logger.info("Configuration loaded; model=%s", config.claude_model)
    try:
        asyncio.run(run_until_signalled(config))
    except Exception as error:
        logger.exception("Agent stopped with an unhandled error")
        detail = f"agent crashed with {describe_error(error)}"
        try:
            asyncio.run(send_standalone_alert(config, "agent", detail))
        except Exception:
            logger.exception("Failed to send crash alert")
        sys.exit(EXIT_CRASHED)


if __name__ == "__main__":
    main()
