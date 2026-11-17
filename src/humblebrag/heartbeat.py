"""Heartbeat file touched by the running agent and checked by Docker."""

import asyncio
import logging
import sys
import time
from pathlib import Path

from humblebrag.logging_setup import configure_logging

logger = logging.getLogger(__name__)

HEARTBEAT_PATH = Path("/tmp/humblebrag.heartbeat")
HEARTBEAT_INTERVAL_SECONDS = 30
HEARTBEAT_MAX_AGE_SECONDS = 60
EXIT_UNHEALTHY = 1


def touch_heartbeat(path: Path) -> None:
    """Create the heartbeat file or update its modification time."""
    path.touch()


def heartbeat_is_fresh(path: Path, now: float, max_age: float) -> bool:
    """Return True if the heartbeat file is readable and at most ``max_age`` old."""
    try:
        age = now - path.stat().st_mtime
    except OSError:
        return False
    return age <= max_age


async def heartbeat_loop(
    path: Path = HEARTBEAT_PATH, interval: float = HEARTBEAT_INTERVAL_SECONDS
) -> None:
    """Touch the heartbeat forever; it stops if the event loop stops or hangs."""
    while True:
        try:
            touch_heartbeat(path)
        except OSError:
            logger.exception("Failed to write heartbeat file; will retry")
        await asyncio.sleep(interval)


def check_health(path: Path = HEARTBEAT_PATH) -> int:
    """Return a process exit code: 0 healthy, non-zero unhealthy."""
    if heartbeat_is_fresh(path, time.time(), HEARTBEAT_MAX_AGE_SECONDS):
        return 0
    logger.error("Heartbeat is missing or older than %ss", HEARTBEAT_MAX_AGE_SECONDS)
    return EXIT_UNHEALTHY


def main() -> None:
    """Docker HEALTHCHECK entrypoint."""
    configure_logging()
    sys.exit(check_health())


if __name__ == "__main__":
    main()
