"""Tests for the seed queue (SA-20 acceptance criteria)."""

import fcntl
import json
from datetime import date
from pathlib import Path

import pytest

from humblebrag import seed_queue
from humblebrag.seed_queue import (
    RESERVATIONS_FILENAME,
    InvalidSeedError,
    InvalidSeedNumberError,
    ReservationBusyError,
    ReservationStateError,
    SeedInUseError,
    SeedNotFoundError,
    SeedQueue,
    UnknownDraftError,
)

TODAY = date(2026, 11, 17)
SEED_FILE = (
    "# my seeds\n"
    "\n"
    "- [x] Already used (used 2026-11-01)\n"
    "- [ ] First unused\n"
    "A stray note\n"
    "- [ ] Second unused\n"
    "\n"
    "- [ ] Third unused (generated)\n"
)


@pytest.fixture
def seed_path(tmp_path: Path) -> Path:
    """A seed file with a mix of used, unused and non-seed lines."""
    path = tmp_path / "seed.md"
    path.write_text(SEED_FILE)
    return path


def _queue(seed_path: Path) -> SeedQueue:
    """A queue whose 'today' is fixed."""
    return SeedQueue(seed_path, today=lambda: TODAY)


def test_pick_returns_the_first_unused_seed_in_file_order(seed_path: Path) -> None:
    """AC1: the first unused line is returned, skipping used and non-seed lines."""
    seed = _queue(seed_path).pick("draft-1")
    assert seed is not None
    assert seed.text == "First unused"
    assert seed.line_index == 3


def test_pick_returns_none_when_nothing_is_unused(tmp_path: Path) -> None:
    """AC1: no unused seeds means None, for the caller to handle."""
    path = tmp_path / "seed.md"
    path.write_text("- [x] Used (used 2026-11-01)\n")
    assert _queue(path).pick("draft-1") is None


def test_pick_on_a_missing_seed_file_creates_it_and_returns_none(
    tmp_path: Path,
) -> None:
    """AC1: a missing seed file is created empty and yields no seed."""
    path = tmp_path / "seed.md"
    assert _queue(path).pick("draft-1") is None
    assert path.read_text() == ""


def test_pick_does_not_modify_the_seed_file(seed_path: Path) -> None:
    """AC1: reserving is not marking; seed.md is untouched until approval."""
    _queue(seed_path).pick("draft-1")
    assert seed_path.read_text() == SEED_FILE


def test_reservation_is_written_next_to_the_seed_file(seed_path: Path) -> None:
    """AC1: the reservation is persisted on the data volume."""
    _queue(seed_path).pick("draft-1")
    state = json.loads((seed_path.parent / RESERVATIONS_FILENAME).read_text())
    assert state == {"draft-1": "First unused"}


def test_reserved_seed_is_not_picked_again(seed_path: Path) -> None:
    """AC4: a seed reserved by a pending draft goes to no other draft."""
    queue = _queue(seed_path)
    first = queue.pick("draft-1")
    second = queue.pick("draft-2")
    third = queue.pick("draft-3")
    fourth = queue.pick("draft-4")
    assert [s.text for s in (first, second, third)] == [
        "First unused",
        "Second unused",
        "Third unused",
    ]
    assert fourth is None


def test_reservation_survives_a_restart(seed_path: Path) -> None:
    """AC4: a new queue object (a restart) still honours the reservation."""
    _queue(seed_path).pick("draft-1")
    restarted = _queue(seed_path)
    other = restarted.pick("draft-2")
    assert other is not None
    assert other.text == "Second unused"


def test_picking_again_for_the_same_draft_returns_the_same_seed(
    seed_path: Path,
) -> None:
    """A retry for the same draft is idempotent and reserves nothing extra."""
    queue = _queue(seed_path)
    first = queue.pick("draft-1")
    again = queue.pick("draft-1")
    assert first == again
    state = json.loads((seed_path.parent / RESERVATIONS_FILENAME).read_text())
    assert state == {"draft-1": "First unused"}


def test_reserved_seed_that_vanished_is_replaced_on_repick(seed_path: Path) -> None:
    """If Stuart deletes a reserved line, re-picking for that draft moves on."""
    queue = _queue(seed_path)
    queue.pick("draft-1")
    seed_path.write_text(SEED_FILE.replace("- [ ] First unused\n", ""))
    seed = queue.pick("draft-1")
    assert seed is not None
    assert seed.text == "Second unused"


def test_approve_marks_the_seed_used_with_todays_date(seed_path: Path) -> None:
    """AC2: approval marks the seed [x] with today's date, changing nothing else."""
    queue = _queue(seed_path)
    queue.pick("draft-1")
    queue.approve("draft-1")
    expected = SEED_FILE.replace(
        "- [ ] First unused\n", "- [x] First unused (used 2026-11-17)\n"
    )
    assert seed_path.read_text() == expected


def test_approve_releases_the_reservation_and_queue_moves_on(seed_path: Path) -> None:
    """AC2: after approval the next pick is the next seed in order."""
    queue = _queue(seed_path)
    queue.pick("draft-1")
    queue.approve("draft-1")
    state = json.loads((seed_path.parent / RESERVATIONS_FILENAME).read_text())
    assert state == {}
    nxt = queue.pick("draft-2")
    assert nxt is not None
    assert nxt.text == "Second unused"


def test_approving_a_generated_seed_keeps_its_marker(tmp_path: Path) -> None:
    """AC2: an invented seed keeps (generated) when marked used."""
    path = tmp_path / "seed.md"
    path.write_text("- [ ] Invented (generated)\n")
    queue = _queue(path)
    queue.pick("d")
    queue.approve("d")
    assert path.read_text() == "- [x] Invented (generated) (used 2026-11-17)\n"


def test_bin_returns_the_seed_to_the_front(seed_path: Path) -> None:
    """AC3: a binned seed is the first unused line again, ahead of later seeds."""
    queue = _queue(seed_path)
    queue.pick("draft-1")  # First unused
    queue.pick("draft-2")  # Second unused
    assert queue.bin_draft("draft-1") is True
    nxt = queue.pick("draft-3")
    assert nxt is not None
    assert nxt.text == "First unused"


def test_bin_leaves_the_seed_file_untouched(seed_path: Path) -> None:
    """AC3: binning does not rewrite seed.md."""
    queue = _queue(seed_path)
    queue.pick("draft-1")
    queue.bin_draft("draft-1")
    assert seed_path.read_text() == SEED_FILE


def test_bin_of_unknown_draft_is_a_harmless_false(seed_path: Path) -> None:
    """A repeated bin (double-tap) is not an error."""
    assert _queue(seed_path).bin_draft("nope") is False


def test_approve_of_unknown_draft_raises(seed_path: Path) -> None:
    """Approving a draft that holds no seed raises a typed error to alert on."""
    with pytest.raises(UnknownDraftError):
        _queue(seed_path).approve("nope")


def test_approve_when_seed_line_was_deleted_raises_and_keeps_reservation(
    seed_path: Path,
) -> None:
    """A vanished seed is reported, not silently dropped."""
    queue = _queue(seed_path)
    queue.pick("draft-1")
    seed_path.write_text(SEED_FILE.replace("- [ ] First unused\n", ""))
    with pytest.raises(SeedNotFoundError):
        queue.approve("draft-1")
    state = json.loads((seed_path.parent / RESERVATIONS_FILENAME).read_text())
    assert state == {"draft-1": "First unused"}


def test_approve_when_seed_was_hand_marked_used_is_idempotent(
    seed_path: Path,
) -> None:
    """If Stuart ticked the seed by hand, approval just clears the reservation."""
    queue = _queue(seed_path)
    queue.pick("draft-1")
    ticked = SEED_FILE.replace(
        "- [ ] First unused\n", "- [x] First unused (used 2026-11-16)\n"
    )
    seed_path.write_text(ticked)
    queue.approve("draft-1")
    assert seed_path.read_text() == ticked
    state = json.loads((seed_path.parent / RESERVATIONS_FILENAME).read_text())
    assert state == {}


def test_identical_seed_texts_count_as_one_seed(tmp_path: Path) -> None:
    """Seeds are identified by text, so duplicate lines are reserved together."""
    path = tmp_path / "seed.md"
    path.write_text("- [ ] Same\n- [ ] Same\n")
    queue = _queue(path)
    assert queue.pick("draft-1") is not None
    assert queue.pick("draft-2") is None


@pytest.mark.parametrize("content", ["{not json", "[1, 2]", '{"d": 5}', '"text"'])
def test_corrupt_reservations_file_is_a_typed_error(
    seed_path: Path, content: str
) -> None:
    """A damaged state file raises a clear error and never echoes its contents."""
    (seed_path.parent / RESERVATIONS_FILENAME).write_text(content)
    with pytest.raises(ReservationStateError) as error:
        _queue(seed_path).pick("draft-1")
    assert content not in str(error.value)
    assert RESERVATIONS_FILENAME in str(error.value)


def test_custom_state_path_is_used(seed_path: Path, tmp_path: Path) -> None:
    """The reservations file location can be overridden."""
    state = tmp_path / "elsewhere" / "state.json"
    queue = SeedQueue(seed_path, today=lambda: TODAY, state_path=state)
    queue.pick("draft-1")
    assert json.loads(state.read_text()) == {"draft-1": "First unused"}


def _hold_lock(seed_path: Path) -> object:
    """Hold the queue's lock file from 'another process' (a second open file)."""
    lock_path = (seed_path.parent / RESERVATIONS_FILENAME).with_suffix(
        seed_queue.LOCK_SUFFIX
    )
    holder = lock_path.open("a")
    fcntl.flock(holder, fcntl.LOCK_EX)
    return holder


@pytest.mark.parametrize("operation", ["pick", "approve", "bin_draft"])
def test_operations_wait_for_the_lock_then_fail_with_a_typed_error(
    seed_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    """Every operation takes the lock; if it stays held, a typed error is raised
    after a bounded wait instead of two processes interleaving."""
    monkeypatch.setattr(seed_queue, "LOCK_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(seed_queue, "LOCK_POLL_SECONDS", 0.01)
    holder = _hold_lock(seed_path)
    try:
        with pytest.raises(ReservationBusyError):
            getattr(_queue(seed_path), operation)("draft-1")
    finally:
        holder.close()  # type: ignore[attr-defined]
    assert not (seed_path.parent / RESERVATIONS_FILENAME).exists()


def test_lock_is_released_after_each_operation(seed_path: Path) -> None:
    """The lock never leaks: a second queue can operate straight after."""
    _queue(seed_path).pick("draft-1")
    assert _queue(seed_path).pick("draft-2") is not None


def test_lock_is_released_when_an_operation_raises(seed_path: Path) -> None:
    """An error inside the critical section still releases the lock."""
    queue = _queue(seed_path)
    with pytest.raises(UnknownDraftError):
        queue.approve("nope")
    assert queue.pick("draft-1") is not None


def test_crash_between_marking_used_and_releasing_is_recoverable(
    seed_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the process dies after seed.md is updated but before the reservation
    is released, approving again clears it without marking the seed twice."""
    queue = _queue(seed_path)
    queue.pick("draft-1")
    real_save = SeedQueue._save_reservations
    calls = {"count": 0}

    def crash_once(self: SeedQueue, reservations: dict[str, str]) -> None:
        calls["count"] += 1
        raise OSError("crashed before releasing")

    monkeypatch.setattr(SeedQueue, "_save_reservations", crash_once)
    with pytest.raises(OSError, match="crashed"):
        queue.approve("draft-1")
    monkeypatch.setattr(SeedQueue, "_save_reservations", real_save)
    marked = SEED_FILE.replace(
        "- [ ] First unused\n", "- [x] First unused (used 2026-11-17)\n"
    )
    assert seed_path.read_text() == marked
    queue.approve("draft-1")
    assert seed_path.read_text() == marked
    state = json.loads((seed_path.parent / RESERVATIONS_FILENAME).read_text())
    assert state == {}
    nxt = queue.pick("draft-2")
    assert nxt is not None
    assert nxt.text == "Second unused"


# SA-45: skipping an unused seed by its number


def test_skip_seed_marks_the_numbered_unused_seed_and_returns_its_text(
    seed_path: Path,
) -> None:
    """SA-45 AC1/AC2: number 2 of the unused list is skipped, dated today, and
    every other line is byte-for-byte what it was."""
    queue = _queue(seed_path)
    assert queue.skip_seed(2) == "Second unused"
    assert seed_path.read_text() == SEED_FILE.replace(
        "- [ ] Second unused", "- [x] Second unused (skipped 2026-11-17)"
    )


def test_skip_seed_keeps_the_generated_marker(seed_path: Path) -> None:
    """SA-45 AC2: skipping an invented seed keeps (generated)."""
    assert _queue(seed_path).skip_seed(3) == "Third unused"
    assert (
        "- [x] Third unused (generated) (skipped 2026-11-17)" in seed_path.read_text()
    )


def test_skipped_seeds_are_not_picked_or_listed(seed_path: Path) -> None:
    """SA-45 AC3: a skipped seed is out of the queue, in order, for good."""
    queue = _queue(seed_path)
    queue.skip_seed(1)
    assert queue.unused_texts() == ["Second unused", "Third unused"]
    picked = queue.pick("draft-1")
    assert picked is not None
    assert picked.text == "Second unused"


def test_skipped_seeds_still_count_as_known_when_a_seed_is_invented(
    seed_path: Path,
) -> None:
    """SA-45 AC3: inventing a seed identical to a skipped one is a duplicate."""
    queue = _queue(seed_path)
    queue.skip_seed(1)
    before = seed_path.read_text()
    assert "First unused" in queue.seed_texts()
    assert queue.add_generated("first   UNUSED", "draft-1") is None
    assert seed_path.read_text() == before


def test_the_same_text_can_be_added_again_after_skipping(seed_path: Path) -> None:
    """SA-45 AC7: /seed re-adds a skipped seed as a new unused line at the end,
    leaving the skipped line in place as history."""
    queue = _queue(seed_path)
    queue.skip_seed(1)
    added = queue.add_seed("First unused")
    assert added.text == "First unused"
    text = seed_path.read_text()
    assert "- [x] First unused (skipped 2026-11-17)" in text
    assert text.rstrip("\n").endswith("- [ ] First unused")
    assert queue.unused_texts() == ["Second unused", "Third unused", "First unused"]


def test_the_re_added_seed_can_be_skipped_again(seed_path: Path) -> None:
    """The re-added copy is an ordinary unused seed, skippable in its turn."""
    queue = _queue(seed_path)
    queue.skip_seed(1)
    queue.add_seed("First unused")
    assert queue.skip_seed(3) == "First unused"
    assert queue.unused_texts() == ["Second unused", "Third unused"]


@pytest.mark.parametrize("number", [0, -1, 4, 99, 10**30])
def test_skip_seed_refuses_a_number_outside_the_list(
    seed_path: Path, number: int
) -> None:
    """SA-45 AC4/PY-07: the documented error, not IndexError; nothing changes."""
    with pytest.raises(InvalidSeedNumberError, match="1 to 3"):
        _queue(seed_path).skip_seed(number)
    assert seed_path.read_text() == SEED_FILE


def test_skip_seed_with_nothing_unused_says_so(tmp_path: Path) -> None:
    """SA-45 AC4: an empty queue has no valid number."""
    path = tmp_path / "seed.md"
    path.write_text("- [x] done (used 2026-11-01)\n")
    with pytest.raises(InvalidSeedNumberError, match="no unused seeds"):
        _queue(path).skip_seed(1)
    assert path.read_text() == "- [x] done (used 2026-11-01)\n"


def test_skip_seed_refuses_a_seed_reserved_by_a_pending_draft(
    seed_path: Path,
) -> None:
    """SA-45 AC5: the reserved seed (first unused) cannot be skipped; the reply
    names the fix; nothing changes."""
    queue = _queue(seed_path)
    queue.pick("draft-1")
    with pytest.raises(SeedInUseError, match="Bin or Approve that draft first"):
        queue.skip_seed(1)
    assert seed_path.read_text() == SEED_FILE
    assert queue.skip_seed(2) == "Second unused"  # others are still fine


def test_a_reserved_seed_can_be_skipped_once_the_draft_is_binned(
    seed_path: Path,
) -> None:
    """SA-45 AC5: the refusal lasts only while the reservation does."""
    queue = _queue(seed_path)
    queue.pick("draft-1")
    queue.bin_draft("draft-1")
    assert queue.skip_seed(1) == "First unused"


def test_skip_seed_waits_for_the_lock_then_fails_with_a_typed_error(
    seed_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SA-45 AC6/ERR-11: it takes the existing lock; a stuck lock is a bounded
    wait and a typed error, and nothing is written."""
    monkeypatch.setattr(seed_queue, "LOCK_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(seed_queue, "LOCK_POLL_SECONDS", 0.01)
    holder = _hold_lock(seed_path)
    try:
        with pytest.raises(ReservationBusyError):
            _queue(seed_path).skip_seed(1)
    finally:
        holder.close()  # type: ignore[attr-defined]
    assert seed_path.read_text() == SEED_FILE


def test_skip_seed_writes_atomically(
    seed_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SA-45 AC6: a failed write leaves the original file and no temp files."""

    def failing_replace(_source: Path, _target: Path) -> None:
        raise OSError("disk exploded")

    monkeypatch.setattr("humblebrag.seed.os.replace", failing_replace)
    with pytest.raises(OSError, match="disk exploded"):
        _queue(seed_path).skip_seed(1)
    assert seed_path.read_text() == SEED_FILE
    assert [p.name for p in seed_path.parent.iterdir() if p.name.startswith(".")] == []


def test_skip_seed_rejects_reservation_state_it_cannot_read(
    seed_path: Path,
) -> None:
    """A corrupt reservations file is the usual typed error, not a skip."""
    (seed_path.parent / RESERVATIONS_FILENAME).write_text("not json")
    with pytest.raises(ReservationStateError):
        _queue(seed_path).skip_seed(1)
    assert seed_path.read_text() == SEED_FILE


def test_invalid_seed_error_is_still_what_add_seed_raises(seed_path: Path) -> None:
    """Skipping does not change /seed's own refusals."""
    with pytest.raises(InvalidSeedError):
        _queue(seed_path).add_seed("First unused")
