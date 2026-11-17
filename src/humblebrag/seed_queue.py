"""Seed queue: pick the oldest unused seed, reserve it, then approve or bin it.

A seed is reserved for a pending draft (keyed by draft id) in a small JSON file
next to seed.md, so a restart cannot hand the same seed out twice. Seeds are
identified by their text, so two lines with identical text count as one seed.
Binning only releases the reservation: the line never moves, so the seed is
automatically the first unused line again.
"""

import json
import logging
from collections.abc import Callable
from datetime import date
from pathlib import Path

from humblebrag.seed import Seed, SeedFile, write_atomically

logger = logging.getLogger(__name__)

RESERVATIONS_FILENAME = "seed-reservations.json"
JSON_INDENT = 2


class ReservationError(Exception):
    """Base class for seed reservation failures (the caller should alert)."""


class UnknownDraftError(ReservationError):
    """No seed is reserved for the given draft id."""


class SeedNotFoundError(ReservationError):
    """The reserved seed's line is no longer in seed.md."""


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

    def pick(self, draft_id: str) -> Seed | None:
        """Reserve and return the first unused, unreserved seed, or None.

        Picking again for a draft that already holds a still-unused seed
        returns that same seed.
        """
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

    def approve(self, draft_id: str) -> None:
        """Mark the draft's seed used today and release its reservation."""
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

    def bin_draft(self, draft_id: str) -> bool:
        """Release the draft's reservation; True if it held one.

        The seed was never moved, so it is the first unused line again.
        """
        reservations = self._load_reservations()
        if draft_id not in reservations:
            return False
        del reservations[draft_id]
        self._save_reservations(reservations)
        return True

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
