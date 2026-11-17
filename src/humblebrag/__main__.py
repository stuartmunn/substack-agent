"""Container entrypoint: validates config, then runs the agent's event loop."""

import asyncio
import logging

from humblebrag.config import Config, load_config_or_exit
from humblebrag.heartbeat import heartbeat_loop
from humblebrag.logging_setup import configure_logging, register_secrets

logger = logging.getLogger(__name__)


def _secret_values(config: Config) -> list[str]:
    """Return the secret values held in ``config``."""
    return [
        config.telegram_bot_token,
        config.anthropic_api_key,
        config.substack_cookie,
    ]


def main() -> None:
    """Load config (exiting non-zero if invalid) and run until stopped."""
    configure_logging()
    config = load_config_or_exit()
    register_secrets(_secret_values(config))
    logger.info("Configuration loaded; model=%s", config.claude_model)
    asyncio.run(heartbeat_loop())


if __name__ == "__main__":
    main()
