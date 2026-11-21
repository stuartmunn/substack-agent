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
STATUS_HANDED_OVER = "handed_over"
SUMMARY_TITLE_CHARS = 60
DRAFTS_TABLE = "drafts"
REBUILT_TABLE = "drafts_new"
NEW_COLUMN = "substack_draft_id"
TEASER_COLUMN = "note_teaser"
SCENE_COLUMN = "image_scene"
PROMPT_MESSAGE_COLUMN = "image_prompt_message_id"
_TABLE_BODY = f"""(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    seed TEXT NOT NULL,
    title TEXT NOT NULL,
    subtitle TEXT NOT NULL,
    body TEXT NOT NULL,
    steer_history TEXT NOT NULL,
    redo_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT '{STATUS_PENDING}'
        CHECK (status IN ('{STATUS_PENDING}', '{STATUS_HANDED_OVER}',
                          '{STATUS_PUBLISHED}', '{STATUS_BINNED}')),
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    substack_url TEXT,
    {NEW_COLUMN} TEXT,
    {TEASER_COLUMN} TEXT NOT NULL DEFAULT '',
    {SCENE_COLUMN} TEXT NOT NULL DEFAULT '',
    {PROMPT_MESSAGE_COLUMN} TEXT NOT NULL DEFAULT ''
)"""
SCHEMA = f"CREATE TABLE {DRAFTS_TABLE} {_TABLE_BODY}"
_OLD_COLUMN_LIST = (
    "id, seed, title, subtitle, body, steer_history, redo_count, status, "
    "created_at, resolved_at, substack_url"
)
STEP_COPY = "copy the existing rows"
_TABLE_INFO_SQL = f"PRAGMA table_info({DRAFTS_TABLE})"
# SQLite cannot change a CHECK constraint in place, so the table is rebuilt.
# Each step is (name, SQL); all of them run in one transaction.
_REBUILD_STEPS = (
    ("create the new table", f"CREATE TABLE {REBUILT_TABLE} {_TABLE_BODY}"),
    (
        STEP_COPY,
        (
            f"INSERT INTO {REBUILT_TABLE} ({_OLD_COLUMN_LIST})"
            f" SELECT {_OLD_COLUMN_LIST} FROM {DRAFTS_TABLE}"
        ),
    ),
    ("drop the old table", f"DROP TABLE {DRAFTS_TABLE}"),
    ("rename the new table", f"ALTER TABLE {REBUILT_TABLE} RENAME TO {DRAFTS_TABLE}"),
)
# Columns added after the first schema: (name, step name, SQL to add it).
_ADDED_COLUMNS = tuple(
    (
        column,
        f"add the {column} column",
        f"ALTER TABLE {DRAFTS_TABLE} ADD COLUMN {column} TEXT NOT NULL DEFAULT ''",
    )
    for column in (TEASER_COLUMN, SCENE_COLUMN, PROMPT_MESSAGE_COLUMN)
)
_COLUMNS = (
    "id, seed, title, subtitle, body, steer_history, redo_count, status, "
    "created_at, resolved_at, substack_url, substack_draft_id, note_teaser, "
    "image_scene, image_prompt_message_id"
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


class DraftMigrationError(DraftStoreError):
    """The drafts table could not be migrated; the original table is untouched."""

    def __init__(self, step: str, cause: str) -> None:
        """Name the step that failed and why."""
        super().__init__(f"Draft table migration failed at step '{step}': {cause}")
        self.step = step
        self.cause = cause


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
    substack_draft_id: str | None = None
    note_teaser: str = ""
    image_scene: str = ""
    image_prompt_message_id: str = ""


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
        substack_draft_id=row[11],
        note_teaser=row[12],
        image_scene=row[13],
        image_prompt_message_id=row[14],
    )


class DraftStore:
    """SQLite-backed drafts. Every operation opens and closes its own connection."""

    def __init__(
        self,
        path: Path = DEFAULT_DB_PATH,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """Create or migrate the drafts table; ``clock`` gives UTC now.

        Raises DraftMigrationError if an old table cannot be rebuilt.
        """
        self._path = path
        self._clock = clock or (lambda: datetime.now(UTC))
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise DraftStoreError(
                f"Draft database directory cannot be created: {type(error).__name__}"
            ) from None
        self._migrate()

    def create(
        self,
        seed: str,
        title: str,
        subtitle: str,
        body: str,
        steer: str | None = None,
        max_pending: int | None = None,
        note_teaser: str = "",
        image_scene: str = "",
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
            "INSERT INTO drafts"
            " (seed, title, subtitle, body, steer_history, created_at, note_teaser,"
            " image_scene)"
            " SELECT ?, ?, ?, ?, ?, ?, ?, ?"
            " WHERE (SELECT COUNT(*) FROM drafts WHERE status = ?) < ?",
            (
                seed,
                title,
                subtitle,
                body,
                history,
                created,
                note_teaser,
                image_scene,
                STATUS_PENDING,
                cap,
            ),
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

    def mark_handed_over(
        self, draft_id: int, substack_draft_id: str, url: str
    ) -> Draft:
        """Resolve a pending draft as handed over to Substack as a draft.

        Records the Substack draft ID and the editor link. Raises
        DraftNotFoundError or DraftStateError (not pending).
        """
        return self._resolve(
            draft_id, STATUS_HANDED_OVER, url, substack_draft_id=substack_draft_id
        )

    def set_substack_draft_id(self, draft_id: int, substack_draft_id: str) -> Draft:
        """Record the Substack draft ID on a pending draft.

        Raises DraftNotFoundError or DraftStateError (not pending).
        """
        cursor = self._execute(
            f"UPDATE drafts SET {NEW_COLUMN} = ? WHERE id = ? AND status = ?",
            (substack_draft_id, draft_id, STATUS_PENDING),
        )
        if cursor.rowcount == 0:
            current = self.get(draft_id)
            raise DraftStateError(f"Draft {draft_id} is already {current.status}")
        return self.get(draft_id)

    def set_image_prompt_message_id(self, draft_id: int, message_id: int) -> None:
        """Remember which Telegram message holds the draft's image prompt, so a
        reply to it can be matched to the draft. Raises DraftNotFoundError."""
        cursor = self._execute(
            f"UPDATE drafts SET {PROMPT_MESSAGE_COLUMN} = ? WHERE id = ?",
            (str(message_id), draft_id),
        )
        if cursor.rowcount == 0:
            raise DraftNotFoundError(f"Draft {draft_id} does not exist")

    def find_by_image_prompt_message(self, message_id: int) -> Draft | None:
        """The draft whose image prompt is Telegram message ``message_id``."""
        rows = self._query(
            f"SELECT {_COLUMNS} FROM drafts WHERE {PROMPT_MESSAGE_COLUMN} = ?",
            (str(message_id),),
        )
        return _to_draft(rows[0]) if rows else None

    def apply_redo(
        self,
        draft_id: int,
        title: str,
        subtitle: str,
        body: str,
        steer: str | None,
        note_teaser: str = "",
        image_scene: str = "",
    ) -> Draft:
        """Replace a pending draft's content (teaser and scene included), count the redo
        and log the steer.

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
                    " note_teaser = ?, image_scene = ?, redo_count = redo_count + 1,"
                    " steer_history = ? WHERE id = ?",
                    (
                        title,
                        subtitle,
                        body,
                        note_teaser,
                        image_scene,
                        json.dumps(history),
                        draft_id,
                    ),
                )
                connection.commit()
        except sqlite3.Error as error:
            raise DraftStoreError(
                f"Draft database error: {type(error).__name__}"
            ) from None
        return self.get(draft_id)

    def _resolve(
        self,
        draft_id: int,
        status: str,
        url: str | None,
        substack_draft_id: str | None = None,
    ) -> Draft:
        """Move a pending draft to ``status``, atomically checking it is pending.

        A ``substack_draft_id`` of None keeps any ID already recorded.
        """
        resolved = self._clock().isoformat()
        cursor = self._execute(
            "UPDATE drafts SET status = ?, resolved_at = ?, substack_url = ?,"
            f" {NEW_COLUMN} = COALESCE(?, {NEW_COLUMN}) WHERE id = ? AND status = ?",
            (status, resolved, url, substack_draft_id, draft_id, STATUS_PENDING),
        )
        if cursor.rowcount == 0:
            current = self.get(draft_id)
            raise DraftStateError(f"Draft {draft_id} is already {current.status}")
        return self.get(draft_id)

    def _migrate(self) -> None:
        """Create the table, or rebuild an old one, in a single transaction.

        Safe to rerun: a table that already has the new column is left alone,
        and that check is a plain read, so a normal start-up takes no write
        lock. On any failure the transaction rolls back, so the original table and
        its rows are untouched, and DraftMigrationError names the step.
        """
        try:
            with closing(
                sqlite3.connect(self._path, timeout=BUSY_TIMEOUT_SECONDS)
            ) as connection:
                connection.isolation_level = None  # explicit BEGIN/COMMIT
                if _is_current(connection):
                    return
                self._migrate_in_transaction(connection)
        except sqlite3.Error as error:
            raise DraftMigrationError(
                "open the database", f"{type(error).__name__}: {error}"
            ) from None

    def _migrate_in_transaction(self, connection: sqlite3.Connection) -> None:
        """BEGIN, migrate, COMMIT; roll back (and log, never mask) on failure."""
        _run_step(connection, "start the transaction", "BEGIN IMMEDIATE")
        try:
            self._create_or_rebuild(connection)
            _run_step(connection, "commit", "COMMIT")
        except BaseException:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                logger.exception("Draft migration rollback failed")
            raise

    @staticmethod
    def _create_or_rebuild(connection: sqlite3.Connection) -> None:
        """Create a missing table; rebuild one that lacks the Substack draft ID
        column; add each later column (teaser, scene) a table still lacks.

        Checked again inside the transaction: another process may have
        migrated between the unlocked check and BEGIN.
        """
        columns = _table_columns(connection)
        if not columns:
            _run_step(connection, "create the drafts table", SCHEMA)
        elif NEW_COLUMN not in columns:
            _rebuild(connection)
        else:
            for column, step, sql in _ADDED_COLUMNS:
                if column not in columns:
                    _run_step(connection, step, sql)

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


def _run_step(
    connection: sqlite3.Connection, step: str, sql: str, params: tuple = ()
) -> sqlite3.Cursor:
    """Run one migration statement; any failure names ``step``."""
    try:
        return connection.execute(sql, params)
    except sqlite3.Error as error:
        raise DraftMigrationError(step, f"{type(error).__name__}: {error}") from None


def _table_columns(connection: sqlite3.Connection) -> list[str]:
    """Column names of the drafts table (empty if it does not exist)."""
    cursor = _run_step(connection, "inspect the drafts table", _TABLE_INFO_SQL)
    return [row[1] for row in cursor]


def _is_current(connection: sqlite3.Connection) -> bool:
    """True if the drafts table already has the new schema."""
    columns = _table_columns(connection)
    return NEW_COLUMN in columns and all(
        column in columns for column, _, _ in _ADDED_COLUMNS
    )


def _count_rows(connection: sqlite3.Connection, step: str, table: str) -> int:
    """How many rows ``table`` holds; a failure names ``step``."""
    cursor = _run_step(connection, step, f"SELECT COUNT(*) FROM {table}")
    return cursor.fetchone()[0]


def _rebuild(connection: sqlite3.Connection) -> None:
    """Rebuild the old table with the new schema, keeping every row and id."""
    sequence = _run_step(
        connection,
        "read the id counter",
        "SELECT seq FROM sqlite_sequence WHERE name = ?",
        (DRAFTS_TABLE,),
    ).fetchone()
    verify = "verify the copied rows"
    expected = _count_rows(connection, verify, DRAFTS_TABLE)
    for step, sql in _REBUILD_STEPS:
        _run_step(connection, step, sql)
        if step == STEP_COPY:
            copied = _count_rows(connection, verify, REBUILT_TABLE)
            if copied != expected:
                raise DraftMigrationError(verify, f"expected {expected} rows")
    if sequence is not None:  # keep ids from ever being reused, even if now empty
        step = "restore the id counter"
        params = (DRAFTS_TABLE,)
        _run_step(
            connection, step, "DELETE FROM sqlite_sequence WHERE name = ?", params
        )
        _run_step(
            connection,
            step,
            "INSERT INTO sqlite_sequence (name, seq) VALUES (?, ?)",
            (DRAFTS_TABLE, sequence[0]),
        )


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
