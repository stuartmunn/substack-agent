"""Tests for the tone and safety guard (SA-24 acceptance criteria)."""

import json
from collections.abc import Iterator
from typing import Any

import pytest

from humblebrag.alerts import configure_alerts
from humblebrag.claude_client import ClaudeAPIError
from humblebrag.generator import GeneratedPost
from humblebrag.guard import (
    ALERT_EVENT,
    CHECK_CLASSIFIER,
    CHECK_WORDLIST,
    CLASSIFIER_SYSTEM,
    MAX_GUARD_REGENERATIONS,
    GuardError,
    GuardRejectedError,
    GuardResult,
    SafetyGuard,
    check_wordlist,
    generate_checked,
)

CLEAN = GeneratedPost("A Humble Brew", "Quietly brilliant", "I spilled my coffee.")
SWEARING = GeneratedPost("A Humble Brew", "Quietly brilliant", "What a load of shit.")


@pytest.fixture(autouse=True)
def alerts() -> Iterator[list[str]]:
    """Capture alert texts for the test, and unset the sender afterwards."""
    sent: list[str] = []

    async def sender(text: str) -> None:
        sent.append(text)

    configure_alerts(sender)
    yield sent
    configure_alerts(None)


class StubClient:
    """Replays canned classifier replies and records the calls."""

    def __init__(self, replies: list[str | Exception]) -> None:
        """Replay ``replies`` in order."""
        self._replies = list(replies)
        self.calls: list[dict[str, Any]] = []

    async def complete(self, system: str, prompt: str, max_tokens: int) -> str:
        """Record the call; return or raise the next canned reply."""
        self.calls.append({"system": system, "prompt": prompt})
        reply = self._replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


class StubGenerator:
    """Replays canned posts and records the steers it was given."""

    def __init__(self, posts: list[GeneratedPost]) -> None:
        """Replay ``posts`` in order."""
        self._posts = list(posts)
        self.steers: list[str | None] = []

    async def generate(self, seed: str, steer: str | None = None) -> GeneratedPost:
        """Record the steer and return the next post."""
        self.steers.append(steer)
        return self._posts.pop(0)


def _verdict(verdict: str = "pass", reason: str = "Clearly satire.") -> str:
    """A classifier reply."""
    return json.dumps({"verdict": verdict, "reason": reason})


def _guard(client: StubClient) -> SafetyGuard:
    """A guard on a stub client."""
    return SafetyGuard(client)  # type: ignore[arg-type]


async def _run(
    posts: list[GeneratedPost], replies: list[str | Exception], steer: str | None = None
) -> tuple[Any, StubGenerator, StubClient]:
    """Run generate_checked on stubs and return the (outcome, generator, client)."""
    generator, client = StubGenerator(posts), StubClient(replies)
    outcome = await generate_checked(generator, _guard(client), "seed", steer)  # type: ignore[arg-type]
    return outcome, generator, client


@pytest.mark.parametrize("field", ["title", "subtitle", "body_markdown"])
def test_wordlist_checks_title_subtitle_and_body(field: str) -> None:
    """AC1: a wordlisted word in any one field fails the check."""
    fields = {"title": "Fine", "subtitle": "Fine", "body_markdown": "Fine"}
    fields[field] = "Well, Shit happens!"
    result = check_wordlist(GeneratedPost(**fields))
    assert not result.passed
    assert result.check == CHECK_WORDLIST
    assert field.removesuffix("_markdown") in result.reason


@pytest.mark.parametrize(
    "text", ["class assessment", "the cockpit", "Scunthorpe", "a classic passage"]
)
def test_wordlist_ignores_words_that_merely_contain_a_listed_word(text: str) -> None:
    """AC1 risk: whole-word matching avoids false positives."""
    assert check_wordlist(GeneratedPost(text, text, text)).passed


async def test_wordlist_failure_skips_the_classifier() -> None:
    """AC1: a wordlist fail is final, so no Claude call is made."""
    client = StubClient([])
    result = await _guard(client).check(SWEARING)
    assert not result.passed
    assert client.calls == []


async def test_classifier_receives_the_draft_and_the_three_criteria() -> None:
    """AC2: a second Claude call reviews the draft for names, defamation, satire."""
    client = StubClient([_verdict()])
    await _guard(client).check(CLEAN)
    call = client.calls[0]
    assert CLEAN.body_markdown in call["prompt"]
    assert call["system"] == CLASSIFIER_SYSTEM
    for criterion in ("real person", "defamation", "satire"):
        assert criterion in CLASSIFIER_SYSTEM


@pytest.mark.parametrize("verdict", ["pass", "fail"])
async def test_classifier_returns_pass_or_fail_with_a_reason(verdict: str) -> None:
    """AC2: the verdict and its reason come back in the result."""
    client = StubClient([_verdict(verdict, "Because.")])
    result = await _guard(client).check(CLEAN)
    assert result == GuardResult(verdict == "pass", "Because.", CHECK_CLASSIFIER)


@pytest.mark.parametrize(
    "reply",
    [
        "not json",
        json.dumps({"verdict": "maybe", "reason": "x"}),
        json.dumps({"verdict": "pass"}),
        json.dumps({"verdict": "pass", "reason": "  "}),
    ],
)
async def test_unusable_classifier_reply_fails_closed_and_alerts(
    reply: str, alerts: list[str]
) -> None:
    """AC2/ERR-01: an unusable verdict is an error, never an implicit pass."""
    with pytest.raises(GuardError):
        await _run([CLEAN], [reply])
    assert len(alerts) == 1
    assert ALERT_EVENT in alerts[0]


async def test_passing_draft_is_returned_with_its_result() -> None:
    """AC3: a pass needs no regeneration."""
    outcome, generator, _ = await _run([CLEAN], [_verdict()])
    assert outcome.post == CLEAN
    assert outcome.guard.passed
    assert len(generator.steers) == 1


async def test_failing_draft_is_regenerated_with_the_reason_as_steer() -> None:
    """AC3: a fail regenerates, keeping Stuart's steer and adding the reason."""
    outcome, generator, _ = await _run(
        [CLEAN, CLEAN], [_verdict("fail", "Names a brand."), _verdict()], "be kind"
    )
    assert outcome.guard.passed
    assert generator.steers[0] == "be kind"
    assert "be kind" in generator.steers[1]
    assert "Names a brand." in generator.steers[1]


async def test_wordlist_failure_also_regenerates() -> None:
    """AC3: regeneration covers the wordlist check as well."""
    outcome, generator, _ = await _run([SWEARING, CLEAN], [_verdict()])
    assert outcome.post == CLEAN
    assert len(generator.steers) == 2


async def test_up_to_two_regenerations_then_alert_with_the_reason(
    alerts: list[str],
) -> None:
    """AC3: three drafts in all, then an alert carrying the last reason."""
    failing = [_verdict("fail", f"reason {n}") for n in (1, 2, 3)]
    generator, client = StubGenerator([CLEAN] * 3), StubClient(failing)
    with pytest.raises(GuardRejectedError) as raised:
        await generate_checked(generator, _guard(client), "seed")  # type: ignore[arg-type]
    assert MAX_GUARD_REGENERATIONS == 2
    assert len(generator.steers) == MAX_GUARD_REGENERATIONS + 1
    assert raised.value.reason == "reason 3"
    assert len(alerts) == 1
    assert "reason 3" in alerts[0]


async def test_claude_errors_propagate_without_a_guard_alert(
    alerts: list[str],
) -> None:
    """A failed classifier call is a ClaudeError for the pipeline to alert on."""
    with pytest.raises(ClaudeAPIError):
        await _run([CLEAN], [ClaudeAPIError("boom", status_code=500)])
    assert alerts == []


def test_pass_result_shows_in_the_preview_line() -> None:
    """AC4: a passing result renders a preview line with its reason."""
    line = GuardResult(True, "Clearly satire.", CHECK_CLASSIFIER).preview_line()
    assert "Guard passed" in line
    assert "Clearly satire." in line


async def test_checked_post_carries_the_result_for_the_preview() -> None:
    """AC4: generate_checked hands the preview its guard line."""
    outcome, _, _ = await _run([CLEAN], [_verdict(reason="Obvious parody.")])
    assert "Obvious parody." in outcome.guard.preview_line()
