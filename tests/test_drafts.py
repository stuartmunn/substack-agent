"""Tests for the draft store and pending cap (SA-27 acceptance criteria)."""

import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from humblebrag.config import ConfigError, load_config
from humblebrag.drafts import (
    DEFAULT_DB_PATH,
    STATUS_BINNED,
    STATUS_PENDING,
    STATUS_PUBLISHED,
    Draft,
    DraftNotFoundError,
    DraftStateError,
    DraftStore,
    DraftStoreError,
    PendingCapReachedError,
    RunGate,
    format_pending_summary,
    paused_message,
)

ENV = {
    "TELEGRAM_BOT_TOKEN": "123456:TESTTOKEN",
    "TELEGRAM_CHAT_ID": "1",
    "ANTHROPIC_API_KEY": "sk-ant-SECRET-key",
    "CLAUDE_MODEL": "claude-test-model",
    "SUBSTACK_PUBLICATION_URL": "https://example.substack.com",
    "SUBSTACK_COOKIE": "cookie",
}
START = datetime(2026, 11, 18, 8, 30, tzinfo=UTC)


class Clock:
    """A clock that moves forward a minute each time it is read."""

    def __init__(self) -> None:
        """Start at START."""
        self._now = START

    def __call__(self) -> datetime:
        """Return the current fake time, then advance it."""
        current = self._now
        self._now += timedelta(minutes=1)
        return current


@pytest.fixture
def store(tmp_path: Path) -> DraftStore:
    """A store on a temporary database."""
    return DraftStore(tmp_path / "data" / "humblebrag.db", clock=Clock())


def _make(store: DraftStore, n: int = 1, steer: str | None = None) -> Draft:
    """Create draft number ``n``."""
    return store.create(f"seed {n}", f"Title {n}", f"Subtitle {n}", f"Body {n}", steer)


class Notifier:
    """A fake sender recording the messages it was given."""

    def __init__(self) -> None:
        """Start with nothing sent."""
        self.sent: list[str] = []

    async def __call__(self, text: str) -> None:
        """Record ``text``."""
        self.sent.append(text)


# AC1: what each draft stores


def test_a_new_draft_stores_every_field(store: DraftStore) -> None:
    """AC1: id, seed, content, steer history, redo count, status, times, URL."""
    draft = _make(store, steer="make it about trains")
    assert draft == Draft(
        id=1,
        seed="seed 1",
        title="Title 1",
        subtitle="Subtitle 1",
        body="Body 1",
        steer_history=("make it about trains",),
        redo_count=0,
        status=STATUS_PENDING,
        created_at=START,
        resolved_at=None,
        substack_url=None,
    )


def test_ids_are_unique_and_increasing(store: DraftStore) -> None:
    """AC1: every draft gets its own id."""
    assert [_make(store, n).id for n in (1, 2, 3)] == [1, 2, 3]


def test_no_steer_means_an_empty_history(store: DraftStore) -> None:
    """AC1: steer history is empty unless a steer was given."""
    assert _make(store).steer_history == ()


def test_publishing_records_status_time_and_url(store: DraftStore) -> None:
    """AC1: published drafts keep the resolved timestamp and Substack URL."""
    draft = _make(store)
    done = store.mark_published(draft.id, "https://example.substack.com/p/x")
    assert done.status == STATUS_PUBLISHED
    assert done.substack_url == "https://example.substack.com/p/x"
    assert done.resolved_at is not None
    assert done.resolved_at > done.created_at


def test_binning_records_status_and_time(store: DraftStore) -> None:
    """AC1: binned drafts keep a resolved timestamp and no URL."""
    done = store.mark_binned(_make(store).id)
    assert done.status == STATUS_BINNED
    assert done.resolved_at is not None
    assert done.substack_url is None


def test_a_resolved_draft_cannot_be_resolved_again(store: DraftStore) -> None:
    """AC1: only pending drafts can be published or binned."""
    draft = _make(store)
    store.mark_binned(draft.id)
    with pytest.raises(DraftStateError, match="already binned"):
        store.mark_published(draft.id, "https://example.substack.com/p/x")
    with pytest.raises(DraftStateError):
        store.mark_binned(draft.id)
    assert store.get(draft.id).substack_url is None


def test_unknown_draft_is_a_typed_error(store: DraftStore) -> None:
    """A missing id raises DraftNotFoundError, never a bare sqlite or None."""
    with pytest.raises(DraftNotFoundError):
        store.get(99)
    with pytest.raises(DraftNotFoundError):
        store.mark_binned(99)


def test_status_is_restricted_by_the_schema(store: DraftStore, tmp_path: Path) -> None:
    """AC1: the database itself rejects any status but the three allowed."""
    _make(store)
    connection = sqlite3.connect(tmp_path / "data" / "humblebrag.db")
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute("UPDATE drafts SET status = 'lost' WHERE id = 1")
    connection.close()


def test_unreadable_database_is_a_typed_error(tmp_path: Path) -> None:
    """ERR-01: a corrupt database file raises DraftStoreError without details."""
    path = tmp_path / "bad.db"
    path.write_bytes(b"this is not a sqlite database" * 50)
    with pytest.raises(DraftStoreError, match="DatabaseError"):
        DraftStore(path)


def test_uncreatable_database_directory_is_a_typed_error(tmp_path: Path) -> None:
    """ERR-14: a directory that cannot be created raises DraftStoreError."""
    blocker = tmp_path / "blocker"
    blocker.write_text("a file, not a directory")
    with pytest.raises(DraftStoreError, match="cannot be created"):
        DraftStore(blocker / "humblebrag.db")


def test_default_database_lives_on_the_data_volume() -> None:
    """DKR-04: persistent state defaults to /data."""
    assert DEFAULT_DB_PATH.parts[:2] == ("/", "data")


# AC2: unanswered drafts queue up, each independent


def test_a_second_draft_leaves_the_first_pending(store: DraftStore) -> None:
    """AC2: both stay pending, oldest first, each addressable by its own id."""
    first, second = _make(store, 1), _make(store, 2)
    assert [d.id for d in store.list_pending()] == [first.id, second.id]
    assert store.pending_count() == 2
    store.mark_binned(first.id)
    assert [d.id for d in store.list_pending()] == [second.id]
    assert store.get(second.id).status == STATUS_PENDING


# AC3: restart


def test_pending_drafts_survive_a_restart(tmp_path: Path) -> None:
    """AC3: a fresh store on the same file sees every pending draft."""
    path = tmp_path / "humblebrag.db"
    first = DraftStore(path, clock=Clock())
    kept, binned = _make(first, 1), _make(first, 2)
    first.mark_binned(binned.id)
    restarted = DraftStore(path)
    assert restarted.list_pending() == [kept]


def test_summary_lists_each_pending_draft(store: DraftStore) -> None:
    """AC3: the restart message is a numbered list with ids for the buttons."""
    drafts = [_make(store, 1), _make(store, 2)]
    text = format_pending_summary(drafts)
    assert text.splitlines() == [
        "2 drafts waiting:",
        "1. Title 1 (#1, 18 Nov)",
        "2. Title 2 (#2, 18 Nov)",
    ]


def test_summary_edge_cases(store: DraftStore) -> None:
    """AC3: singular wording, an empty list, and long titles are handled."""
    assert format_pending_summary([]) == "No drafts waiting."
    one = store.create("s", "x" * 100, "sub", "body")
    text = format_pending_summary([one])
    assert text.startswith("1 draft waiting:")
    assert "x" * 59 + "…" in text


# AC4/AC5: the pending cap


def test_cap_defaults_to_7_and_is_configurable() -> None:
    """AC4: MAX_PENDING_DRAFTS defaults to 7 and can be set."""
    assert load_config(ENV).max_pending_drafts == 7
    assert load_config({**ENV, "MAX_PENDING_DRAFTS": "3"}).max_pending_drafts == 3


@pytest.mark.parametrize("value", ["0", "-1", "many"])
def test_invalid_cap_is_a_config_error(value: str) -> None:
    """AC4: a non-positive or non-numeric cap is rejected."""
    with pytest.raises(ConfigError, match="MAX_PENDING_DRAFTS"):
        load_config({**ENV, "MAX_PENDING_DRAFTS": value})


def test_paused_message_matches_the_story_wording() -> None:
    """AC4: the exact message from the story."""
    assert paused_message(7) == "7 drafts waiting, run paused. Clear some to resume."
    assert paused_message(1) == "1 draft waiting, run paused. Clear some to resume."


async def test_at_the_cap_the_run_is_blocked_with_one_message(
    store: DraftStore,
) -> None:
    """AC4: 7 pending -> no run, exactly one message to Stuart."""
    for n in range(7):
        _make(store, n)
    notifier = Notifier()
    gate = RunGate(store, 7, notifier)
    assert await gate.allow_run() is False
    assert notifier.sent == ["7 drafts waiting, run paused. Clear some to resume."]
    assert store.pending_count() == 7


async def test_below_the_cap_the_run_goes_ahead_silently(store: DraftStore) -> None:
    """AC5: 6 pending of 7 -> allowed, no message."""
    for n in range(6):
        _make(store, n)
    notifier = Notifier()
    assert await RunGate(store, 7, notifier).allow_run() is True
    assert notifier.sent == []


async def test_clearing_a_draft_resumes_runs(store: DraftStore) -> None:
    """AC5: once pending drops below the cap the next run is allowed."""
    drafts = [_make(store, n) for n in range(7)]
    notifier = Notifier()
    gate = RunGate(store, 7, notifier)
    assert await gate.allow_run() is False
    store.mark_binned(drafts[0].id)
    assert await gate.allow_run() is True
    assert len(notifier.sent) == 1


async def test_the_same_gate_serves_now_and_the_scheduler(store: DraftStore) -> None:
    """AC5: /now uses the same gate, so it respects the cap too."""
    for n in range(2):
        _make(store, n)
    notifier = Notifier()
    gate = RunGate(store, 2, notifier)
    assert await gate.allow_run() is False  # e.g. /now
    assert await gate.allow_run() is False  # e.g. the scheduled run
    assert len(notifier.sent) == 2


async def test_a_lowered_cap_reports_the_real_pending_count(
    store: DraftStore,
) -> None:
    """AC4: if the cap was lowered below the pending count, say the true number."""
    for n in range(5):
        _make(store, n)
    notifier = Notifier()
    await RunGate(store, 3, notifier).allow_run()
    assert notifier.sent[0].startswith("5 drafts waiting")


def test_create_refuses_to_exceed_the_cap_and_stores_nothing(
    store: DraftStore,
) -> None:
    """ERR-12: the cap is enforced by the insert, not just checked beforehand."""
    for n in range(3):
        store.create("s", f"T{n}", "sub", "body", max_pending=3)
    with pytest.raises(PendingCapReachedError, match="3 drafts"):
        store.create("s", "T3", "sub", "body", max_pending=3)
    assert store.pending_count() == 3


def test_resolved_drafts_do_not_count_towards_the_cap(store: DraftStore) -> None:
    """AC5: only pending drafts fill the cap."""
    first = store.create("s", "T0", "sub", "body", max_pending=1)
    store.mark_binned(first.id)
    assert store.create("s", "T1", "sub", "body", max_pending=1).id == 2


def test_racing_creators_cannot_exceed_the_cap(tmp_path: Path) -> None:
    """ERR-12: ten simultaneous runs with cap 3 produce exactly 3 drafts."""
    path = tmp_path / "humblebrag.db"
    DraftStore(path)
    results: list[bool] = []
    lock = threading.Lock()
    start = threading.Barrier(10)

    def attempt() -> None:
        racer = DraftStore(path)
        start.wait()
        try:
            racer.create("s", "T", "sub", "body", max_pending=3)
            outcome = True
        except PendingCapReachedError:
            outcome = False
        with lock:
            results.append(outcome)

    threads = [threading.Thread(target=attempt) for _ in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results.count(True) == 3
    assert DraftStore(path).pending_count() == 3
