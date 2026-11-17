"""Tests for the persona prompt and its sample outputs (SA-22 acceptance criteria)."""

import re
from pathlib import Path

import pytest

from humblebrag.persona import DEFAULT_PERSONA_PATH, PersonaError, load_persona

SAMPLES_DIR = Path(__file__).parent / "fixtures" / "samples"
SAMPLE_COUNT = 5
EMOJI_BULLET = re.compile(r"^[^\w\s#*>\-`]", re.UNICODE)
HASHTAG_LINE = re.compile(r"^(#[A-Za-z]+\s?){3,6}$")
SENTENCE_BREAK = re.compile(r"[.!?]\s+\S")
FRONT_MATTER = re.compile(r"\A---\nseed: .+\n---\n")
MIN_WORDS = 100
MAX_WORDS = 260
REQUIRED_TOPICS = [
    "humble-bragging",
    "faux vulnerability",
    "what it taught me about leadership",
    "line break after every sentence",
    "emoji bullets",
    '"Agree?"',
    "hashtags",
]
REQUIRED_RULES = [
    "obvious satire",
    "no real named people or companies",
    "nothing defamatory",
    "no foul or offensive language",
    "english only",
]


def sample_paths() -> list[Path]:
    """Return the sample fixtures in order."""
    return sorted(SAMPLES_DIR.glob("*.md"))


def post_body(path: Path) -> str:
    """Return a sample without its seed front matter."""
    return FRONT_MATTER.sub("", path.read_text(encoding="utf-8"), count=1)


def test_prompt_lives_in_file_with_version() -> None:
    """AC1: the prompt is a file, with a version number."""
    persona = load_persona()
    assert DEFAULT_PERSONA_PATH.name == "persona.md"
    assert persona.version >= 1
    assert persona.text.startswith("# Sir Humblebrag III")


@pytest.mark.parametrize("topic", REQUIRED_TOPICS)
def test_prompt_covers_voice(topic: str) -> None:
    """AC2: every voice element is named in the prompt."""
    text = " ".join(load_persona().text.lower().split())
    assert topic.lower() in text


@pytest.mark.parametrize("rule", REQUIRED_RULES)
def test_prompt_states_safety_rules(rule: str) -> None:
    """AC3: every safety rule is stated in the prompt."""
    assert rule in " ".join(load_persona().text.lower().split())


def test_load_persona_rejects_missing_file(tmp_path: Path) -> None:
    """A missing persona file raises PersonaError."""
    with pytest.raises(PersonaError):
        load_persona(tmp_path / "nope.md")


def test_load_persona_rejects_missing_version(tmp_path: Path) -> None:
    """A persona file without version front matter raises PersonaError."""
    path = tmp_path / "persona.md"
    path.write_text("no front matter here", encoding="utf-8")
    with pytest.raises(PersonaError):
        load_persona(path)


def test_load_persona_rejects_undecodable_file(tmp_path: Path) -> None:
    """A non-UTF-8 persona file raises PersonaError, not UnicodeDecodeError."""
    path = tmp_path / "persona.md"
    path.write_bytes(b"\xff\xfe\x00")
    with pytest.raises(PersonaError):
        load_persona(path)


def test_five_samples_exist() -> None:
    """AC4: five samples are kept in tests/fixtures."""
    assert len(sample_paths()) == SAMPLE_COUNT


@pytest.mark.parametrize("path", sample_paths(), ids=lambda p: p.stem)
def test_sample_follows_format(path: Path) -> None:
    """AC2: samples have one sentence per line, emoji bullets and a closing."""
    text = post_body(path)
    lines = [line for line in text.splitlines() if line.strip()]
    assert lines[0].startswith("# ")
    body_lines = lines[1:]
    assert not any(
        SENTENCE_BREAK.search(line) for line in body_lines if line[0].isalpha()
    )
    assert sum(bool(EMOJI_BULLET.match(line)) for line in body_lines) >= 3
    ends_agree = lines[-1] == "Agree?"
    ends_tags = bool(HASHTAG_LINE.match(lines[-1]))
    assert ends_agree != ends_tags
    assert MIN_WORDS <= len(text.split()) <= MAX_WORDS
    assert "leadership" in text.lower()
