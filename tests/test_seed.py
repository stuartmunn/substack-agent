"""Tests for the seed.md format and parser (SA-19 acceptance criteria)."""

import os
import stat
from datetime import date
from pathlib import Path

import pytest

from humblebrag import seed as seed_module
from humblebrag.seed import (
    DEFAULT_SEED_PATH,
    Seed,
    SeedFile,
    SeedFileError,
    format_seed_line,
    parse_seed_line,
)

TODAY = date(2026, 11, 17)
HAND_EDITED = (
    "# My seeds - edit freely\n"
    "\n"
    "- [ ] Rebranding my morning toast as a thought-leadership journey\n"
    "Some stray note I typed by hand\n"
    "- [x] Humbled to announce my lunch (used 2026-11-01)\n"
    "\n"
    "- [x] Why failing at parallel parking taught me agile "
    "(generated) (used 2026-11-10)\n"
    "-[ ] malformed line with no space\n"
    "- [ ] An invented one (generated)\n"
    "  - [ ] indented line is not a seed\n"
    "\n"
)


def _write(path: Path, content: str) -> Path:
    """Write ``content`` as UTF-8 bytes (no newline translation)."""
    path.write_bytes(content.encode("utf-8"))
    return path


def test_unused_line_parses() -> None:
    """AC1: `- [ ] <text>` is an unused seed."""
    parsed = parse_seed_line("- [ ] Toast thoughts")
    assert parsed == Seed(0, "Toast thoughts", False, None, False)


def test_used_line_parses_with_date() -> None:
    """AC1: `- [x] <text> (used YYYY-MM-DD)` is a used seed with its date."""
    parsed = parse_seed_line("- [x] Toast thoughts (used 2026-11-01)")
    assert parsed == Seed(0, "Toast thoughts", True, date(2026, 11, 1), False)


def test_generated_marker_parses_in_either_order() -> None:
    """AC1: invented seeds carry (generated), alone or with a used date."""
    assert parse_seed_line("- [ ] Idea (generated)").generated is True
    canonical = parse_seed_line("- [x] Idea (generated) (used 2026-11-01)")
    swapped = parse_seed_line("- [x] Idea (used 2026-11-01) (generated)")
    assert canonical == swapped
    assert canonical.text == "Idea"
    assert canonical.generated and canonical.used


@pytest.mark.parametrize(
    "line",
    [
        "",
        "# a comment",
        "plain note",
        "-[ ] no space",
        "- [ ]",
        "- [ ]   ",
        "  - [ ] indented",
        "- [?] unknown mark",
    ],
)
def test_other_lines_are_not_seeds(line: str) -> None:
    """AC2: blank lines, comments and malformed lines are not seeds."""
    assert parse_seed_line(line) is None


def test_invalid_used_date_is_left_in_the_text() -> None:
    """A hand-typo in the date does not crash or lose the text."""
    parsed = parse_seed_line("- [x] Idea (used 2026-13-45)")
    assert parsed.text == "Idea (used 2026-13-45)"
    assert parsed.used_date is None


def test_format_seed_line_is_canonical() -> None:
    """AC1: the writer emits exactly the documented formats."""
    assert format_seed_line("A", None, False) == "- [ ] A"
    assert format_seed_line("A", TODAY, False) == "- [x] A (used 2026-11-17)"
    assert format_seed_line("A", None, True) == "- [ ] A (generated)"
    assert format_seed_line("A", TODAY, True) == (
        "- [x] A (generated) (used 2026-11-17)"
    )


def test_untouched_file_round_trips_byte_for_byte(tmp_path: Path) -> None:
    """AC2: load then save changes nothing, including blanks and comments."""
    path = _write(tmp_path / "seed.md", HAND_EDITED)
    SeedFile.load(path).save()
    assert path.read_bytes() == HAND_EDITED.encode("utf-8")


def test_only_the_marked_line_changes(tmp_path: Path) -> None:
    """AC2: marking one seed used rewrites that line and nothing else."""
    path = _write(tmp_path / "seed.md", HAND_EDITED)
    seed_file = SeedFile.load(path)
    target = seed_file.unused[0]
    seed_file.mark_used(target, TODAY)
    seed_file.save()
    before = HAND_EDITED.split("\n")
    after = path.read_text().split("\n")
    changed = [i for i, (a, b) in enumerate(zip(before, after, strict=True)) if a != b]
    assert changed == [target.line_index]
    assert after[target.line_index] == (
        "- [x] Rebranding my morning toast as a thought-leadership journey "
        "(used 2026-11-17)"
    )
    assert len(before) == len(after)


def test_mark_used_keeps_the_generated_marker(tmp_path: Path) -> None:
    """AC1/AC2: using an invented seed keeps (generated)."""
    path = _write(tmp_path / "seed.md", "- [ ] Invented (generated)\n")
    seed_file = SeedFile.load(path)
    seed_file.mark_used(seed_file.unused[0], TODAY)
    assert seed_file.text == "- [x] Invented (generated) (used 2026-11-17)\n"


def test_unused_lists_only_unused_seeds(tmp_path: Path) -> None:
    """The parser exposes used and unused seeds correctly from a messy file."""
    seed_file = SeedFile.load(_write(tmp_path / "seed.md", HAND_EDITED))
    assert len(seed_file.seeds) == 4
    assert [s.text for s in seed_file.unused] == [
        "Rebranding my morning toast as a thought-leadership journey",
        "An invented one",
    ]


def test_append_seed_adds_a_line_and_keeps_the_rest(tmp_path: Path) -> None:
    """AC1/AC2: appending an invented seed leaves existing lines untouched."""
    path = _write(tmp_path / "seed.md", HAND_EDITED)
    seed_file = SeedFile.load(path)
    seed_file.append_seed("Brand new idea", generated=True)
    seed_file.save()
    assert path.read_text() == HAND_EDITED + "- [ ] Brand new idea (generated)\n"


def test_append_to_file_without_trailing_newline(tmp_path: Path) -> None:
    """Appending never glues onto the last line."""
    seed_file = SeedFile.load(_write(tmp_path / "seed.md", "- [ ] one"))
    seed_file.append_seed("two")
    assert seed_file.text == "- [ ] one\n- [ ] two"


def test_append_to_empty_file(tmp_path: Path) -> None:
    """An empty file gains exactly one line."""
    seed_file = SeedFile.load(_write(tmp_path / "seed.md", ""))
    seed_file.append_seed("first")
    assert seed_file.text == "- [ ] first\n"


@pytest.mark.parametrize("bad", ["", "   ", "two\nlines", "cr\rhere"])
def test_append_rejects_non_single_line_text(tmp_path: Path, bad: str) -> None:
    """Seed text must be one non-empty line."""
    seed_file = SeedFile.load(_write(tmp_path / "seed.md", ""))
    with pytest.raises(ValueError, match="one non-empty line"):
        seed_file.append_seed(bad)


def test_windows_line_endings_survive(tmp_path: Path) -> None:
    """AC2: a file edited on Windows keeps its CRLF endings."""
    content = "# note\r\n- [ ] one\r\n- [ ] two\r\n"
    path = _write(tmp_path / "seed.md", content)
    seed_file = SeedFile.load(path)
    assert [s.text for s in seed_file.unused] == ["one", "two"]
    seed_file.mark_used(seed_file.unused[0], TODAY)
    seed_file.append_seed("three")
    seed_file.save()
    assert path.read_bytes() == (
        b"# note\r\n- [x] one (used 2026-11-17)\r\n- [ ] two\r\n- [ ] three\r\n"
    )


def test_non_ascii_text_survives(tmp_path: Path) -> None:
    """Unicode seeds round-trip unchanged."""
    content = "- [ ] Café ☕ thought leadership\n"
    path = _write(tmp_path / "seed.md", content)
    SeedFile.load(path).save()
    assert path.read_bytes() == content.encode("utf-8")


def test_stale_or_used_seed_cannot_be_marked(tmp_path: Path) -> None:
    """Marking guards against a changed line and against double-marking."""
    path = _write(tmp_path / "seed.md", "- [ ] one\n")
    seed_file = SeedFile.load(path)
    target = seed_file.unused[0]
    seed_file.mark_used(target, TODAY)
    with pytest.raises(ValueError, match="stale"):
        seed_file.mark_used(target, TODAY)
    used = seed_file.seeds[0]
    with pytest.raises(ValueError, match="already used"):
        seed_file.mark_used(used, TODAY)


def test_missing_file_is_created_empty(tmp_path: Path) -> None:
    """AC3: a missing file is created empty, including its directory."""
    path = tmp_path / "data" / "seed.md"
    seed_file = SeedFile.load(path)
    assert path.is_file()
    assert path.read_bytes() == b""
    assert seed_file.seeds == []


def test_default_path_is_the_data_volume() -> None:
    """AC3: the default location is /data/seed.md."""
    assert DEFAULT_SEED_PATH == Path("/data/seed.md")


def test_undecodable_file_is_a_clear_error(tmp_path: Path) -> None:
    """A non-UTF-8 file raises SeedFileError naming the file."""
    path = tmp_path / "seed.md"
    path.write_bytes(b"- [ ] \xff\xfe bad")
    with pytest.raises(SeedFileError, match="seed.md"):
        SeedFile.load(path)


def test_save_writes_a_temp_file_then_renames(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC4: the content is written to a temp file, then os.replace swaps it in."""
    path = _write(tmp_path / "seed.md", "- [ ] old\n")
    seed_file = SeedFile.load(path)
    seed_file.append_seed("new")
    calls: list[tuple[Path, Path, bytes]] = []
    real_replace = os.replace

    def spy(source: Path, target: Path) -> None:
        calls.append((Path(source), Path(target), Path(source).read_bytes()))
        real_replace(source, target)

    monkeypatch.setattr(seed_module.os, "replace", spy)
    seed_file.save()
    assert len(calls) == 1
    source, target, written = calls[0]
    assert target == path
    assert source != path
    assert source.parent == path.parent
    assert written == b"- [ ] old\n- [ ] new\n"
    assert not source.exists()


def test_failed_save_leaves_the_original_and_no_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC4: if the rename fails, the original is intact and no temp remains."""
    path = _write(tmp_path / "seed.md", "- [ ] old\n")
    seed_file = SeedFile.load(path)
    seed_file.append_seed("new")

    def failing_replace(_source: Path, _target: Path) -> None:
        raise OSError("disk exploded")

    monkeypatch.setattr(seed_module.os, "replace", failing_replace)
    with pytest.raises(OSError, match="disk exploded"):
        seed_file.save()
    assert path.read_bytes() == b"- [ ] old\n"
    assert [p.name for p in tmp_path.iterdir()] == ["seed.md"]


def test_save_preserves_file_permissions(tmp_path: Path) -> None:
    """The rename must not change who can read or edit the file by hand."""
    path = _write(tmp_path / "seed.md", "- [ ] old\n")
    path.chmod(0o664)
    seed_file = SeedFile.load(path)
    seed_file.append_seed("new")
    seed_file.save()
    assert stat.S_IMODE(path.stat().st_mode) == 0o664


def test_new_file_is_world_readable_not_private(tmp_path: Path) -> None:
    """A created file is 0644, not mkstemp's private 0600, so it is editable."""
    path = tmp_path / "seed.md"
    SeedFile.load(path)
    assert stat.S_IMODE(path.stat().st_mode) == seed_module.FILE_MODE


def test_save_recreates_a_missing_directory(tmp_path: Path) -> None:
    """save() works even if the data directory was removed after load()."""
    path = tmp_path / "data" / "seed.md"
    seed_file = SeedFile.load(path)
    path.unlink()
    path.parent.rmdir()
    seed_file.append_seed("after the directory vanished")
    seed_file.save()
    assert path.read_text() == "- [ ] after the directory vanished\n"


def test_seed_from_a_longer_file_is_stale_not_an_index_error(
    tmp_path: Path,
) -> None:
    """A seed whose line number is beyond this file raises ValueError."""
    long_file = SeedFile.load(_write(tmp_path / "a.md", "\n\n\n- [ ] far away\n"))
    short_file = SeedFile.load(_write(tmp_path / "b.md", "- [ ] near\n"))
    with pytest.raises(ValueError, match="stale"):
        short_file.mark_used(long_file.unused[0], TODAY)


def test_used_marker_on_an_unchecked_line_is_kept_as_text(tmp_path: Path) -> None:
    """A trailing (used ...) only counts as metadata on a checked line, so an
    unchecked seed that happens to end that way keeps its text."""
    parsed = parse_seed_line("- [ ] Retrospective (used 2024-01-01)")
    assert parsed == Seed(0, "Retrospective (used 2024-01-01)", False, None, False)
    path = _write(tmp_path / "seed.md", "- [ ] Retrospective (used 2024-01-01)\n")
    seed_file = SeedFile.load(path)
    seed_file.mark_used(seed_file.unused[0], TODAY)
    assert seed_file.text == (
        "- [x] Retrospective (used 2024-01-01) (used 2026-11-17)\n"
    )
    assert seed_file.seeds[0].text == "Retrospective (used 2024-01-01)"
    assert seed_file.seeds[0].used_date == TODAY
