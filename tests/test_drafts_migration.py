"""Tests for the handed_over status and Substack draft ID (SA-40)."""

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from humblebrag import __main__ as entrypoint
from humblebrag import drafts as drafts_module
from humblebrag.backup import DB_FILENAME, BackupService
from humblebrag.bot import build_application
from humblebrag.config import Config, load_config
from humblebrag.drafts import (
    STATUS_BINNED,
    STATUS_HANDED_OVER,
    STATUS_PENDING,
    STATUS_PUBLISHED,
    DraftMigrationError,
    DraftNotFoundError,
    DraftStateError,
    DraftStore,
    RunGate,
)
from humblebrag.wiring import Runtime

ENV = {
    "TELEGRAM_BOT_TOKEN": "123456:TESTTOKEN",
    "TELEGRAM_CHAT_ID": "12345",
    "ANTHROPIC_API_KEY": "sk-ant-key",
    "CLAUDE_MODEL": "claude-test-model",
    "SUBSTACK_PUBLICATION_URL": "https://humblebrag.substack.com",
    "SUBSTACK_COOKIE": "s%3ASECRET-SID",
}
EDITOR_URL = "https://humblebrag.substack.com/publish/post/123"
OLD_SCHEMA = """
CREATE TABLE drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    seed TEXT NOT NULL,
    title TEXT NOT NULL,
    subtitle TEXT NOT NULL,
    body TEXT NOT NULL,
    steer_history TEXT NOT NULL,
    redo_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'published', 'binned')),
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    substack_url TEXT
)
"""
OLD_ROWS = [
    (1, "seed 1", "T1", "S1", "B1", '["more shouting"]', 2, "pending",
     "2026-11-10T08:30:00+00:00", None, None),
    (2, "seed 2", "T2", "S2", "B2", "[]", 0, "published",
     "2026-11-11T08:30:00+00:00", "2026-11-11T09:00:00+00:00", "https://x/p/two"),
    (3, "seed 3", "T3", "S3", "B3", "[]", 1, "binned",
     "2026-11-12T08:30:00+00:00", "2026-11-12T09:00:00+00:00", None),
    (4, "seed 4", "T4", "S4", "B4", "[]", 0, "pending",
     "2026-11-13T08:30:00+00:00", None, None),
]  # fmt: skip
ROW_SQL = "SELECT {cols} FROM drafts ORDER BY id"
OLD_COLUMNS = (
    "id, seed, title, subtitle, body, steer_history, redo_count, status, "
    "created_at, resolved_at, substack_url"
)


def _old_database(path: Path) -> None:
    """Build a database exactly as the pre-SA-40 code did, with rows in every
    status, and a deleted last row so the id counter is ahead of the max id."""
    connection = sqlite3.connect(path)
    connection.execute(OLD_SCHEMA)
    connection.executemany(
        "INSERT INTO drafts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", OLD_ROWS
    )
    connection.execute(
        "INSERT INTO drafts (seed, title, subtitle, body, steer_history, created_at)"
        " VALUES ('gone', 'g', 'g', 'g', '[]', '2026-11-14T08:30:00+00:00')"
    )
    connection.execute("DELETE FROM drafts WHERE id = 5")
    connection.commit()
    connection.close()


def _read(path: Path, sql: str) -> list[tuple]:
    """Run a query on a raw connection."""
    connection = sqlite3.connect(path)
    try:
        return connection.execute(sql).fetchall()
    finally:
        connection.close()


def _table_sql(path: Path) -> list[tuple]:
    """Every schema entry, for before/after comparison."""
    return _read(path, "SELECT type, name, sql FROM sqlite_master ORDER BY name")


@pytest.fixture
def old_db(tmp_path: Path) -> Path:
    """A database built with the old schema."""
    path = tmp_path / "humblebrag.db"
    _old_database(path)
    return path


def _make(store: DraftStore, n: int = 1) -> int:
    """Create a pending draft and return its id."""
    return store.create(f"seed {n}", f"T{n}", f"S{n}", f"B{n}").id


# AC1 + AC2: the new status and the nullable column


def test_a_fresh_database_allows_handed_over_and_has_the_column(
    tmp_path: Path,
) -> None:
    """AC1/AC2: the schema accepts all four statuses and a nullable ID column."""
    path = tmp_path / "humblebrag.db"
    store = DraftStore(path)
    draft_id = _make(store)
    assert store.get(draft_id).substack_draft_id is None
    connection = sqlite3.connect(path)
    for status in (STATUS_HANDED_OVER, STATUS_PUBLISHED, STATUS_BINNED):
        connection.execute("UPDATE drafts SET status = ?", (status,))
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute("UPDATE drafts SET status = 'lost'")
    connection.close()


# AC3: existing rows survive the migration unchanged


def test_every_row_survives_with_its_values_and_a_null_draft_id(old_db: Path) -> None:
    """AC3: rows in every old status are intact; the new column is null."""
    before = _read(old_db, ROW_SQL.format(cols=OLD_COLUMNS))
    store = DraftStore(old_db)
    assert _read(old_db, ROW_SQL.format(cols=OLD_COLUMNS)) == before
    assert _read(old_db, "SELECT substack_draft_id FROM drafts") == [(None,)] * 4
    assert [store.get(row[0]).status for row in OLD_ROWS] == [
        STATUS_PENDING,
        STATUS_PUBLISHED,
        STATUS_BINNED,
        STATUS_PENDING,
    ]
    assert store.get(1).steer_history == ("more shouting",)
    assert store.get(1).redo_count == 2


def test_the_migrated_table_accepts_handed_over(old_db: Path) -> None:
    """AC1: after migrating an old database the CHECK allows handed_over."""
    store = DraftStore(old_db)
    assert store.mark_handed_over(1, "sub-1", EDITOR_URL).status == STATUS_HANDED_OVER


def test_ids_are_never_reused_after_the_rebuild(old_db: Path) -> None:
    """AC3: the id counter survives, so a deleted id is not handed out again."""
    store = DraftStore(old_db)
    assert _make(store) == 6


def test_the_id_counter_survives_when_every_row_was_deleted(tmp_path: Path) -> None:
    """AC3: an old table emptied before migrating still never reuses an id."""
    path = tmp_path / "humblebrag.db"
    _old_database(path)
    connection = sqlite3.connect(path)
    connection.execute("DELETE FROM drafts")
    connection.commit()
    connection.close()
    assert _make(DraftStore(path)) == 6


# AC4: automatic, once, idempotent


def test_a_migrated_database_is_not_rebuilt_again(
    old_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC4: the second start-up finds the column and rebuilds nothing."""
    DraftStore(old_db)
    schema = _table_sql(old_db)

    def boom(_connection: sqlite3.Connection) -> None:
        raise AssertionError("rebuilt an already-migrated table")

    monkeypatch.setattr(drafts_module, "_rebuild", boom)
    store = DraftStore(old_db)
    assert _table_sql(old_db) == schema
    assert store.get(2).substack_url == "https://x/p/two"


def test_rerunning_keeps_data_written_after_the_first_migration(old_db: Path) -> None:
    """AC4: a rerun never loses a draft handed over since the first run."""
    DraftStore(old_db).mark_handed_over(1, "sub-1", EDITOR_URL)
    again = DraftStore(old_db)
    assert again.get(1).substack_draft_id == "sub-1"
    assert again.get(1).status == STATUS_HANDED_OVER


def test_a_current_database_starts_without_taking_a_write_lock(old_db: Path) -> None:
    """AC4: with the migration done, a second store opens while another
    connection holds the write lock."""
    DraftStore(old_db)
    holder = sqlite3.connect(old_db)
    holder.execute("BEGIN IMMEDIATE")
    try:
        DraftStore(old_db)
    finally:
        holder.execute("ROLLBACK")
        holder.close()


def test_a_failure_while_inspecting_names_that_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC5: a failing table inspection is reported as such, not as 'open'."""
    monkeypatch.setattr(drafts_module, "_TABLE_INFO_SQL", "PRAGMA nonsense(")
    with pytest.raises(DraftMigrationError) as caught:
        DraftStore(tmp_path / "humblebrag.db")
    assert caught.value.step == "inspect the drafts table"


def test_a_missing_database_is_created_with_the_new_schema(tmp_path: Path) -> None:
    """AC4: a first-ever start creates the new table directly, no rebuild."""
    path = tmp_path / "fresh.db"
    DraftStore(path)
    columns = [row[1] for row in _read(path, "PRAGMA table_info(drafts)")]
    assert "substack_draft_id" in columns
    assert not _read(path, "SELECT name FROM sqlite_master WHERE name = 'drafts_new'")


def test_setup_migrates_the_database_at_start_up(tmp_path: Path) -> None:
    """AC4: building the runtime migrates an old database with no extra step."""
    _old_database(tmp_path / "humblebrag.db")
    config = load_config(ENV)
    Runtime(config, data_dir=tmp_path).setup(build_application(config))
    columns = [
        row[1] for row in _read(tmp_path / "humblebrag.db", "PRAGMA table_info(drafts)")
    ]
    assert "substack_draft_id" in columns


# AC5: a failed migration changes nothing and alerts with step and cause


def _break_step(monkeypatch: pytest.MonkeyPatch, step: str) -> None:
    """Make one rebuild step fail with invalid SQL."""
    steps = tuple(
        (name, "THIS IS NOT SQL" if name == step else sql)
        for name, sql in drafts_module._REBUILD_STEPS
    )
    monkeypatch.setattr(drafts_module, "_REBUILD_STEPS", steps)


@pytest.mark.parametrize(
    "step",
    [
        "create the new table",
        "copy the existing rows",
        "drop the old table",
        "rename the new table",
    ],
)
def test_a_failure_at_any_step_leaves_the_original_table_untouched(
    old_db: Path, monkeypatch: pytest.MonkeyPatch, step: str
) -> None:
    """AC5: rollback restores the schema and every row, whichever step breaks."""
    schema = _table_sql(old_db)
    rows = _read(old_db, ROW_SQL.format(cols=OLD_COLUMNS))
    _break_step(monkeypatch, step)
    with pytest.raises(DraftMigrationError) as caught:
        DraftStore(old_db)
    assert caught.value.step == step
    assert "OperationalError" in caught.value.cause
    assert step in str(caught.value)
    assert _table_sql(old_db) == schema
    assert _read(old_db, ROW_SQL.format(cols=OLD_COLUMNS)) == rows


def test_a_copy_that_loses_rows_is_caught_and_rolled_back(
    old_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC5: if the copy lands fewer rows than the original, nothing is changed."""
    steps = tuple(
        (name, sql + " WHERE id < 3" if name == "copy the existing rows" else sql)
        for name, sql in drafts_module._REBUILD_STEPS
    )
    monkeypatch.setattr(drafts_module, "_REBUILD_STEPS", steps)
    schema = _table_sql(old_db)
    with pytest.raises(DraftMigrationError) as caught:
        DraftStore(old_db)
    assert caught.value.step == "verify the copied rows"
    assert _table_sql(old_db) == schema
    assert len(_read(old_db, "SELECT id FROM drafts")) == 4


def test_the_agent_can_start_again_once_the_cause_is_fixed(
    old_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC5: a failed run leaves a database the next run can still migrate."""
    with monkeypatch.context() as broken:
        _break_step(broken, "drop the old table")
        with pytest.raises(DraftMigrationError):
            DraftStore(old_db)
    assert DraftStore(old_db).get(2).status == STATUS_PUBLISHED


def test_a_failed_migration_alerts_stuart_with_the_step_and_cause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC5: start-up fails, and the crash alert to Telegram names step and cause."""
    for name, value in ENV.items():
        monkeypatch.setenv(name, value)
    _old_database(tmp_path / "humblebrag.db")
    _break_step(monkeypatch, "copy the existing rows")

    async def start(config: Config) -> None:
        Runtime(config, data_dir=tmp_path).setup(build_application(config))

    alerts: list[tuple[str, str]] = []

    async def fake_alert(_config: Config, event: str, detail: str) -> bool:
        alerts.append((event, detail))
        return True

    monkeypatch.setattr(entrypoint, "run_until_signalled", start)
    monkeypatch.setattr(entrypoint, "send_standalone_alert", fake_alert)
    with pytest.raises(SystemExit):
        entrypoint.main()
    assert len(alerts) == 1
    assert "copy the existing rows" in alerts[0][1]
    assert "OperationalError" in alerts[0][1]


# AC6: handed_over drafts are not pending


async def test_handed_over_drafts_do_not_count_towards_the_cap(
    tmp_path: Path,
) -> None:
    """AC6: a handed-over draft frees its slot in the pending cap."""
    store = DraftStore(tmp_path / "humblebrag.db")
    first = _make(store, 1)
    _make(store, 2)
    sent: list[str] = []

    async def notify(text: str) -> None:
        sent.append(text)

    gate = RunGate(store, 2, notify)
    assert not await gate.allow_run()
    store.mark_handed_over(first, "sub-1", EDITOR_URL)
    assert store.pending_count() == 1
    assert await gate.allow_run()


def test_handed_over_drafts_are_not_listed_as_pending(tmp_path: Path) -> None:
    """AC6: the pending list leaves out handed-over drafts."""
    store = DraftStore(tmp_path / "humblebrag.db")
    first, second = _make(store, 1), _make(store, 2)
    store.mark_handed_over(first, "sub-1", EDITOR_URL)
    assert [draft.id for draft in store.list_pending()] == [second]


def test_create_cap_ignores_handed_over_drafts(tmp_path: Path) -> None:
    """AC6: the INSERT-time cap also leaves handed-over drafts out."""
    store = DraftStore(tmp_path / "humblebrag.db")
    store.mark_handed_over(_make(store), "sub-1", EDITOR_URL)
    assert store.create("s", "t", "u", "b", max_pending=1).status == STATUS_PENDING


# AC7: mark_handed_over and set_substack_draft_id


def test_mark_handed_over_records_status_id_link_and_time(tmp_path: Path) -> None:
    """AC7: the draft ends handed_over with the Substack ID, link and time."""
    store = DraftStore(
        tmp_path / "humblebrag.db",
        clock=lambda: datetime(2026, 11, 19, 8, 45, tzinfo=UTC),
    )
    draft = store.mark_handed_over(_make(store), "sub-9", EDITOR_URL)
    assert draft.status == STATUS_HANDED_OVER
    assert draft.substack_draft_id == "sub-9"
    assert draft.substack_url == EDITOR_URL
    assert draft.resolved_at == datetime(2026, 11, 19, 8, 45, tzinfo=UTC)


def test_mark_handed_over_keeps_an_id_set_earlier(tmp_path: Path) -> None:
    """AC7: the ID saved straight after creation is the one that is kept."""
    store = DraftStore(tmp_path / "humblebrag.db")
    draft_id = _make(store)
    store.set_substack_draft_id(draft_id, "sub-1")
    assert store.mark_handed_over(draft_id, "sub-1", EDITOR_URL).substack_draft_id == (
        "sub-1"
    )


def test_set_substack_draft_id_keeps_the_draft_pending(tmp_path: Path) -> None:
    """AC7: recording the ID does not resolve the draft."""
    store = DraftStore(tmp_path / "humblebrag.db")
    draft = store.set_substack_draft_id(_make(store), "sub-1")
    assert draft.status == STATUS_PENDING
    assert draft.substack_draft_id == "sub-1"
    assert draft.resolved_at is None


@pytest.mark.parametrize(
    "resolve",
    [
        lambda store, draft_id: store.mark_published(draft_id, "https://x"),
        lambda store, draft_id: store.mark_binned(draft_id),
        lambda store, draft_id: store.mark_handed_over(draft_id, "sub-1", EDITOR_URL),
    ],
    ids=["published", "binned", "handed_over"],
)
def test_both_methods_refuse_a_draft_that_is_not_pending(
    tmp_path: Path, resolve
) -> None:
    """AC7: a resolved draft cannot be handed over or given a Substack ID."""
    store = DraftStore(tmp_path / "humblebrag.db")
    draft_id = _make(store)
    resolve(store, draft_id)
    before = store.get(draft_id)
    with pytest.raises(DraftStateError):
        store.mark_handed_over(draft_id, "other", EDITOR_URL)
    with pytest.raises(DraftStateError):
        store.set_substack_draft_id(draft_id, "other")
    assert store.get(draft_id) == before


def test_both_methods_report_an_unknown_draft(tmp_path: Path) -> None:
    """AC7: an unknown id is DraftNotFoundError, not a silent no-op."""
    store = DraftStore(tmp_path / "humblebrag.db")
    with pytest.raises(DraftNotFoundError):
        store.mark_handed_over(99, "sub", EDITOR_URL)
    with pytest.raises(DraftNotFoundError):
        store.set_substack_draft_id(99, "sub")


# AC8: the nightly backup works on a migrated database


def test_the_backup_of_a_migrated_database_passes_its_integrity_check(
    old_db: Path, tmp_path: Path
) -> None:
    """AC8: back up a migrated database; the copy opens with the new data."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    store = DraftStore(data_dir / DB_FILENAME)
    draft_id = _make(store)
    store.mark_handed_over(draft_id, "sub-1", EDITOR_URL)
    folder = BackupService(data_dir).back_up(datetime(2026, 11, 19).date())
    copy = DraftStore(folder / DB_FILENAME)
    assert copy.get(draft_id).substack_draft_id == "sub-1"
    assert copy.get(draft_id).status == STATUS_HANDED_OVER


def test_the_backup_of_a_database_migrated_from_the_old_schema(
    old_db: Path, tmp_path: Path
) -> None:
    """AC8: the old-schema database, once migrated in place, backs up cleanly."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / DB_FILENAME).write_bytes(old_db.read_bytes())
    DraftStore(data_dir / DB_FILENAME)
    folder = BackupService(data_dir).back_up(datetime(2026, 11, 19).date())
    assert DraftStore(folder / DB_FILENAME).get(2).substack_url == "https://x/p/two"
