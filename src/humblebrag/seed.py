"""seed.md: a hand-editable checklist of post seeds, read and written safely.

Format (one seed per line; every other line is preserved untouched):

    - [ ] <text>                              unused
    - [x] <text> (used YYYY-MM-DD)            used
    - [x] <text> (skipped YYYY-MM-DD)         dropped from the queue (/unseed)
    - [ ] <text> (generated)                  invented by the agent, unused
    - [x] <text> (generated) (used YYYY-MM-DD)
    - [x] <text> (generated) (skipped YYYY-MM-DD)

Trailing `(generated)` and, on checked lines only, one of `(used YYYY-MM-DD)` or
`(skipped YYYY-MM-DD)` are metadata. A trailing `(used ...)` or `(skipped ...)`
on an unchecked line contradicts the format, so it is kept as part of the
seed's text; so is a second status suffix on a checked line.
"""

import logging
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_SEED_PATH = Path("/data/seed.md")
FILE_MODE = 0o644
ENCODING = "utf-8"
_SEED_LINE = re.compile(r"^- \[(?P<mark>[ xX])\] (?P<rest>\S.*)$")
_USED_SUFFIX = re.compile(r"\s+\(used (?P<date>\d{4}-\d{2}-\d{2})\)\s*$")
_SKIPPED_SUFFIX = re.compile(r"\s+\(skipped (?P<date>\d{4}-\d{2}-\d{2})\)\s*$")
_GENERATED_SUFFIX = re.compile(r"\s+\(generated\)\s*$")
_STATUS_SUFFIXES = (("used", _USED_SUFFIX), ("skipped", _SKIPPED_SUFFIX))


class SeedFileError(Exception):
    """Raised when the seed file cannot be read as text."""


@dataclass(frozen=True)
class Seed:
    """One seed line: its position in the file and its parsed fields."""

    line_index: int
    text: str
    used: bool
    used_date: date | None
    generated: bool
    skipped_date: date | None = None


def _strip_status(rest: str) -> tuple[str, str, date] | None:
    """Split a trailing ``(used ...)`` or ``(skipped ...)`` off ``rest``.

    Returns the text before it, which suffix it was and its date, or None if
    there is no such suffix or its date is not a real date.
    """
    for kind, pattern in _STATUS_SUFFIXES:
        match = pattern.search(rest)
        if match is None:
            continue
        try:
            return rest[: match.start()], kind, date.fromisoformat(match.group("date"))
        except ValueError:
            return None
    return None


def parse_seed_line(line: str, line_index: int = 0) -> Seed | None:
    """Parse a seed line, or return None for blank, comment or other lines."""
    match = _SEED_LINE.match(line.rstrip("\r"))
    if match is None:
        return None
    rest = match.group("rest").rstrip()
    checked = match.group("mark") != " "
    used_date: date | None = None
    skipped_date: date | None = None
    generated = False
    while True:
        status = (
            _strip_status(rest)
            if checked and used_date is None and skipped_date is None
            else None
        )
        if status is not None:
            rest, kind, when = status
            if kind == "used":
                used_date = when
            else:
                skipped_date = when
            continue
        generated_match = _GENERATED_SUFFIX.search(rest)
        if generated_match and not generated:
            generated = True
            rest = rest[: generated_match.start()]
            continue
        break
    text = rest.strip()
    if not text:
        return None
    return Seed(line_index, text, checked, used_date, generated, skipped_date)


def seed_key(text: str) -> str:
    """Normalise seed text for duplicate checks: case-insensitive, spaces collapsed."""
    return " ".join(text.split()).casefold()


def format_seed_line(
    text: str,
    used_on: date | None,
    generated: bool,
    skipped_on: date | None = None,
) -> str:
    """Build the canonical line for a seed (a seed is used or skipped, not both)."""
    if used_on and skipped_on:
        raise ValueError("A seed cannot be both used and skipped")
    parts = [f"- [{'x' if used_on or skipped_on else ' '}] {text}"]
    if generated:
        parts.append("(generated)")
    if used_on:
        parts.append(f"(used {used_on.isoformat()})")
    if skipped_on:
        parts.append(f"(skipped {skipped_on.isoformat()})")
    return " ".join(parts)


def write_atomically(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` via a temp file in the same directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if path.exists() else FILE_MODE
    handle, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    temp_path = Path(temp_name)
    try:
        with os.fdopen(handle, "wb") as temp_file:
            temp_file.write(content.encode(ENCODING))
            temp_file.flush()
            os.fsync(temp_file.fileno())
        temp_path.chmod(mode)
        os.replace(temp_path, path)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise


class SeedFile:
    """The seed file held as raw lines, so untouched lines round-trip exactly."""

    def __init__(self, path: Path, lines: list[str]) -> None:
        """Hold ``lines`` (split on newline, so a trailing newline is a final '')."""
        self.path = path
        self._lines = lines

    @classmethod
    def load(cls, path: Path = DEFAULT_SEED_PATH) -> "SeedFile":
        """Read the file, creating it empty (and its directory) if missing."""
        if not path.exists():
            write_atomically(path, "")
            logger.info("Created empty seed file at %s", path)
        try:
            content = path.read_bytes().decode(ENCODING)
        except UnicodeDecodeError:
            raise SeedFileError(f"{path} is not valid {ENCODING} text") from None
        return cls(path, content.split("\n"))

    @property
    def text(self) -> str:
        """The full file content, exactly as it would be saved."""
        return "\n".join(self._lines)

    @property
    def seeds(self) -> list[Seed]:
        """Every seed line, in file order."""
        parsed = (parse_seed_line(line, i) for i, line in enumerate(self._lines))
        return [seed for seed in parsed if seed is not None]

    @property
    def unused(self) -> list[Seed]:
        """Seeds not yet used, in file order."""
        return [seed for seed in self.seeds if not seed.used]

    def mark_used(self, seed: Seed, on: date) -> None:
        """Rewrite only ``seed``'s line as used on ``on``."""
        self._rewrite(seed, format_seed_line(seed.text, on, seed.generated))

    def mark_skipped(self, seed: Seed, on: date) -> None:
        """Rewrite only ``seed``'s line as skipped on ``on``, keeping (generated)."""
        line = format_seed_line(seed.text, None, seed.generated, skipped_on=on)
        self._rewrite(seed, line)

    def _rewrite(self, seed: Seed, new_line: str) -> None:
        """Replace ``seed``'s own line (keeping its CR) after checking it is
        still the same, unchecked seed that was read."""
        in_range = 0 <= seed.line_index < len(self._lines)
        current = (
            parse_seed_line(self._lines[seed.line_index], seed.line_index)
            if in_range
            else None
        )
        if current != seed:
            raise ValueError("Seed is stale: its line has changed since it was read")
        if seed.used:
            raise ValueError("Seed is already used")
        line = self._lines[seed.line_index]
        carriage_return = "\r" if line.endswith("\r") else ""
        self._lines[seed.line_index] = new_line + carriage_return

    def _new_line(self, text: str, generated: bool) -> str:
        """Build an unused seed line, matching the file's line endings."""
        if "\n" in text or "\r" in text or not text.strip():
            raise ValueError("Seed text must be one non-empty line")
        carriage_return = "\r" if any(x.endswith("\r") for x in self._lines) else ""
        return format_seed_line(text.strip(), None, generated) + carriage_return

    def insert_seed_first(self, text: str, generated: bool = False) -> None:
        """Add an unused seed directly above the first unused seed line.

        "First" means the first unused seed, wherever it sits in a hand-edited
        file, not line 1. With no unused seed the line is appended. Every other
        line is untouched.
        """
        unused = self.unused
        if not unused:
            self.append_seed(text, generated)
            return
        self._lines.insert(unused[0].line_index, self._new_line(text, generated))

    def append_seed(self, text: str, generated: bool = False) -> None:
        """Add an unused seed at the end, leaving every other line untouched."""
        line = self._new_line(text, generated)
        if self._lines[-1] == "":
            self._lines.insert(len(self._lines) - 1, line)
        else:
            self._lines.append(line)

    def save(self) -> None:
        """Atomically write the file: temp file, then rename."""
        write_atomically(self.path, self.text)
