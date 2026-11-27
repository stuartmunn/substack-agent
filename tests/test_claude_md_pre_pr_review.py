"""Checks CLAUDE.md and the PR template describe the pre-PR review (SA-57)."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLAUDE_MD = ROOT / "CLAUDE.md"
PR_TEMPLATE = ROOT / ".github" / "pull_request_template.md"


def _section(heading: str) -> str:
    text = CLAUDE_MD.read_text(encoding="utf-8")
    start = text.index(f"## {heading}")
    end = text.find("\n## ", start + 1)
    section = text[start:] if end == -1 else text[start:end]
    return " ".join(section.split())  # prose is hard-wrapped; ignore line breaks


def test_runs_code_review_medium_fix_before_pr() -> None:
    section = _section("Pre-PR review")
    assert "Before running `gh pr create`" in section
    assert "`/code-review medium --fix`" in section
    assert "Always pass `medium` explicitly" in section


def test_waits_for_findings_before_raising_pr() -> None:
    section = _section("Pre-PR review")
    assert "Wait for the findings before going on" in section
    assert "background" in section


def test_each_finding_has_one_of_three_outcomes() -> None:
    section = _section("Pre-PR review")
    for phrase in ["fix it", 'mark it "no change needed" with a', "skip it with a"]:
        assert phrase in section


def test_reruns_up_to_three_rounds_then_raises_and_flags() -> None:
    section = _section("Pre-PR review")
    assert "rerun the review" in section
    assert "after 3 rounds" in section
    assert "raise the PR anyway" in section
    assert "flag that finding" in section


def test_findings_recorded_in_pr_description() -> None:
    assert '"Pre-PR review" section to the PR description' in _section("Pre-PR review")
    assert "## Pre-PR review" in PR_TEMPLATE.read_text(encoding="utf-8")


def test_review_pointed_at_coding_standards() -> None:
    assert "`CODING_STANDARDS.md`" in _section("Pre-PR review")


def test_review_alias_is_distinguished_from_pr_agent_trigger() -> None:
    section = _section("Pre-PR review")
    assert "comment of exactly `/review` triggers PR-Agent" in section
    assert "alias for `/code-review`" in section


def test_git_workflow_points_at_pre_pr_review() -> None:
    assert 'Complete the "Pre-PR review"' in _section("Git workflow")
