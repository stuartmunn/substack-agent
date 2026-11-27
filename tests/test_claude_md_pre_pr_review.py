"""Checks CLAUDE.md and the PR template describe the pre-PR review (SA-57)."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLAUDE_MD = ROOT / "CLAUDE.md"
PR_TEMPLATE = ROOT / ".github" / "pull_request_template.md"
HEADING = "Pre-PR review"


def _section(heading: str) -> str:
    """Return a CLAUDE.md section with line wrapping collapsed."""
    text = CLAUDE_MD.read_text(encoding="utf-8")
    start = text.index(f"## {heading}")
    end = text.find("\n## ", start + 1)
    section = text[start:] if end == -1 else text[start:end]
    return " ".join(section.split())  # prose is hard-wrapped; ignore line breaks


def test_runs_code_review_medium_fix_before_pr() -> None:
    """AC1: /code-review medium --fix runs before gh pr create, level explicit."""
    section = _section(HEADING)
    assert "Before running `gh pr create`" in section
    assert "`/code-review medium --fix" in section
    assert "Always pass `medium` explicitly" in section


def test_waits_for_findings_before_raising_pr() -> None:
    """AC2: the agent waits for the background review to report."""
    section = _section(HEADING)
    assert "Wait for the findings before going on" in section
    assert "background" in section


def test_each_finding_has_one_of_three_outcomes() -> None:
    """AC3: fix, no change needed, or skip, each with a reason."""
    section = _section(HEADING)
    for phrase in ["fix it", 'mark it "no change needed" with a', "skip it with a"]:
        assert phrase in section


def test_reruns_up_to_three_rounds_then_raises_and_flags() -> None:
    """AC4/AC5: rerun after fixing, cap at 3 rounds, raise anyway and flag."""
    section = _section(HEADING)
    assert "rerun the review" in section
    assert "after 3 rounds" in section
    assert "raise the PR anyway" in section
    assert "flag that finding" in section


def test_findings_recorded_in_pr_description() -> None:
    """AC3/AC8: the PR description and template carry a Pre-PR review section."""
    assert f'"{HEADING}" section to the PR description' in _section(HEADING)
    assert f"## {HEADING}" in PR_TEMPLATE.read_text(encoding="utf-8")


def test_commits_before_each_round() -> None:
    """Risk: --fix has no checkpoints, so work is committed before each round."""
    section = _section(HEADING)
    assert "Commit all story work" in section
    assert "before each round" in section


def test_review_is_told_to_follow_coding_standards() -> None:
    """AC6: the invocation points /code-review at CODING_STANDARDS.md."""
    assert (
        "`/code-review medium --fix Follow CODING_STANDARDS.md as well as CLAUDE.md.`"
        in _section(HEADING)
    )


def test_review_alias_is_distinguished_from_pr_agent_trigger() -> None:
    """AC7: only the GitHub /review comment triggers PR-Agent."""
    section = _section(HEADING)
    assert "comment of exactly `/review` triggers PR-Agent" in section
    assert "alias for `/code-review`" in section


def test_git_workflow_points_at_pre_pr_review() -> None:
    """AC1: the Git workflow checklist links to the Pre-PR review section."""
    assert f'Complete the "{HEADING}"' in _section("Git workflow")
