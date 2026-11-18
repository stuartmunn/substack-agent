"""Tests for the Approve handler (SA-34 acceptance criteria).

Real DraftStore and SeedQueue in a temp dir; the Substack publisher is faked
(or, once, the real publisher over a fake API). Nothing is ever published.
"""

import asyncio
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from humblebrag.alerts import configure_alerts
from humblebrag.approval import (
    FAILED_TEXT,
    IN_FLIGHT_TEXT,
    MAY_BE_LIVE_NOTE,
    ApproveService,
    install_approve,
)
from humblebrag.buttons import ACTIONS_KEY, ALREADY_HANDLED_TEXT
from humblebrag.config import load_config
from humblebrag.drafts import STATUS_PENDING, STATUS_PUBLISHED, DraftStore
from humblebrag.preview import ACTION_APPROVE
from humblebrag.publisher import PublishError, SubstackError, SubstackPublisher
from humblebrag.seed_queue import SeedQueue

CHAT = 12345
TODAY = date(2026, 11, 18)
URL = "https://humblebrag.substack.com/p/my-post"
SEEDS = "- [ ] First seed\n- [ ] Second seed\n"
ENV = {
    "TELEGRAM_BOT_TOKEN": "123456:TESTTOKEN",
    "TELEGRAM_CHAT_ID": "1",
    "ANTHROPIC_API_KEY": "sk-ant-key",
    "CLAUDE_MODEL": "claude-test-model",
    "SUBSTACK_PUBLICATION_URL": "https://humblebrag.substack.com",
    "SUBSTACK_COOKIE": "s%3ASECRET-SID",
}


@pytest.fixture(autouse=True)
def alerts() -> Iterator[list[str]]:
    """Capture alert texts."""
    sent: list[str] = []

    async def sender(text: str) -> None:
        sent.append(text)

    configure_alerts(sender)
    yield sent
    configure_alerts(None)


class FakeBot:
    """Records messages and button removals."""

    def __init__(self) -> None:
        """Start empty."""
        self.sent: list[str] = []
        self.edits: list[dict[str, Any]] = []
        self.edit_error: Exception | None = None

    async def send_message(self, **kwargs: Any) -> None:
        """Record a sent message's text."""
        self.sent.append(kwargs["text"])

    async def edit_message_reply_markup(self, **kwargs: Any) -> None:
        """Record a button removal, or raise the configured error."""
        if self.edit_error is not None:
            raise self.edit_error
        self.edits.append(kwargs)


class FakePublisher:
    """Stands in for SubstackPublisher.publish."""

    def __init__(self) -> None:
        """Succeed by default."""
        self.calls: list[tuple[str, str, str]] = []
        self.error: Exception | None = None
        self.gate: asyncio.Event | None = None

    async def publish(self, title: str, subtitle: str, body: str) -> str:
        """Record the call, optionally wait on a gate, then succeed or fail."""
        self.calls.append((title, subtitle, body))
        if self.gate is not None:
            await self.gate.wait()
        if self.error is not None:
            raise self.error
        return URL


class Harness:
    """Real store and queue, fake publisher and bot."""

    def __init__(self, tmp_path: Path) -> None:
        """One pending draft that holds the first seed."""
        self.seed_path = tmp_path / "seed.md"
        self.seed_path.write_text(SEEDS)
        self.queue = SeedQueue(self.seed_path, lambda: TODAY)
        self.store = DraftStore(tmp_path / "d.db")
        self.bot = FakeBot()
        self.publisher = FakePublisher()
        self.service = ApproveService(
            self.store,
            self.queue,
            self.publisher,  # type: ignore[arg-type]
        )
        seed = self.queue.pick("1")
        assert seed is not None
        self.draft = self.store.create(seed.text, "The Title", "The Sub", "# Body")

    async def tap(self, message_id: int = 40) -> None:
        """Tap Approve on the draft's message."""
        update = SimpleNamespace(
            effective_chat=SimpleNamespace(id=CHAT),
            callback_query=SimpleNamespace(
                message=SimpleNamespace(message_id=message_id)
            ),
        )
        await self.service.on_approve_tap(
            update,  # type: ignore[arg-type]
            SimpleNamespace(bot=self.bot),  # type: ignore[arg-type]
            self.draft,
        )

    def status(self) -> str:
        """The draft's stored status."""
        return self.store.get(self.draft.id).status


@pytest.fixture
def h(tmp_path: Path) -> Harness:
    """A fresh harness."""
    return Harness(tmp_path)


# AC1: publishes via SA-32 with subscriber email off


async def test_approve_publishes_the_drafts_content(h: Harness) -> None:
    """AC1: the draft's title, subtitle and body go to the publisher."""
    await h.tap()
    assert h.publisher.calls == [("The Title", "The Sub", "# Body")]


async def test_approve_uses_the_real_publisher_with_send_false(
    h: Harness,
) -> None:
    """AC1: through the real SubstackPublisher, publish_draft gets send=False."""
    calls: list[tuple[str, dict[str, Any]]] = []

    class Api:
        def create_draft_from_markdown(self, *_a: Any, **_k: Any) -> dict[str, Any]:
            return {"draft": {"id": 9}}

        def prepublish_draft(self, *_a: Any, **_k: Any) -> dict[str, Any]:
            return {}

        def publish_draft(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            calls.append(("publish", kwargs))
            return {"canonical_url": URL}

    publisher = SubstackPublisher(load_config(ENV), lambda **_k: Api())
    service = ApproveService(h.store, h.queue, publisher)
    update = SimpleNamespace(
        effective_chat=SimpleNamespace(id=CHAT), callback_query=None
    )
    await service.on_approve_tap(
        update,  # type: ignore[arg-type]
        SimpleNamespace(bot=h.bot),  # type: ignore[arg-type]
        h.draft,
    )
    assert calls == [("publish", {"send": False})]
    assert h.bot.sent == [f"Published: {URL}"]


# AC2: success -> seed used today, draft published, URL replied


async def test_success_marks_the_seed_used_with_todays_date(h: Harness) -> None:
    """AC2: the seed line is marked used on today's date."""
    await h.tap()
    text = h.seed_path.read_text()
    assert "2026-11-18" in text
    assert text != SEEDS
    nxt = h.queue.pick("2")
    assert nxt is not None
    assert nxt.text == "Second seed"


async def test_success_marks_the_draft_published_with_its_url(h: Harness) -> None:
    """AC2: the draft is published and stores the post URL."""
    await h.tap()
    stored = h.store.get(h.draft.id)
    assert stored.status == STATUS_PUBLISHED
    assert stored.substack_url == URL


async def test_success_replies_with_the_post_url(h: Harness) -> None:
    """AC2: the bot replies with the URL."""
    await h.tap()
    assert h.bot.sent == [f"Published: {URL}"]


async def test_success_removes_the_old_buttons(h: Harness) -> None:
    """The tapped message loses its buttons."""
    await h.tap(message_id=77)
    assert h.bot.edits == [{"chat_id": CHAT, "message_id": 77, "reply_markup": None}]


async def test_a_button_removal_failure_never_hides_the_url(
    h: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    """ERR-15: cleanup errors are logged, the URL reply still goes out first."""
    h.bot.edit_error = RuntimeError("odd")
    await h.tap()
    assert h.bot.sent == [f"Published: {URL}"]
    assert "Could not remove old buttons" in caplog.text


# AC3: failure -> draft pending, error sent to Stuart, buttons stay


@pytest.mark.parametrize(
    "error",
    [
        PublishError(
            "Substack publish failed (X). No draft was created.",
            None,
            maybe_published=False,
        ),
        SubstackError("Substack could not be reached"),
    ],
)
async def test_failure_keeps_the_draft_pending_and_sends_the_error(
    h: Harness, error: SubstackError
) -> None:
    """AC3: pending, seed untouched, error text sent, buttons not removed."""
    h.publisher.error = error
    await h.tap()
    assert h.status() == STATUS_PENDING
    assert h.seed_path.read_text() == SEEDS
    assert h.bot.sent == [FAILED_TEXT.format(detail=str(error))]
    assert h.bot.edits == []


async def test_failure_leaves_the_seed_reserved_for_a_retry(h: Harness) -> None:
    """AC3: after a failed publish the same draft can still be approved."""
    h.publisher.error = SubstackError("down")
    await h.tap()
    h.publisher.error = None
    await h.tap()
    assert h.status() == STATUS_PUBLISHED
    assert h.bot.sent[-1] == f"Published: {URL}"


async def test_a_failure_that_may_have_gone_live_warns_before_a_retry(
    h: Harness,
) -> None:
    """AC3: with maybe_published the message says to check Substack first."""
    error = PublishError("publish failed after the call", "9", maybe_published=True)
    h.publisher.error = error
    await h.tap()
    assert h.bot.sent[0].endswith(MAY_BE_LIVE_NOTE)
    assert h.status() == STATUS_PENDING


async def test_a_typed_failure_is_not_alerted_twice(
    h: Harness, alerts: list[str]
) -> None:
    """The publisher already alerted a typed failure; the handler does not repeat it."""
    h.publisher.error = SubstackError("down")
    await h.tap()
    assert alerts == []


async def test_an_untyped_failure_alerts_and_keeps_the_draft_pending(
    h: Harness, alerts: list[str]
) -> None:
    """ERR-01: a surprise exception is alerted, reported, and leaves the draft pending."""
    h.publisher.error = RuntimeError("kaboom")
    await h.tap()
    assert h.status() == STATUS_PENDING
    assert len(alerts) == 1
    assert "RuntimeError: kaboom" in alerts[0]
    assert h.bot.sent == [FAILED_TEXT.format(detail="RuntimeError: kaboom")]


async def test_a_store_read_failure_never_publishes(
    h: Harness, alerts: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the draft cannot be re-read nothing is published."""

    def broken(_id: int) -> None:
        raise RuntimeError("db gone")

    monkeypatch.setattr(h.store, "get", broken)
    await h.tap()
    assert h.publisher.calls == []
    assert len(alerts) == 1


# AC4: double tap -> one publish


async def test_a_concurrent_double_tap_publishes_once(h: Harness) -> None:
    """AC4: a second tap while publishing is told to hold on and does not publish."""
    h.publisher.gate = asyncio.Event()
    first = asyncio.create_task(h.tap())
    await asyncio.sleep(0)
    await asyncio.sleep(0.05)  # first tap is now inside publish()
    await h.tap()
    assert h.bot.sent == [IN_FLIGHT_TEXT]
    h.publisher.gate.set()
    await first
    assert len(h.publisher.calls) == 1
    assert h.bot.sent[-1] == f"Published: {URL}"


async def test_many_simultaneous_taps_publish_once(h: Harness) -> None:
    """AC4: five taps at once still make exactly one publish call."""
    await asyncio.gather(*(h.tap() for _ in range(5)))
    assert len(h.publisher.calls) == 1
    assert h.status() == STATUS_PUBLISHED


async def test_a_tap_after_success_is_already_handled(h: Harness) -> None:
    """AC4: a later tap on the same draft does not publish again."""
    await h.tap()
    await h.tap()
    assert len(h.publisher.calls) == 1
    assert h.bot.sent[-1] == ALREADY_HANDLED_TEXT


async def test_a_binned_draft_is_not_published(h: Harness) -> None:
    """A draft binned in the meantime is "Already handled.", never published."""
    h.store.mark_binned(h.draft.id)
    await h.tap()
    assert h.publisher.calls == []
    assert h.bot.sent == [ALREADY_HANDLED_TEXT]


# Post-publish bookkeeping failures never hide the live URL


async def test_a_mark_published_failure_still_replies_with_the_url_and_alerts(
    h: Harness, alerts: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The post is live: reply with the URL, alert, and warn not to tap again."""

    def broken(_id: int, _url: str) -> None:
        raise RuntimeError("disk full")

    monkeypatch.setattr(h.store, "mark_published", broken)
    await h.tap()
    assert h.bot.sent == [f"Published: {URL}"]
    assert len(alerts) == 1
    assert "do NOT tap Approve again" in alerts[0]
    assert URL in alerts[0]


async def test_a_seed_failure_still_replies_with_the_url_and_alerts(
    h: Harness, alerts: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The draft is published but the seed could not be marked: alert, keep the URL."""

    def broken(_draft_id: str) -> None:
        raise RuntimeError("seed.md locked")

    monkeypatch.setattr(h.queue, "approve", broken)
    await h.tap()
    assert h.status() == STATUS_PUBLISHED
    assert h.bot.sent == [f"Published: {URL}"]
    assert "was not marked used" in alerts[0]


async def test_draft_is_marked_published_before_the_seed_is_touched(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ordering: a seed failure cannot leave a pending draft that republishes."""
    seen: list[str] = []

    def spy(_draft_id: str) -> None:
        seen.append(h.status())

    monkeypatch.setattr(h.queue, "approve", spy)
    await h.tap()
    assert seen == [STATUS_PUBLISHED]


def test_install_registers_the_approve_action() -> None:
    """The Approve button is wired to the service."""
    application = SimpleNamespace(bot_data={})
    service = ApproveService(None, None, None)  # type: ignore[arg-type]
    install_approve(application, service)  # type: ignore[arg-type]
    assert application.bot_data[ACTIONS_KEY][ACTION_APPROVE] == service.on_approve_tap


async def test_a_failed_published_reply_still_alerts_with_the_url_and_clears_buttons(
    h: Harness, alerts: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """PR-Agent: if the URL reply cannot be sent, alert with the URL; no crash."""

    async def broken(**_kwargs: Any) -> None:
        raise RuntimeError("telegram down")

    monkeypatch.setattr(h.bot, "send_message", broken)
    await h.tap()
    assert h.status() == STATUS_PUBLISHED
    assert len(alerts) == 1
    assert URL in alerts[0]
    assert "reply to you could not be sent" in alerts[0]
    assert len(h.bot.edits) == 1
