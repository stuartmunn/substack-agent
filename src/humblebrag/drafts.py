"""Draft store: drafts persisted in SQLite on the data volume, plus the
pending-draft cap that pauses scheduled and manual runs.

The store is synchronous (short queries with a busy timeout); async callers
run it with ``asyncio.to_thread``.
"""

import asyncio
import json
import logging
import sqlite3
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from humblebrag.alerts import Sender

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = Path("/data/humblebrag.db")
BUSY_TIMEOUT_SECONDS = 5.0
UNLIMITED_PENDING = 2**62
MAX_REDOS = 3
STATUS_PENDING = "pending"
STATUS_PUBLISHED = "published"
STATUS_BINNED = "binned"
SUMMARY_TITLE_CHARS = 60
SCHEMA = f"""
CREATE TABLE IF NOT EXISTS drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    seed TEXT NOT NULL,
    title TEXT NOT NULL,
    subtitle TEXT NOT NULL,
    body TEXT NOT NULL,
    steer_history TEXT NOT NULL,
    redo_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT '{STATUS_PENDING}'
        CHECK (status IN ('{STATUS_PENDING}', '{STATUS_PUBLISHED}', '{STATUS_BINNED}')),
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    substack_url TEXT
)
"""
_COLUMNS = (
    "id, seed, title, subtitle, body, steer_history, redo_count, status, "
    "created_at, resolved_at, substack_url"
)


class DraftError(Exception):
    """Base class for draft store failures; messages never hold secrets."""


class DraftNotFoundError(DraftError):
    """No draft has the given id."""


class DraftStateError(DraftError):
    """The draft is not pending, so it cannot be resolved again."""


class PendingCapReachedError(DraftError):
    """Creating the draft would exceed the pending cap; nothing was stored."""


class RedoLimitReachedError(DraftError):
    """The draft has used all its redos."""


class DraftStoreError(DraftError):
    """The database could not be opened, read or written."""


@dataclass(frozen=True)
class Draft:
    """One stored draft."""

    id: int
    seed: str
    title: str
    subtitle: str
    body: str
    steer_history: tuple[str, ...]
    redo_count: int
    status: str
    created_at: datetime
    resolved_at: datetime | None
    substack_url: str | None


def _to_draft(row: tuple) -> Draft:
    """Build a Draft from a database row in ``_COLUMNS`` order."""
    resolved = row[9]
    return Draft(
        id=row[0],
        seed=row[1],
        title=row[2],
        subtitle=row[3],
        body=row[4],
        steer_history=tuple(json.loads(row[5])),
        redo_count=row[6],
        status=row[7],
        created_at=datetime.fromisoformat(row[8]),
        resolved_at=None if resolved is None else datetime.fromisoformat(resolved),
        substack_url=row[10],
    )


class DraftStore:
    """SQLite-backed drafts. Every operation opens and closes its own connection."""

    def __init__(
        self,
        path: Path = DEFAULT_DB_PATH,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """Create the database file and table if needed; ``clock`` gives UTC now."""
        self._path = path
        self._clock = clock or (lambda: datetime.now(UTC))
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise DraftStoreError(
                f"Draft database directory cannot be created: {type(error).__name__}"
            ) from None
        self._execute(SCHEMA)

    def create(
        self,
        seed: str,
        title: str,
        subtitle: str,
        body: str,
        steer: str | None = None,
        max_pending: int | None = None,
    ) -> Draft:
        """Store a new pending draft (steer, if any, starts the steer history).

        With ``max_pending`` the cap is enforced inside the INSERT itself, so
        two runs racing past ``RunGate`` cannot both add the last draft.
        Raises PendingCapReachedError if the cap is already reached.
        """
        history = json.dumps([steer] if steer else [])
        created = self._clock().isoformat()
        cap = max_pending if max_pending is not None else UNLIMITED_PENDING
        cursor = self._execute(
            "INSERT INTO drafts (seed, title, subtitle, body, steer_history, created_at)"
            " SELECT ?, ?, ?, ?, ?, ?"
            " WHERE (SELECT COUNT(*) FROM drafts WHERE status = ?) < ?",
            (seed, title, subtitle, body, history, created, STATUS_PENDING, cap),
        )
        if cursor.rowcount == 0:
            raise PendingCapReachedError(f"{cap} drafts are already pending")
        return self.get(cursor.lastrowid)

    def get(self, draft_id: int) -> Draft:
        """Return the draft, or raise DraftNotFoundError."""
        rows = self._query(f"SELECT {_COLUMNS} FROM drafts WHERE id = ?", (draft_id,))
        if not rows:
            raise DraftNotFoundError(f"No draft with id {draft_id}")
        return _to_draft(rows[0])

    def list_pending(self) -> list[Draft]:
        """Every pending draft, oldest first."""
        rows = self._query(
            f"SELECT {_COLUMNS} FROM drafts WHERE status = ? ORDER BY created_at, id",
            (STATUS_PENDING,),
        )
        return [_to_draft(row) for row in rows]

    def pending_count(self) -> int:
        """How many drafts are pending."""
        rows = self._query(
            "SELECT COUNT(*) FROM drafts WHERE status = ?", (STATUS_PENDING,)
        )
        return rows[0][0]

    def mark_published(self, draft_id: int, substack_url: str) -> Draft:
        """Resolve a pending draft as published at ``substack_url``."""
        return self._resolve(draft_id, STATUS_PUBLISHED, substack_url)

    def mark_binned(self, draft_id: int) -> Draft:
        """Resolve a pending draft as binned."""
        return self._resolve(draft_id, STATUS_BINNED, None)

    def apply_redo(
        self,
        draft_id: int,
        title: str,
        subtitle: str,
        body: str,
        steer: str | None,
    ) -> Draft:
        """Replace a pending draft's content, count the redo and log the steer.

        One transaction checks the draft is pending and has redos left, so two
        racing redos cannot exceed ``MAX_REDOS``. Raises DraftNotFoundError,
        DraftStateError or RedoLimitReachedError.
        """
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT status, redo_count, steer_history FROM drafts WHERE id = ?",
                    (draft_id,),
                ).fetchone()
                if row is None:
                    raise DraftNotFoundError(f"No draft with id {draft_id}")
                status, redo_count, raw_history = row
                if status != STATUS_PENDING:
                    raise DraftStateError(f"Draft {draft_id} is already {status}")
                if redo_count >= MAX_REDOS:
                    raise RedoLimitReachedError(f"Draft {draft_id} has no redos left")
                history = json.loads(raw_history) + ([steer] if steer else [])
                connection.execute(
                    "UPDATE drafts SET title = ?, subtitle = ?, body = ?,"
                    " redo_count = redo_count + 1, steer_history = ? WHERE id = ?",
                    (title, subtitle, body, json.dumps(history), draft_id),
                )
                connection.commit()
        except sqlite3.Error as error:
            raise DraftStoreError(
                f"Draft database error: {type(error).__name__}"
            ) from None
        return self.get(draft_id)

    def _resolve(self, draft_id: int, status: str, url: str | None) -> Draft:
        """Move a pending draft to ``status``, atomically checking it is pending."""
        resolved = self._clock().isoformat()
        cursor = self._execute(
            "UPDATE drafts SET status = ?, resolved_at = ?, substack_url = ?"
            " WHERE id = ? AND status = ?",
            (status, resolved, url, draft_id, STATUS_PENDING),
        )
        if cursor.rowcount == 0:
            current = self.get(draft_id)
            raise DraftStateError(f"Draft {draft_id} is already {current.status}")
        return self.get(draft_id)

    def _query(self, sql: str, params: tuple = ()) -> list[tuple]:
        """Run a read-only statement and return its rows."""
        try:
            with closing(self._connect()) as connection:
                return connection.execute(sql, params).fetchall()
        except sqlite3.Error as error:
            raise DraftStoreError(
                f"Draft database error: {type(error).__name__}"
            ) from None

    def _execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        """Run a write statement in a transaction and commit it."""
        try:
            with closing(self._connect()) as connection, connection:
                return connection.execute(sql, params)
        except sqlite3.Error as error:
            raise DraftStoreError(
                f"Draft database error: {type(error).__name__}"
            ) from None

    def _connect(self) -> sqlite3.Connection:
        """Open a connection with a busy timeout."""
        return sqlite3.connect(self._path, timeout=BUSY_TIMEOUT_SECONDS)


def format_pending_summary(drafts: list[Draft]) -> str:
    """The restart message: a numbered list of the pending drafts."""
    if not drafts:
        return "No drafts waiting."
    noun = "draft" if len(drafts) == 1 else "drafts"
    lines = [f"{len(drafts)} {noun} waiting:"]
    for position, draft in enumerate(drafts, start=1):
        title = draft.title
        if len(title) > SUMMARY_TITLE_CHARS:
            title = title[: SUMMARY_TITLE_CHARS - 1] + "…"
        lines.append(f"{position}. {title} (#{draft.id}, {draft.created_at:%d %b})")
    return "\n".join(lines)


def paused_message(pending: int) -> str:
    """The message sent when the pending cap pauses a run."""
    noun = "draft" if pending == 1 else "drafts"
    return f"{pending} {noun} waiting, run paused. Clear some to resume."


class RunGate:
    """Decides whether a run (scheduled or ``/now``) may generate a new draft."""

    def __init__(self, store: DraftStore, max_pending: int, notify: Sender) -> None:
        """Hold the store, the cap and the sender used for the paused message."""
        self._store = store
        self._max_pending = max_pending
        self._notify = notify

    async def allow_run(self) -> bool:
        """True if pending drafts are below the cap; else tell Stuart once, False."""
        pending = await asyncio.to_thread(self._store.pending_count)
        if pending < self._max_pending:
            return True
        logger.info(
            "Run paused: %d pending drafts (cap %d)", pending, self._max_pending
        )
        await self._notify(paused_message(pending))
        return False
