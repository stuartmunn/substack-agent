"""Builds and connects every service: the one place the agent is assembled.

``Runtime.setup`` runs inside ``run_bot`` once the Telegram application exists
(handlers must be installed before polling starts); ``Runtime.start`` runs
after polling and alerts are live. Constructing a ``Runtime`` only builds the
Substack publisher and session monitor, which read no files and use no network
until their first check; everything else (database, seed files, Claude client,
handlers) is created in ``setup``.
"""

import asyncio
import logging
from datetime import datetime
from pathlib import Path

from telegram.ext import Application

from humblebrag.alerts import telegram_sender
from humblebrag.approval import ApproveService, install_approve
from humblebrag.binning import BinService, install_bin
from humblebrag.bot import install_buttons
from humblebrag.claude_client import ClaudeClient
from humblebrag.commands import CommandService, RunCoordinator, install_commands
from humblebrag.config import Config
from humblebrag.drafts import Draft, DraftStore, RunGate
from humblebrag.generator import PostGenerator
from humblebrag.guard import CheckedPost, SafetyGuard, generate_checked
from humblebrag.persona import load_persona
from humblebrag.pipeline import RunPipeline
from humblebrag.preview import send_preview
from humblebrag.publisher import SubstackPublisher
from humblebrag.redo import RedoService, install_redo
from humblebrag.scheduler import Scheduler, SchedulerState
from humblebrag.seed_provider import SeedProvider
from humblebrag.seed_queue import SeedQueue
from humblebrag.session_monitor import MonitorState, SessionMonitor

logger = logging.getLogger(__name__)

DEFAULT_DATA_DIR = Path("/data")
SEED_FILENAME = "seed.md"
DB_FILENAME = "humblebrag.db"
SCHEDULER_STATE_FILENAME = "scheduler-state.json"
MONITOR_STATE_FILENAME = "session-monitor-state.json"


class Runtime:
    """The assembled agent."""

    def __init__(self, config: Config, data_dir: Path = DEFAULT_DATA_DIR) -> None:
        """Remember the config and where the data volume is; build nothing yet."""
        self._config = config
        self._data_dir = data_dir
        self.publisher = SubstackPublisher(config)
        self.monitor = SessionMonitor(
            config,
            MonitorState(data_dir / MONITOR_STATE_FILENAME),
            self.publisher.ensure_session,
        )
        self.pipeline: RunPipeline | None = None
        self.scheduler: Scheduler | None = None

    def setup(self, application: Application) -> None:
        """Create the services and install the button, redo and bin handlers."""
        config = self._config
        chat_id = config.telegram_chat_id
        store = DraftStore(self._data_dir / DB_FILENAME)
        install_buttons(application, store)
        queue = SeedQueue(
            self._data_dir / SEED_FILENAME, lambda: datetime.now(config.tz).date()
        )
        client = ClaudeClient(config)
        generator = PostGenerator(client, load_persona(), config)
        guard = SafetyGuard(client)

        async def generate(seed: str, steer: str | None) -> CheckedPost:
            return await generate_checked(generator, guard, seed, steer)

        async def preview(draft: Draft, guard_line: str | None) -> None:
            await send_preview(application.bot, chat_id, draft, guard_line)

        install_redo(application, RedoService(store, generate))
        install_bin(application, BinService(store, queue))
        install_approve(application, ApproveService(store, queue, self.publisher))
        self.pipeline = RunPipeline(
            gate=RunGate(
                store,
                config.max_pending_drafts,
                telegram_sender(application.bot, chat_id),
            ),
            store=store,
            queue=queue,
            seed_for=SeedProvider(queue, client).seed_for,
            generate=generate,
            send_preview=preview,
            max_pending=config.max_pending_drafts,
        )
        coordinator = RunCoordinator(self.pipeline.run)
        scheduler_state = SchedulerState(self._data_dir / SCHEDULER_STATE_FILENAME)
        install_commands(
            application, CommandService(queue, scheduler_state, config, coordinator)
        )
        self.scheduler = Scheduler(config, scheduler_state, coordinator.run_scheduled)

    async def start(self) -> None:
        """Check the Substack session, then run the daily monitor and scheduler."""
        assert self.scheduler is not None, "setup() must run before start()"
        scheduler = self.scheduler
        async with asyncio.TaskGroup() as group:
            group.create_task(self._session_checks())
            group.create_task(scheduler.run_forever())

    async def _session_checks(self) -> None:
        """Check the session at startup, then daily (counting the startup check)."""
        await self.publisher.check_session_or_alert()
        await self.monitor.mark_checked_if_due()
        await self.monitor.run_forever()
