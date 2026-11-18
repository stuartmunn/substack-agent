"""Tests for publishing with subscriber email off (SA-32 acceptance criteria).

No network and nothing is ever published: the Substack API is a recording fake.
"""

import ast
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from substack.exceptions import SubstackAPIException

from humblebrag.alerts import configure_alerts
from humblebrag.config import load_config
from humblebrag.publisher import (
    SEND_EMAIL,
    PublishError,
    SubstackAuthError,
    SubstackPublisher,
)

URL = "https://humblebrag.substack.com/p/my-post"
ENV = {
    "TELEGRAM_BOT_TOKEN": "123456:TESTTOKEN",
    "TELEGRAM_CHAT_ID": "1",
    "ANTHROPIC_API_KEY": "sk-ant-key",
    "CLAUDE_MODEL": "claude-test-model",
    "SUBSTACK_PUBLICATION_URL": "https://humblebrag.substack.com",
    "SUBSTACK_COOKIE": "s%3ASECRET-SID",
}
SOURCE = Path(__file__).resolve().parent.parent / "src/humblebrag/publisher.py"


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
        self.publish_reply: Any = {"canonical_url": URL, "slug": "my-post"}

    def _step(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append((name, args, kwargs))
        if self.fail_at == name:
            raise SubstackAPIException(500, "boom")

    def create_draft_from_markdown(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """Record and return a draft."""
        self._step("create", *args, **kwargs)
        return {"draft": {"id": 4242}}

    def prepublish_draft(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """Record the prepublish check."""
        self._step("prepublish", *args, **kwargs)
        return {}

    def publish_draft(self, *args: Any, **kwargs: Any) -> Any:
        """Record the publish call."""
        self._step("publish", *args, **kwargs)
        return self.publish_reply

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


# AC1: Markdown -> draft -> prepublish -> publish(send=False)


async def test_publish_goes_markdown_draft_prepublish_publish_in_order(
    h: Harness,
) -> None:
    """AC1: the steps run in that order, and the content reaches the draft."""
    await h.publisher.publish("Title", "Sub", "# Body\n\ntext")
    assert h.api.names() == ["create", "prepublish", "publish"]
    _name, args, kwargs = h.api.calls[0]
    assert args == ("Title", "# Body\n\ntext")
    assert kwargs == {"subtitle": "Sub"}
    assert h.api.calls[1][1] == ("4242",)
    assert h.api.calls[2][1] == ("4242",)


async def test_the_session_is_checked_before_every_publish(h: Harness) -> None:
    """SA-31 AC2: each publish re-checks the session first."""
    checks: list[int] = []

    def factory(**_kwargs: Any) -> FakeApi:
        checks.append(1)
        return h.api

    publisher = SubstackPublisher(load_config(ENV), factory)
    await publisher.publish("T", "S", "b")
    await publisher.publish("T", "S", "b")
    assert len(checks) == 2


async def test_an_expired_session_stops_the_publish_before_any_draft(
    h: Harness, alerts: list[str]
) -> None:
    """An expired session never reaches Substack's draft endpoint."""

    def factory(**_kwargs: Any) -> Any:
        raise SubstackAPIException(401, "{}")

    publisher = SubstackPublisher(load_config(ENV), factory)
    with pytest.raises(SubstackAuthError):
        await publisher.publish("T", "S", "b")
    assert h.api.calls == []
    assert len(alerts) == 1
    assert "session expired" in alerts[0]


# AC2: send=False is always passed


async def test_publish_draft_is_always_called_with_send_false(h: Harness) -> None:
    """AC2: send=False is passed, by keyword, on every publish."""
    for _ in range(3):
        await h.publisher.publish("T", "S", "b")
    publishes = [c for c in h.api.calls if c[0] == "publish"]
    assert len(publishes) == 3
    for _name, _args, kwargs in publishes:
        assert kwargs == {"send": False}


def test_the_send_switch_is_false_and_never_a_parameter() -> None:
    """AC2: SEND_EMAIL is False and publish() cannot be told otherwise."""
    assert SEND_EMAIL is False
    tree = ast.parse(SOURCE.read_text())
    publish = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "publish"
    )
    assert [a.arg for a in publish.args.args] == [
        "self",
        "title",
        "subtitle",
        "body_markdown",
    ]


def test_source_never_enables_send_or_publishes_through_the_shortcut() -> None:
    """AC2: no send=True, and no create_draft_from_markdown(publish=...) anywhere."""
    tree = ast.parse(SOURCE.read_text())
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            if keyword.arg == "send":
                assert isinstance(keyword.value, ast.Name)
                assert keyword.value.id == "SEND_EMAIL"
            assert keyword.arg != "publish"
    assert "send=True" not in SOURCE.read_text().replace(" ", "")


# AC3: success returns the public URL


async def test_success_returns_the_public_url(h: Harness) -> None:
    """AC3: the canonical URL from Substack is returned."""
    assert await h.publisher.publish("T", "S", "b") == URL


async def test_url_falls_back_to_the_slug_on_the_configured_publication(
    h: Harness,
) -> None:
    """AC3: with no canonical URL, build it from the slug."""
    h.api.publish_reply = {"slug": "my-post"}
    assert await h.publisher.publish("T", "S", "b") == URL


@pytest.mark.parametrize(
    "reply", [{}, None, "ok", {"canonical_url": "http://insecure/p/x"}]
)
async def test_a_reply_without_a_usable_url_is_an_error_not_a_guess(
    h: Harness, reply: Any, alerts: list[str]
) -> None:
    """AC3/AC4: no URL means a typed error that warns the post may be live."""
    h.api.publish_reply = reply
    with pytest.raises(PublishError) as raised:
        await h.publisher.publish("T", "S", "b")
    assert raised.value.maybe_published is True
    assert "may be live" in str(raised.value)
    assert len(alerts) == 1


# AC4: typed error, alert, no blind retry


@pytest.mark.parametrize("step", ["create", "prepublish", "publish"])
async def test_any_api_error_raises_a_typed_error_and_alerts_once(
    h: Harness, alerts: list[str], step: str
) -> None:
    """AC4: a failure at any step is a PublishError and alerts Stuart."""
    h.api.fail_at = step
    with pytest.raises(PublishError):
        await h.publisher.publish("T", "S", "b")
    assert len(alerts) == 1
    assert "Substack publish failed" in alerts[0]


@pytest.mark.parametrize("step", ["create", "prepublish", "publish"])
async def test_a_failed_publish_is_never_retried(h: Harness, step: str) -> None:
    """AC4: each API call is made at most once (no blind retry, no double post)."""
    h.api.fail_at = step
    with pytest.raises(PublishError):
        await h.publisher.publish("T", "S", "b")
    names = h.api.names()
    assert len(names) == len(set(names))
    assert names.count("publish") <= 1


async def test_a_failure_in_prepublish_means_nothing_was_published(
    h: Harness,
) -> None:
    """AC4: failing before the publish call says nothing went live."""
    h.api.fail_at = "prepublish"
    with pytest.raises(PublishError) as raised:
        await h.publisher.publish("T", "S", "b")
    assert raised.value.maybe_published is False
    assert "Nothing was published" in str(raised.value)
    assert "publish" not in h.api.names()


async def test_a_failure_in_the_publish_call_warns_it_may_be_live(
    h: Harness,
) -> None:
    """AC4: a failed publish call may still have gone through."""
    h.api.fail_at = "publish"
    with pytest.raises(PublishError) as raised:
        await h.publisher.publish("T", "S", "b")
    assert raised.value.maybe_published is True
    assert "Check Substack before retrying" in str(raised.value)


async def test_a_non_substack_exception_is_still_typed(h: Harness) -> None:
    """ERR-18: any exception type from the library becomes a PublishError."""

    def broken(*_a: Any, **_k: Any) -> Any:
        raise KeyError("draft")

    h.api.create_draft_from_markdown = broken  # type: ignore[method-assign]
    with pytest.raises(PublishError, match="KeyError"):
        await h.publisher.publish("T", "S", "b")


async def test_two_publishes_never_overlap(h: Harness) -> None:
    """CON-01: concurrent publishes are serialised, not interleaved."""
    import asyncio
    import threading
    import time

    active = 0
    peak = 0
    guard = threading.Lock()
    original = h.api.create_draft_from_markdown

    def slow(*args: Any, **kwargs: Any) -> dict[str, Any]:
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
        h.publisher.publish("A", "S", "b"), h.publisher.publish("B", "S", "b")
    )
    assert peak == 1


# AC5: orphaned drafts are logged with their ID


@pytest.mark.parametrize("step", ["prepublish", "publish"])
async def test_an_orphaned_draft_is_logged_and_alerted_with_its_id(
    h: Harness,
    alerts: list[str],
    caplog: pytest.LogCaptureFixture,
    step: str,
) -> None:
    """AC5: the Substack draft ID left behind is in the log and the alert."""
    h.api.fail_at = step
    with caplog.at_level(logging.ERROR), pytest.raises(PublishError) as raised:
        await h.publisher.publish("T", "S", "b")
    assert raised.value.draft_id == "4242"
    assert "4242" in caplog.text
    assert "4242" in alerts[0]


async def test_a_failure_before_any_draft_exists_logs_no_orphan(
    h: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    """AC5: if no draft was created there is nothing to report as orphaned."""
    h.api.fail_at = "create"
    with caplog.at_level(logging.ERROR), pytest.raises(PublishError) as raised:
        await h.publisher.publish("T", "S", "b")
    assert raised.value.draft_id is None
    assert "Orphaned" not in caplog.text
    assert "No draft was created" in str(raised.value)


async def test_the_cookie_never_appears_in_alerts_or_logs(
    h: Harness, alerts: list[str], caplog: pytest.LogCaptureFixture
) -> None:
    """SEC-01: a failed publish leaks neither the cookie nor its value."""
    h.api.fail_at = "publish"
    with caplog.at_level(logging.DEBUG), pytest.raises(PublishError):
        await h.publisher.publish("T", "S", "b")
    assert "SECRET-SID" not in " ".join(alerts) + caplog.text
