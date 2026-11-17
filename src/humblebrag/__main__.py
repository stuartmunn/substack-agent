"""Container entrypoint: validates config, then hands over to the agent."""

import logging

from humblebrag.config import load_config_or_exit

logger = logging.getLogger(__name__)


def main() -> None:
    """Load config (exiting non-zero if it is invalid) and log readiness."""
    logging.basicConfig(level=logging.INFO)
    config = load_config_or_exit()
    logger.info("Configuration loaded; model=%s", config.claude_model)


if __name__ == "__main__":
    main()
