"""Seed queue: pick the oldest unused seed, reserve it, then approve or bin it.

A seed is reserved for a pending draft (keyed by draft id) in a small JSON file
next to seed.md, so a restart cannot hand the same seed out twice. Seeds are
identified by their text, so two lines with identical text count as one seed.
Binning only releases the reservation: the line never moves, so the seed is
automatically the first unused line again.
"""

import fcntl
import json
import logging
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import date
from pathlib import Path

from humblebrag.seed import Seed, SeedFile, seed_key, write_atomically

logger = logging.getLogger(__name__)

RESERVATIONS_FILENAME = "seed-reservations.json"
JSON_INDENT = 2
LOCK_SUFFIX = ".lock"
LOCK_TIMEOUT_SECONDS = 5.0
LOCK_POLL_SECONDS = 0.05


class ReservationError(Exception):
    """Base class for seed reservation failures (the caller should alert)."""


class UnknownDraftError(ReservationError):
    """No seed is reserved for the given draft id."""


class SeedNotFoundError(ReservationError):
    """The reserved seed's line is no longer in seed.md."""


class ReservationBusyError(ReservationError):
    """Another process held the reservations lock for too long."""


class ReservationStateError(ReservationError):
    """The reservations file exists but cannot be understood."""


class SeedQueue:
    """Hands out seeds in file order and tracks which are reserved."""

    def __init__(
        self,
        seed_path: Path,
        today: Callable[[], date],
        state_path: Path | None = None,
    ) -> None:
        """Use ``seed_path``; ``today`` supplies the date for marking used."""
        self._seed_path = seed_path
        self._today = today
        self._state_path = state_path or seed_path.with_name(RESERVATIONS_FILENAME)
        self._lock_path = self._state_path.with_suffix(LOCK_SUFFIX)

    def pick(self, draft_id: str) -> Seed | None:
        """Reserve and return the first unused, unreserved seed, or None.

        Picking again for a draft that already holds a still-unused seed
        returns that same seed.
        """
        with self._locked():
            return self._pick_locked(draft_id)

    def approve(self, draft_id: str) -> None:
        """Mark the draft's seed used today and release its reservation."""
        with self._locked():
            self._approve_locked(draft_id)

    def bin_draft(self, draft_id: str) -> bool:
        """Release the draft's reservation; True if it held one.

        The seed was never moved, so it is the first unused line again.
        """
        with self._locked():
            reservations = self._load_reservations()
            if draft_id not in reservations:
                return False
            del reservations[draft_id]
            self._save_reservations(reservations)
            return True

    def seed_texts(self) -> list[str]:
        """Every seed's text (used or not), in file order."""
        return [seed.text for seed in SeedFile.load(self._seed_path).seeds]

    def add_generated(self, text: str) -> bool:
        """Append ``text`` as a ``(generated)`` seed unless it duplicates one.

        Duplicates are matched case-insensitively against every seed, used or
        not. Returns True if the seed was added.
        """
        with self._locked():
            seed_file = SeedFile.load(self._seed_path)
            known = {seed_key(seed.text) for seed in seed_file.seeds}
            if seed_key(text) in known:
                return False
            seed_file.append_seed(text, generated=True)
            seed_file.save()
            return True

    def _pick_locked(self, draft_id: str) -> Seed | None:
        """The body of ``pick``; the caller holds the lock."""
        reservations = self._load_reservations()
        unused = SeedFile.load(self._seed_path).unused
        held = reservations.get(draft_id)
        if held is not None:
            for seed in unused:
                if seed.text == held:
                    return seed
            logger.warning("Seed reserved for draft %s is gone; picking anew", draft_id)
        reserved_texts = {t for d, t in reservations.items() if d != draft_id}
        for seed in unused:
            if seed.text not in reserved_texts:
                reservations[draft_id] = seed.text
                self._save_reservations(reservations)
                return seed
        return None

    def _approve_locked(self, draft_id: str) -> None:
        """The body of ``approve``; the caller holds the lock.

        The seed file is written before the reservation is released, so a crash
        in between leaves a harmless stale reservation: approving again finds
        the seed already used and just clears it.
        """
        reservations = self._load_reservations()
        text = self._reserved_text(reservations, draft_id)
        seed_file = SeedFile.load(self._seed_path)
        target = next((s for s in seed_file.unused if s.text == text), None)
        if target is not None:
            seed_file.mark_used(target, self._today())
            seed_file.save()
        elif not any(s.text == text for s in seed_file.seeds):
            raise SeedNotFoundError(
                f"Seed for draft {draft_id} is no longer in seed.md"
            )
        del reservations[draft_id]
        self._save_reservations(reservations)

    @contextmanager
    def _locked(self) -> Iterator[None]:
        """Hold an exclusive file lock, waiting at most LOCK_TIMEOUT_SECONDS."""
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
        with self._lock_path.open("a") as lock_file:
            while True:
                try:
                    fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise ReservationBusyError(
                            f"{self._lock_path.name} is held by another process"
                        ) from None
                    time.sleep(LOCK_POLL_SECONDS)
            try:
                yield
            finally:
                fcntl.flock(lock_file, fcntl.LOCK_UN)

    @staticmethod
    def _reserved_text(reservations: dict[str, str], draft_id: str) -> str:
        """Return the seed text reserved for ``draft_id`` or raise."""
        try:
            return reservations[draft_id]
        except KeyError:
            raise UnknownDraftError(f"No seed reserved for draft {draft_id}") from None

    def _load_reservations(self) -> dict[str, str]:
        """Read the reservations file (empty if it does not exist yet)."""
        if not self._state_path.exists():
            return {}
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise ReservationStateError(
                f"{self._state_path.name} cannot be read as JSON"
            ) from None
        valid = isinstance(data, dict) and all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in data.items()
        )
        if not valid:
            raise ReservationStateError(
                f"{self._state_path.name} must map draft ids to seed text"
            )
        return data

    def _save_reservations(self, reservations: dict[str, str]) -> None:
        """Atomically write the reservations file."""
        content = json.dumps(reservations, indent=JSON_INDENT, sort_keys=True)
        write_atomically(self._state_path, content + "\n")
