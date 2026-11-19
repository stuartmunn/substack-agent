"""Tests for the draft-only Substack publisher (SA-41).

No network: the Substack API is a recording fake. The agent creates and updates
drafts only; these tests also fail if any code path can publish or post Notes.
"""

import logging
import re
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from substack.exceptions import SubstackAPIException

from humblebrag import publisher as publisher_module
from humblebrag.alerts import configure_alerts
from humblebrag.config import load_config
from humblebrag.publisher import DraftError, SubstackAuthError, SubstackPublisher

ENV = {
    "TELEGRAM_BOT_TOKEN": "123456:TESTTOKEN",
    "TELEGRAM_CHAT_ID": "1",
    "ANTHROPIC_API_KEY": "sk-ant-key",
    "CLAUDE_MODEL": "claude-test-model",
    "SUBSTACK_PUBLICATION_URL": "https://humblebrag.substack.com",
    "SUBSTACK_COOKIE": "s%3ASECRET-SID",
}
SOURCE_DIR = Path(__file__).resolve().parent.parent / "src" / "humblebrag"
FORBIDDEN = re.compile(
    r"publish_draft|schedule_draft|prepublish|/drafts/[^\"']*/publish"
    r"|publish\s*=\s*True|(post|create|publish|add)_note|/notes\b",
    re.IGNORECASE,
)


@pytest.fixture(autouse=True)
def alerts() -> Iterator[list[str]]:
    """Capture alert texts."""
    sent: list[str] = []

    async def sender(text: str) -> None:
        sent.append(text)

    configure_alerts(sender)
    yield sent
    configure_alerts(None)


class FakeApi:
    """Records every call; can be told to fail at a named step."""

    def __init__(self, **_kwargs: Any) -> None:
        """Start with a healthy account."""
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.fail_at: str | None = None
        self.create_reply: Any = {"draft": {"id": 4242}}

    def _step(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append((name, args, kwargs))
        if self.fail_at == name:
            raise SubstackAPIException(500, "boom")

    def create_draft_from_markdown(self, *args: Any, **kwargs: Any) -> Any:
        """Record and return a draft."""
        self._step("create", *args, **kwargs)
        return self.create_reply

    def update_draft_from_markdown(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """Record a body update."""
        self._step("update", *args, **kwargs)
        return {}

    def put_draft(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """Record a field update."""
        self._step("put", *args, **kwargs)
        return {}

    def __getattr__(self, name: str) -> Any:
        """Any other API call (publish, prepublish, schedule, notes) is a failure."""
        raise AssertionError(f"the agent must never call {name}")

    def names(self) -> list[str]:
        """The call names in order."""
        return [name for name, _a, _k in self.calls]


class Harness:
    """A publisher whose api factory hands out one shared FakeApi."""

    def __init__(self) -> None:
        """Build the publisher around a single fake."""
        self.api = FakeApi()
        self.publisher = SubstackPublisher(load_config(ENV), lambda **_k: self.api)


@pytest.fixture
def h() -> Harness:
    """A fresh harness."""
    return Harness()


# AC1: a draft is created from the title, subtitle and body


async def test_create_draft_sends_the_content_and_returns_the_id(h: Harness) -> None:
    """AC1: one create call with the content; the Substack ID comes back."""
    draft_id = await h.publisher.create_draft("Title", "Sub", "# Body\n\ntext")
    assert draft_id == "4242"
    assert h.api.names() == ["create"]
    _name, args, kwargs = h.api.calls[0]
    assert args == ("Title", "# Body\n\ntext")
    assert kwargs == {"subtitle": "Sub"}


@pytest.mark.parametrize("reply", [{}, None, "ok", {"draft": {}}, {"draft": None}])
async def test_a_reply_without_a_draft_id_is_an_error_not_a_guess(
    h: Harness, reply: Any
) -> None:
    """AC1: no ID means a typed error, never a made-up one."""
    h.api.create_reply = reply
    with pytest.raises(DraftError, match="ValueError"):
        await h.publisher.create_draft("T", "S", "b")


async def test_an_unreadable_create_reply_warns_the_draft_may_exist(
    h: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    """PR-Agent: Substack made the draft but no ID came back, so say so and log
    the reply, because a retry could otherwise duplicate it unseen."""
    h.api.create_reply = {"unexpected": "shape"}
    with caplog.at_level(logging.ERROR), pytest.raises(DraftError) as raised:
        await h.publisher.create_draft("T", "S", "b")
    assert "may exist on Substack" in str(raised.value)
    assert "Check Drafts" in str(raised.value)
    assert "unexpected" in caplog.text


# AC7: update an existing draft


async def test_update_draft_rewrites_body_subtitle_and_title(h: Harness) -> None:
    """AC7: body and subtitle via the library update, title via a field update."""
    await h.publisher.update_draft("777", "New title", "New sub", "# New")
    assert h.api.names() == ["update", "put"]
    assert h.api.calls[0][1] == ("777", "# New")
    assert h.api.calls[0][2]["subtitle"] == "New sub"
    assert h.api.calls[1] == ("put", ("777",), {"draft_title": "New title"})


async def test_update_never_creates_a_second_draft(h: Harness) -> None:
    """AC7: updating makes no create call."""
    await h.publisher.update_draft("777", "T", "S", "b")
    assert "create" not in h.api.names()


@pytest.mark.parametrize("step", ["update", "put"])
async def test_an_update_failure_is_typed(h: Harness, step: str) -> None:
    """AC7/ERR-18: a failure at either update call is a DraftError."""
    h.api.fail_at = step
    with pytest.raises(DraftError, match="Nothing was published"):
        await h.publisher.update_draft("777", "T", "S", "b")


# AC4 + AC5: never publishes, never posts a Note


def test_no_source_file_can_publish_prepublish_schedule_or_post_a_note() -> None:
    """AC4/AC5: a test fails if any source mentions a publish call or Notes."""
    offenders = []
    for path in sorted(SOURCE_DIR.glob("*.py")):
        for number, line in enumerate(path.read_text().splitlines(), start=1):
            if FORBIDDEN.search(line):
                offenders.append(f"{path.name}:{number}: {line.strip()}")
    assert offenders == []


def test_the_forbidden_pattern_catches_each_banned_form() -> None:
    """The scan above is meaningful: it matches every form it is meant to."""
    for banned in (
        "api.publish_draft(draft_id)",
        "api.prepublish_draft(x)",
        "api.schedule_draft(x, when)",
        "f'{base}/drafts/{id}/publish'",
        "create_draft_from_markdown(t, b, publish=True)",
        "api.post_note(text)",
        "url + '/notes'",
    ):
        assert FORBIDDEN.search(banned), banned
    assert not FORBIDDEN.search('EDITOR_PATH = "/publish/post/{draft_id}"')


def test_the_publisher_exposes_no_publish_method() -> None:
    """AC4: nothing on the publisher can be called to publish."""
    names = [name for name in dir(SubstackPublisher) if "publish" in name.lower()]
    assert names == []
    assert not hasattr(publisher_module, "SEND_EMAIL")


async def test_creating_and_updating_never_reach_a_forbidden_call(h: Harness) -> None:
    """AC4/AC5: the fake raises on any call outside create/update/put."""
    await h.publisher.create_draft("T", "S", "b")
    await h.publisher.update_draft("4242", "T", "S", "b")
    assert set(h.api.names()) <= {"create", "update", "put"}


# AC3: the editor link


def test_the_editor_link_opens_the_draft_in_the_editor(h: Harness) -> None:
    """AC3: publication URL + /publish/post/<id>."""
    assert (
        h.publisher.editor_url("4242")
        == "https://humblebrag.substack.com/publish/post/4242"
    )


def test_the_editor_link_ignores_a_trailing_slash() -> None:
    """AC3: a publication URL ending in a slash gives no double slash."""
    config = load_config({**ENV, "SUBSTACK_PUBLICATION_URL": "https://x.substack.com/"})
    link = SubstackPublisher(config).editor_url("9")
    assert link == "https://x.substack.com/publish/post/9"


# Session handling and error hygiene


async def test_the_session_is_checked_before_every_draft_call(h: Harness) -> None:
    """SA-31 AC2: each create and update re-checks the session first."""
    checks: list[int] = []

    def factory(**_kwargs: Any) -> FakeApi:
        checks.append(1)
        return h.api

    publisher = SubstackPublisher(load_config(ENV), factory)
    await publisher.create_draft("T", "S", "b")
    await publisher.update_draft("1", "T", "S", "b")
    assert len(checks) == 2


async def test_an_expired_session_stops_the_call_before_any_draft(
    h: Harness, alerts: list[str]
) -> None:
    """An expired session never reaches Substack's draft endpoint."""

    def factory(**_kwargs: Any) -> Any:
        raise SubstackAPIException(401, "{}")

    publisher = SubstackPublisher(load_config(ENV), factory)
    with pytest.raises(SubstackAuthError):
        await publisher.create_draft("T", "S", "b")
    assert h.api.calls == []
    assert len(alerts) == 1
    assert "session expired" in alerts[0]


async def test_a_failed_create_is_typed_and_never_retried(h: Harness) -> None:
    """ERR-03: one create call, then a DraftError (the caller alerts)."""
    h.api.fail_at = "create"
    with pytest.raises(DraftError, match="creating failed"):
        await h.publisher.create_draft("T", "S", "b")
    assert h.api.names() == ["create"]


async def test_a_non_substack_exception_is_still_typed(h: Harness) -> None:
    """ERR-18: any exception type from the library becomes a DraftError."""

    def broken(*_a: Any, **_k: Any) -> Any:
        raise KeyError("draft")

    h.api.create_draft_from_markdown = broken  # type: ignore[method-assign]
    with pytest.raises(DraftError, match="KeyError"):
        await h.publisher.create_draft("T", "S", "b")


async def test_two_draft_calls_never_overlap(h: Harness) -> None:
    """CON-01: concurrent draft calls are serialised, not interleaved."""
    import asyncio

    active = 0
    peak = 0
    guard = threading.Lock()
    original = h.api.create_draft_from_markdown

    def slow(*args: Any, **kwargs: Any) -> Any:
        nonlocal active, peak
        with guard:
            active += 1
            peak = max(peak, active)
        time.sleep(0.05)
        with guard:
            active -= 1
        return original(*args, **kwargs)

    h.api.create_draft_from_markdown = slow  # type: ignore[method-assign]
    await asyncio.gather(
        h.publisher.create_draft("A", "S", "b"), h.publisher.create_draft("B", "S", "b")
    )
    assert peak == 1


async def test_the_cookie_never_appears_in_errors_or_logs(
    h: Harness, alerts: list[str], caplog: pytest.LogCaptureFixture
) -> None:
    """SEC-01: a failed draft call leaks neither the cookie nor its value."""
    h.api.fail_at = "create"
    with caplog.at_level(logging.DEBUG), pytest.raises(DraftError) as raised:
        await h.publisher.create_draft("T", "S", "b")
    assert "SECRET-SID" not in str(raised.value) + " ".join(alerts) + caplog.text
