"""Tests that docs/runbook.md covers what SA-37 requires."""

import re
from pathlib import Path

import pytest

from humblebrag.publisher import RUNBOOK_URL

RUNBOOK = Path(__file__).resolve().parent.parent / "docs" / "runbook.md"
REQUIRED_SECTIONS = [
    "First deploy",
    "Update the image",
    "Refresh the Substack cookie",
    "Edit `seed.md` by hand",
    "Read the logs",
    "Recover an orphaned Substack draft",
]
MIN_SECTION_LINES = 5


def _sections() -> dict[str, str]:
    """Map each second-level heading to the text under it."""
    parts = re.split(r"^## (.+)$", RUNBOOK.read_text(), flags=re.MULTILINE)
    return dict(zip(parts[1::2], parts[2::2], strict=True))


def _anchor(heading: str) -> str:
    """GitHub's anchor for a heading: lowercase, punctuation dropped, spaces to dashes."""
    return re.sub(r"[^a-z0-9 -]", "", heading.lower()).replace(" ", "-")


@pytest.mark.parametrize("heading", REQUIRED_SECTIONS)
def test_runbook_has_every_required_section_with_content(heading: str) -> None:
    """AC1: each required topic has its own, non-trivial section."""
    sections = _sections()
    assert heading in sections
    lines = [line for line in sections[heading].splitlines() if line.strip()]
    assert len(lines) >= MIN_SECTION_LINES


def test_the_session_expiry_alert_link_lands_on_the_cookie_section() -> None:
    """The alert's runbook link (SA-31) points at a heading that exists."""
    fragment = RUNBOOK_URL.split("#", 1)[1]
    anchors = {_anchor(heading) for heading in _sections()}
    assert fragment in anchors


def test_the_runbook_names_no_secret_values() -> None:
    """SEC-01: only placeholders, never a realistic token or key."""
    text = RUNBOOK.read_text()
    assert not re.search(r"sk-ant-\w{10,}|\d{6,}:[\w-]{20,}", text)


def test_the_runbook_documents_the_deploy_script_and_smoke_test() -> None:
    """SA-39: the one-command deploy and the smoke test are both documented."""
    text = RUNBOOK.read_text()
    assert "./scripts/deploy.sh" in text
    assert "docker compose up -d --build" in text
    assert "python -m humblebrag.smoke" in text
    assert "Sir Humblebrag reporting for duty" in text
    assert "Smoke test" in _sections()


def test_the_runbook_and_the_script_agree_on_the_smoke_command() -> None:
    """The command in the runbook is the one deploy.sh runs."""
    script = (RUNBOOK.parent.parent / "scripts" / "deploy.sh").read_text()
    assert "python -m humblebrag.smoke" in script


def test_the_runbook_says_to_run_the_smoke_test_after_a_model_change() -> None:
    """SA-59 AC5: a CLAUDE_MODEL change is followed by the smoke check."""
    section = _sections()["Smoke test"]
    assert "after any `CLAUDE_MODEL` change" in section
    assert "one real, minimal Claude call per flow" in section
