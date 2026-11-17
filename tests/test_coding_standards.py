"""Checks that CODING_STANDARDS.md keeps its agreed structure (SA-11)."""

import re
from pathlib import Path

STANDARDS = Path(__file__).resolve().parent.parent / "CODING_STANDARDS.md"
SECTIONS = [
    "General",
    "Python",
    "Testing",
    "Security and secrets",
    "Error handling and alerting",
    "Docker",
    "Learned from PR-Agent",
]
RULE = re.compile(r"^- \[([A-Z]+-\d{2})\] \S.*$")
LEARNED = re.compile(r"^- \[[A-Z]+-\d{2}\] .+ \(from PR #\d+, \d{4}-\d{2}-\d{2}\)$")


def _text() -> str:
    return STANDARDS.read_text(encoding="utf-8")


def _section(name: str) -> list[str]:
    lines = _text().splitlines()
    start = lines.index(f"## {name}") + 1
    body: list[str] = []
    for line in lines[start:]:
        if line.startswith("## "):
            break
        body.append(line)
    return body


def test_file_has_all_sections() -> None:
    headings = [ln[3:] for ln in _text().splitlines() if ln.startswith("## ")]
    assert headings == SECTIONS


def test_rules_are_single_line_with_unique_ids() -> None:
    ids: list[str] = []
    for name in SECTIONS[:-1]:
        rules = [ln for ln in _section(name) if ln.startswith("- ")]
        assert rules, f"section {name} has no rules"
        for rule in rules:
            match = RULE.match(rule)
            assert match, f"malformed rule: {rule}"
            ids.append(match.group(1))
    assert len(ids) == len(set(ids))


def test_learned_rules_use_required_format() -> None:
    for line in _section("Learned from PR-Agent"):
        if line.startswith("- "):
            assert LEARNED.match(line), f"malformed learned rule: {line}"


def test_seed_rules_present() -> None:
    text = _text().lower()
    for phrase in [
        "type hints",
        "no bare `except:`",
        "no secrets in code, logs",
        "explicit timeout",
        "calls `alert()`",
        "acceptance criterion has at least one test",
        "no network calls in unit tests",
    ]:
        assert phrase in text, phrase


def test_claude_md_requires_self_review() -> None:
    claude = (STANDARDS.parent / "CLAUDE.md").read_text(encoding="utf-8")
    assert "CODING_STANDARDS.md" in claude
    assert "Self-review" in claude


def test_pr_template_has_self_review_checkbox() -> None:
    template = STANDARDS.parent / ".github" / "pull_request_template.md"
    assert "- [ ] Self-reviewed against CODING_STANDARDS.md" in template.read_text(
        encoding="utf-8"
    )
