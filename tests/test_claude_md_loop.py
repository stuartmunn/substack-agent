"""Checks CLAUDE.md describes the PR-Agent review loop as agreed in SA-12."""

from pathlib import Path

CLAUDE_MD = Path(__file__).resolve().parent.parent / "CLAUDE.md"


def _section(heading: str) -> str:
    text = CLAUDE_MD.read_text(encoding="utf-8")
    start = text.index(f"## {heading}")
    end = text.find("\n## ", start + 1)
    return text[start:] if end == -1 else text[start:end]


def test_waits_then_polls_comments() -> None:
    loop = _section("PR-Agent review loop")
    assert "wait 3–5 minutes" in loop
    assert "gh pr view <n> --comments" in loop
    assert "gh api" in loop


def test_timeout_posts_note_and_alerts() -> None:
    loop = _section("PR-Agent review loop")
    assert "15 minutes, post a note on the PR, alert Stuart" in loop


def test_each_finding_gets_fix_mitigation_or_reply() -> None:
    loop = _section("PR-Agent review loop")
    for phrase in ["Fix it", "Mitigate it", "doesn't apply: reply on the comment"]:
        assert phrase in loop


def test_review_comment_is_exact() -> None:
    assert "comment exactly `/review` (nothing else in the comment)" in _section(
        "PR-Agent review loop"
    )


def test_valid_findings_become_rules_in_same_pr() -> None:
    loop = _section("PR-Agent review loop")
    assert "`CODING_STANDARDS.md`" in loop
    assert "- [ID] rule (from PR #n, YYYY-MM-DD)" in loop
    assert "unless an existing rule covers it" in loop


def test_five_round_cap_stops_without_merging() -> None:
    loop = _section("PR-Agent review loop")
    assert "After 5 review rounds without a clean review" in loop
    assert "do not merge" in loop


def test_clean_judgement_reasoning_goes_on_pr() -> None:
    assert "write the reasoning on the PR for audit" in _section("PR-Agent review loop")


def test_merge_requires_clean_review_and_ci_and_squash_deletes_branch() -> None:
    merge = _section("Merge")
    assert "CI green" in merge
    assert "PR-Agent's latest review clean" in merge
    assert "gh pr merge --squash --delete-branch" in merge
