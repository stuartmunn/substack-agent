"""Tests for the Approve handover (SA-41 acceptance criteria).

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
from substack.exceptions import SubstackAPIException

from humblebrag import approval as approval_module
from humblebrag.alerts import configure_alerts
from humblebrag.approval import (
    FAILED_TEXT,
    HANDED_OVER_TEXT,
    IN_FLIGHT_TEXT,
    ApproveService,
    install_approve,
)
from humblebrag.buttons import ACTIONS_KEY, ALREADY_HANDLED_TEXT
from humblebrag.config import load_config
from humblebrag.drafts import STATUS_HANDED_OVER, STATUS_PENDING, DraftStore
from humblebrag.image_prompt import build_image_prompt
from humblebrag.preview import ACTION_APPROVE
from humblebrag.publisher import (
    DraftError,
    SubstackAuthError,
    SubstackError,
    SubstackPublisher,
)
from humblebrag.seed_queue import SeedQueue

CHAT = 12345
TODAY = date(2026, 11, 18)
SUBSTACK_ID = "5001"
EDITOR = "https://humblebrag.substack.com/publish/post/5001"
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
        self.send_error: Exception | None = None

    async def send_message(self, **kwargs: Any) -> SimpleNamespace:
        """Record a sent message's text and give it an id, or raise the error."""
        if self.send_error is not None:
            raise self.send_error
        self.sent.append(kwargs["text"])
        return SimpleNamespace(message_id=1000 + len(self.sent))

    async def edit_message_reply_markup(self, **kwargs: Any) -> None:
        """Record a button removal, or raise the configured error."""
        if self.edit_error is not None:
            raise self.edit_error
        self.edits.append(kwargs)


class FakePublisher:
    """Stands in for SubstackPublisher: drafts only, publishing is forbidden."""

    def __init__(self) -> None:
        """Succeed by default."""
        self.created: list[tuple[str, str, str]] = []
        self.updated: list[tuple[str, str, str, str]] = []
        self.error: Exception | None = None
        self.gate: asyncio.Event | None = None

    async def create_draft(self, title: str, subtitle: str, body: str) -> str:
        """Record the call, optionally wait on a gate, then succeed or fail."""
        self.created.append((title, subtitle, body))
        if self.gate is not None:
            await self.gate.wait()
        if self.error is not None:
            raise self.error
        return SUBSTACK_ID

    async def update_draft(
        self, draft_id: str, title: str, subtitle: str, body: str
    ) -> None:
        """Record the call, or fail."""
        self.updated.append((draft_id, title, subtitle, body))
        if self.error is not None:
            raise self.error

    @staticmethod
    def editor_url(draft_id: str) -> str:
        """The editor link for a draft."""
        return f"https://humblebrag.substack.com/publish/post/{draft_id}"

    def __getattr__(self, name: str) -> Any:
        """Any publish-like call is a test failure."""
        if "publish" in name or "note" in name:
            raise AssertionError(f"the agent must never call {name}")
        raise AttributeError(name)


class Harness:
    """Real store and queue, fake publisher and bot."""

    def __init__(self, tmp_path: Path, teaser: str = "", scene: str = "") -> None:
        """One pending draft that holds the first seed, with ``teaser`` and
        image ``scene`` if given."""
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
        self.draft = self.store.create(
            seed.text,
            "The Title",
            "The Sub",
            "# Body",
            note_teaser=teaser,
            image_scene=scene,
        )

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

    def stored(self):
        """The draft as stored now."""
        return self.store.get(self.draft.id)

    def seed_used(self) -> bool:
        """True if the seed line is marked used today."""
        return "First seed (used 2026-11-18)" in self.seed_path.read_text()


@pytest.fixture
def h(tmp_path: Path) -> Harness:
    """A fresh harness."""
    return Harness(tmp_path)


TEASER = "I spilled a coffee and found leadership. I am humbled."


SCENE = "Sir Humblebrag III ponders a spilled coffee in a grand library."
PROMPT = build_image_prompt(SCENE)


@pytest.fixture
def ht(tmp_path: Path) -> Harness:
    """A harness whose draft has a Note teaser."""
    return Harness(tmp_path, teaser=TEASER)


def _break(monkeypatch: pytest.MonkeyPatch, target: object, name: str) -> None:
    """Make ``target.name`` raise a RuntimeError."""

    def boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("it broke")

    monkeypatch.setattr(target, name, boom)


# AC1: a Substack draft is created from the stored content, ID saved at once


async def test_approve_creates_a_draft_from_the_stored_content(h: Harness) -> None:
    """AC1: title, subtitle and body go to Substack as a draft."""
    await h.tap()
    assert h.publisher.created == [("The Title", "The Sub", "# Body")]


async def test_the_substack_id_is_saved_right_after_creation(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC1: even if the status write fails, the ID is already stored."""
    _break(monkeypatch, h.store, "mark_handed_over")
    await h.tap()
    stored = h.stored()
    assert stored.status == STATUS_PENDING
    assert stored.substack_draft_id == SUBSTACK_ID


# AC2: marked handed over with the ID and link; seed used today


async def test_success_marks_the_draft_handed_over_with_id_and_link(
    h: Harness,
) -> None:
    """AC2: status, Substack ID and editor link are stored."""
    await h.tap()
    stored = h.stored()
    assert stored.status == STATUS_HANDED_OVER
    assert stored.substack_draft_id == SUBSTACK_ID
    assert stored.substack_url == EDITOR


async def test_success_marks_the_seed_used_with_todays_date(h: Harness) -> None:
    """AC2: the seed line is ticked with today's date and the reservation freed."""
    await h.tap()
    assert h.seed_used()
    assert h.queue._load_reservations() == {}


# AC3: reply with the editor link; buttons removed


async def test_success_replies_with_the_editor_link(h: Harness) -> None:
    """AC3: Stuart gets a link that opens the draft in the Substack editor."""
    await h.tap()
    assert h.bot.sent == [HANDED_OVER_TEXT.format(url=EDITOR)]


async def test_success_removes_the_old_buttons(h: Harness) -> None:
    """AC3: Approve, Redo and Bin come off the preview message."""
    await h.tap(message_id=40)
    assert h.bot.edits == [
        {"chat_id": CHAT, "message_id": 40, "reply_markup": None},
    ]


async def test_a_button_removal_failure_never_hides_the_link(
    h: Harness, alerts: list[str]
) -> None:
    """ERR-15: a failed removal is logged; the handover still completes."""
    h.bot.edit_error = RuntimeError("telegram said no")
    await h.tap()
    assert h.bot.sent == [HANDED_OVER_TEXT.format(url=EDITOR)]
    assert h.stored().status == STATUS_HANDED_OVER


# AC4 + AC5: never publishes, no Notes (static checks live in test_publish.py)


async def test_a_full_handover_never_touches_publishing(h: Harness) -> None:
    """AC4: the fake publisher raises if any publish-like call is made."""
    await h.tap()
    assert h.stored().status == STATUS_HANDED_OVER


# AC6: a double tap creates one draft


async def test_a_concurrent_double_tap_creates_one_draft(h: Harness) -> None:
    """AC6: the second tap during the first is told to hold on."""
    h.publisher.gate = asyncio.Event()
    first = asyncio.create_task(h.tap())
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    await h.tap()
    assert IN_FLIGHT_TEXT in h.bot.sent
    h.publisher.gate.set()
    await first
    assert len(h.publisher.created) == 1


async def test_many_simultaneous_taps_create_one_draft(h: Harness) -> None:
    """AC6: ten taps at once still make one Substack draft."""
    await asyncio.gather(*(h.tap() for _ in range(10)))
    assert len(h.publisher.created) == 1


async def test_a_tap_after_success_is_already_handled(h: Harness) -> None:
    """AC6: a later tap neither creates nor updates anything."""
    await h.tap()
    await h.tap()
    assert h.bot.sent[-1] == ALREADY_HANDLED_TEXT
    assert len(h.publisher.created) == 1
    assert h.publisher.updated == []


async def test_a_binned_draft_is_not_handed_over(h: Harness) -> None:
    """AC6: a draft that is no longer pending creates nothing."""
    h.store.mark_binned(h.draft.id)
    await h.tap()
    assert h.publisher.created == []
    assert h.bot.sent == [ALREADY_HANDLED_TEXT]


# AC7: a pending draft that already has a Substack ID is updated, not recreated


async def test_a_pending_draft_with_an_id_is_updated_not_recreated(
    h: Harness,
) -> None:
    """AC7: after a crash the existing Substack draft is updated and handed over."""
    h.store.set_substack_draft_id(h.draft.id, "777")
    await h.tap()
    assert h.publisher.created == []
    assert h.publisher.updated == [("777", "The Title", "The Sub", "# Body")]
    stored = h.stored()
    assert stored.status == STATUS_HANDED_OVER
    assert stored.substack_draft_id == "777"
    assert stored.substack_url == FakePublisher.editor_url("777")
    assert h.seed_used()


async def test_a_retry_after_a_failed_status_write_updates_the_draft(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC7: a second tap after a failed status write does not create another."""
    with monkeypatch.context() as broken:
        _break(broken, h.store, "mark_handed_over")
        await h.tap()
    await h.tap()
    assert len(h.publisher.created) == 1
    assert h.publisher.updated == [(SUBSTACK_ID, "The Title", "The Sub", "# Body")]
    assert h.stored().status == STATUS_HANDED_OVER


async def test_a_retry_updates_even_if_the_id_was_never_stored(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC7/IDEM-01: the in-memory record stops a duplicate when the store is down."""
    with monkeypatch.context() as broken:
        _break(broken, h.store, "set_substack_draft_id")
        _break(broken, h.store, "mark_handed_over")
        await h.tap()
    await h.tap()
    assert len(h.publisher.created) == 1
    assert len(h.publisher.updated) == 1


async def test_the_memory_is_pruned_once_the_store_records_the_handover(
    h: Harness,
) -> None:
    """Housekeeping: nothing lingers after a clean handover."""
    await h.tap()
    assert h.service._created == {}


# AC8: creating fails -> alert with step and cause; stays pending; nothing used


async def test_a_failed_create_alerts_with_step_and_cause(
    h: Harness, alerts: list[str]
) -> None:
    """AC8: one alert names the step and the cause."""
    h.publisher.error = DraftError("Substack draft creating failed (KeyError).")
    await h.tap()
    assert len(alerts) == 1
    assert "create Substack draft" in alerts[0]
    assert "KeyError" in alerts[0]


async def test_a_failed_create_keeps_the_draft_pending_and_unmarked(
    h: Harness,
) -> None:
    """AC8: pending, no ID, seed untouched and still reserved."""
    h.publisher.error = DraftError("boom")
    await h.tap()
    stored = h.stored()
    assert stored.status == STATUS_PENDING
    assert stored.substack_draft_id is None
    assert not h.seed_used()
    assert h.queue._load_reservations() == {str(h.draft.id): "First seed"}


async def test_a_failed_create_leaves_the_buttons_and_tells_stuart(
    h: Harness,
) -> None:
    """AC8: buttons stay active (not removed) and the error is sent."""
    h.publisher.error = DraftError("boom")
    await h.tap()
    assert h.bot.edits == []
    assert h.bot.sent == [FAILED_TEXT.format(detail="DraftError: boom")]


async def test_a_failed_create_can_be_retried(h: Harness) -> None:
    """AC8: the draft is still approvable once Substack recovers."""
    h.publisher.error = DraftError("boom")
    await h.tap()
    h.publisher.error = None
    await h.tap()
    assert h.stored().status == STATUS_HANDED_OVER


async def test_a_failed_update_alerts_with_step_id_and_link(
    h: Harness, alerts: list[str]
) -> None:
    """AC8: an update failure names its step and keeps the draft pending."""
    h.store.set_substack_draft_id(h.draft.id, "777")
    h.publisher.error = DraftError("boom")
    await h.tap()
    assert "update Substack draft" in alerts[0]
    assert "777" in alerts[0]
    assert FakePublisher.editor_url("777") in alerts[0]
    assert h.stored().status == STATUS_PENDING


async def test_a_session_failure_is_not_alerted_twice(
    h: Harness, alerts: list[str]
) -> None:
    """The publisher already alerts on session errors; Approve adds none."""
    h.publisher.error = SubstackAuthError("Substack session expired")
    await h.tap()
    assert alerts == []
    assert h.stored().status == STATUS_PENDING
    assert "session expired" in h.bot.sent[-1]


async def test_a_plain_session_check_failure_is_not_alerted_twice(
    h: Harness, alerts: list[str]
) -> None:
    """PR-Agent: any SubstackError that is not a DraftError came from the session
    check, which alerts itself, so Approve adds no second alert."""
    h.publisher.error = SubstackError("Session check failed: outage")
    await h.tap()
    assert alerts == []
    assert h.stored().status == STATUS_PENDING


async def test_an_untyped_create_failure_alerts_and_keeps_the_draft_pending(
    h: Harness, alerts: list[str]
) -> None:
    """ERR-18: a surprise exception is alerted with the step and left pending."""
    h.publisher.error = RuntimeError("library changed")
    await h.tap()
    assert "create Substack draft" in alerts[0]
    assert "library changed" in alerts[0]
    assert h.stored().status == STATUS_PENDING


async def test_a_store_read_failure_creates_nothing(
    h: Harness, alerts: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed re-read must not create a draft blind."""
    _break(monkeypatch, h.store, "get")
    await h.tap()
    assert h.publisher.created == []
    assert len(alerts) == 1


# AC9: created but a later step fails -> alert with step, Substack ID, link


@pytest.mark.parametrize(
    ("method", "owner", "step"),
    [
        ("set_substack_draft_id", "store", "save Substack draft ID"),
        ("mark_handed_over", "store", "save handed-over status"),
        ("approve", "queue", "mark seed used"),
    ],
)
async def test_a_later_step_failure_alerts_with_step_id_and_link(
    h: Harness,
    alerts: list[str],
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    owner: str,
    step: str,
) -> None:
    """AC9: the alert names the failed step, the Substack ID and the link."""
    _break(monkeypatch, getattr(h, owner), method)
    await h.tap()
    assert len(alerts) == 1
    assert f"step '{step}' failed" in alerts[0]
    assert SUBSTACK_ID in alerts[0]
    assert EDITOR in alerts[0]
    assert "it broke" in alerts[0]


async def test_a_failed_reply_alerts_with_step_id_and_link(
    h: Harness, alerts: list[str]
) -> None:
    """AC9: if the reply cannot be sent, the alert still carries the link."""
    h.bot.send_error = RuntimeError("telegram down")
    await h.tap()
    assert "step 'send reply' failed" in alerts[0]
    assert SUBSTACK_ID in alerts[0]
    assert EDITOR in alerts[0]
    assert h.stored().status == STATUS_HANDED_OVER
    assert h.bot.edits  # buttons still cleared (ERR-20)


async def test_a_seed_failure_does_not_skip_the_reply(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC9: the link reaches Stuart even when marking the seed failed."""
    _break(monkeypatch, h.queue, "approve")
    await h.tap()
    assert h.bot.sent == [HANDED_OVER_TEXT.format(url=EDITOR)]
    assert h.stored().status == STATUS_HANDED_OVER


async def test_several_failures_are_reported_in_one_alert(
    h: Harness, alerts: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC9: one alert lists every failed step."""
    _break(monkeypatch, h.store, "mark_handed_over")
    _break(monkeypatch, h.queue, "approve")
    await h.tap()
    assert len(alerts) == 1
    assert "save handed-over status" in alerts[0]
    assert "mark seed used" in alerts[0]


# The real publisher over a fake API: drafts only, never publish


class RecordingApi:
    """A Substack API that records calls and refuses anything that publishes."""

    def __init__(self, **_kwargs: Any) -> None:
        """Start empty."""
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def create_draft_from_markdown(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """Record a draft creation."""
        self.calls.append(("create", args, kwargs))
        return {"draft": {"id": 5001}}

    def update_draft_from_markdown(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """Record a body update."""
        self.calls.append(("update", args, kwargs))
        return {}

    def put_draft(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """Record a field update."""
        self.calls.append(("put", args, kwargs))
        return {}

    def __getattr__(self, name: str) -> Any:
        """Publishing, prepublishing, scheduling or Notes are forbidden."""
        raise AssertionError(f"the agent must never call {name}")


async def test_the_real_publisher_creates_a_draft_and_nothing_else(
    tmp_path: Path,
) -> None:
    """AC1/AC4/AC5: Approve over the real publisher makes exactly one create call."""
    api = RecordingApi()
    publisher = SubstackPublisher(load_config(ENV), lambda **_k: api)
    harness = Harness(tmp_path)
    harness.service = ApproveService(harness.store, harness.queue, publisher)
    await harness.tap()
    assert [name for name, _a, _k in api.calls] == ["create"]
    assert harness.stored().substack_url == EDITOR


async def test_a_substack_error_from_the_real_publisher_is_alerted_once(
    tmp_path: Path, alerts: list[str]
) -> None:
    """AC8 end to end: a library error becomes one alert and a pending draft."""

    class FailingApi(RecordingApi):
        def create_draft_from_markdown(self, *a: Any, **k: Any) -> dict[str, Any]:
            raise SubstackAPIException(500, "boom")

    api = FailingApi()
    publisher = SubstackPublisher(load_config(ENV), lambda **_k: api)
    harness = Harness(tmp_path)
    harness.service = ApproveService(harness.store, harness.queue, publisher)
    await harness.tap()
    assert len(alerts) == 1
    assert "create Substack draft" in alerts[0]
    assert harness.stored().status == STATUS_PENDING


def test_install_registers_the_approve_action() -> None:
    """The Approve button is routed to the service."""
    application = SimpleNamespace(bot_data={})
    service = SimpleNamespace(on_approve_tap=lambda *a: None)
    install_approve(application, service)  # type: ignore[arg-type]
    assert ACTION_APPROVE in application.bot_data[ACTIONS_KEY]


# SA-48: the Note teaser arrives alone after the link


async def test_the_teaser_follows_the_link_alone_in_its_own_message(
    ht: Harness,
) -> None:
    """SA-48 AC3/AC10: link first, then the teaser by itself, nothing else in it."""
    await ht.tap()
    assert ht.bot.sent == [HANDED_OVER_TEXT.format(url=EDITOR), TEASER]


async def test_a_draft_without_a_teaser_sends_no_teaser_message(h: Harness) -> None:
    """SA-48 AC6: an older draft with an empty teaser approves as before."""
    await h.tap()
    assert h.bot.sent == [HANDED_OVER_TEXT.format(url=EDITOR)]
    assert h.stored().status == STATUS_HANDED_OVER


async def test_a_failed_teaser_send_alerts_and_keeps_the_approval(
    ht: Harness, alerts: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """SA-48 AC4/TST-04: Stuart is alerted; the handover is not rolled back."""
    real = ht.bot.send_message

    async def fail_on_teaser(**kwargs: Any) -> None:
        if kwargs["text"] == TEASER:
            raise RuntimeError("telegram said no")
        await real(**kwargs)

    monkeypatch.setattr(ht.bot, "send_message", fail_on_teaser)
    await ht.tap()
    assert ht.bot.sent == [HANDED_OVER_TEXT.format(url=EDITOR)]
    assert ht.stored().status == STATUS_HANDED_OVER
    assert ht.seed_used()
    [alert_text] = alerts
    assert "send Note teaser" in alert_text
    assert "telegram said no" in alert_text
    assert EDITOR in alert_text


async def test_no_teaser_is_sent_when_the_substack_draft_was_not_created(
    ht: Harness,
) -> None:
    """SA-48 AC5: a failed create sends only the failure message."""
    ht.publisher.error = DraftError("substack is down")
    await ht.tap()
    assert TEASER not in ht.bot.sent
    assert ht.stored().status == STATUS_PENDING


async def test_a_repeat_tap_on_a_handed_over_draft_does_not_resend_the_teaser(
    ht: Harness,
) -> None:
    """SA-48 AC7: the second tap is refused as already handled."""
    await ht.tap()
    await ht.tap()
    assert ht.bot.sent.count(TEASER) == 1
    assert ht.bot.sent[-1] == ALREADY_HANDLED_TEXT


async def test_a_retry_after_a_bookkeeping_failure_does_not_resend_the_teaser(
    ht: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SA-48 AC7 risk: the draft is still pending after a failed status write,
    so the retry must not send the teaser a second time."""
    _break(monkeypatch, ht.store, "mark_handed_over")
    await ht.tap()
    await ht.tap()
    assert ht.stored().status == STATUS_PENDING
    assert ht.bot.sent.count(TEASER) == 1


async def test_the_teaser_is_sent_even_if_the_link_reply_failed(
    ht: Harness, alerts: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The teaser does not depend on the link message; the link failure alerts."""
    real = ht.bot.send_message

    async def fail_on_link(**kwargs: Any) -> None:
        if kwargs["text"] == HANDED_OVER_TEXT.format(url=EDITOR):
            raise RuntimeError("link send failed")
        await real(**kwargs)

    monkeypatch.setattr(ht.bot, "send_message", fail_on_link)
    await ht.tap()
    assert ht.bot.sent == [TEASER]
    assert len(alerts) == 1
    assert "send reply" in alerts[0]


async def test_the_sent_teaser_bookkeeping_is_cleared_once_handed_over(
    ht: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PR-Agent: the in-memory set only holds drafts the store still has pending."""
    await ht.tap()
    assert ht.service._teaser_sent == set()
    assert ht.service._created == {}


async def test_the_sent_teaser_bookkeeping_is_kept_while_the_draft_is_pending(
    ht: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The entry stays after a failed status write, which is when a retry needs it."""
    _break(monkeypatch, ht.store, "mark_handed_over")
    await ht.tap()
    assert ht.service._teaser_sent == {ht.draft.id}


# SA-49: the Gemini image prompt arrives alone after the teaser


@pytest.fixture
def hp(tmp_path: Path) -> Harness:
    """A harness whose draft has a Note teaser and an image scene."""
    return Harness(tmp_path, teaser=TEASER, scene=SCENE)


async def test_the_image_prompt_follows_the_teaser_alone(hp: Harness) -> None:
    """SA-49 AC7/AC11: link, teaser, then the full prompt with the reply line."""
    await hp.tap()
    assert hp.bot.sent == [HANDED_OVER_TEXT.format(url=EDITOR), TEASER, PROMPT]
    prompt_id = 1000 + 3
    assert hp.store.find_by_image_prompt_message(prompt_id).id == hp.draft.id  # SA-50
    assert SCENE in PROMPT
    assert PROMPT.endswith("Reply to this message with the image.")


async def test_the_prompt_is_sent_when_the_draft_has_no_teaser(tmp_path: Path) -> None:
    """The prompt does not depend on a teaser being present."""
    only_scene = Harness(tmp_path, scene=SCENE)
    await only_scene.tap()
    assert only_scene.bot.sent == [HANDED_OVER_TEXT.format(url=EDITOR), PROMPT]


async def test_an_older_draft_with_no_scene_sends_no_prompt(ht: Harness) -> None:
    """SA-49 AC8: a draft with a teaser but no scene sends no prompt."""
    await ht.tap()
    assert ht.bot.sent == [HANDED_OVER_TEXT.format(url=EDITOR), TEASER]


async def test_a_repeat_tap_does_not_resend_the_prompt(hp: Harness) -> None:
    """SA-49 AC9/AC11: the second tap is refused as already handled."""
    await hp.tap()
    await hp.tap()
    assert hp.bot.sent.count(PROMPT) == 1
    assert hp.bot.sent[-1] == ALREADY_HANDLED_TEXT


async def test_a_retry_after_a_bookkeeping_failure_does_not_resend_the_prompt(
    hp: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SA-49 AC9: a still-pending draft retried must not repeat the prompt."""
    _break(monkeypatch, hp.store, "mark_handed_over")
    await hp.tap()
    await hp.tap()
    assert hp.stored().status == STATUS_PENDING
    assert hp.bot.sent.count(PROMPT) == 1


async def test_a_failed_prompt_send_alerts_and_keeps_the_approval(
    hp: Harness, alerts: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """SA-49 AC10/TST-04: alert Stuart; the handover is not rolled back."""
    real = hp.bot.send_message

    async def fail_on_prompt(**kwargs: Any) -> None:
        if kwargs["text"] == PROMPT:
            raise RuntimeError("telegram said no")
        await real(**kwargs)

    monkeypatch.setattr(hp.bot, "send_message", fail_on_prompt)
    await hp.tap()
    assert hp.bot.sent == [HANDED_OVER_TEXT.format(url=EDITOR), TEASER]
    assert hp.stored().status == STATUS_HANDED_OVER
    [alert_text] = alerts
    assert "send image prompt" in alert_text
    assert "telegram said no" in alert_text
    assert EDITOR in alert_text


async def test_a_failed_teaser_does_not_stop_the_prompt(
    hp: Harness, alerts: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The two follow-ups are independent: the prompt still goes out."""
    real = hp.bot.send_message

    async def fail_on_teaser(**kwargs: Any) -> None:
        if kwargs["text"] == TEASER:
            raise RuntimeError("teaser failed")
        await real(**kwargs)

    monkeypatch.setattr(hp.bot, "send_message", fail_on_teaser)
    await hp.tap()
    assert hp.bot.sent == [HANDED_OVER_TEXT.format(url=EDITOR), PROMPT]
    assert len(alerts) == 1


async def test_a_missing_template_alerts_and_keeps_the_approval(
    hp: Harness, alerts: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A broken template is a failure to send: alert, never roll back."""
    _break(monkeypatch, approval_module, "build_image_prompt")
    await hp.tap()
    assert hp.stored().status == STATUS_HANDED_OVER
    assert "send image prompt" in alerts[0]


async def test_no_prompt_is_sent_when_the_substack_draft_was_not_created(
    hp: Harness,
) -> None:
    """A failed create sends neither the teaser nor the prompt."""
    hp.publisher.error = DraftError("substack is down")
    await hp.tap()
    assert PROMPT not in hp.bot.sent
    assert TEASER not in hp.bot.sent


async def test_the_sent_prompt_bookkeeping_is_cleared_once_handed_over(
    hp: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MEM-01: cleared once handed over, kept while the draft is still pending."""
    await hp.tap()
    assert hp.service._image_prompt_sent == set()


async def test_the_sent_prompt_bookkeeping_is_kept_while_pending(
    hp: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After a failed status write the retry still needs the entry."""
    _break(monkeypatch, hp.store, "mark_handed_over")
    await hp.tap()
    assert hp.service._image_prompt_sent == {hp.draft.id}


async def test_a_failure_to_save_the_prompt_message_id_alerts(
    hp: Harness, alerts: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """SA-50: without the id a reply cannot be matched, so Stuart is alerted;
    the approval stands and the prompt is not sent again."""
    _break(monkeypatch, hp.store, "set_image_prompt_message_id")
    await hp.tap()
    assert hp.bot.sent.count(PROMPT) == 1
    assert hp.stored().status == STATUS_HANDED_OVER
    [alert_text] = alerts
    assert "send image prompt" in alert_text
