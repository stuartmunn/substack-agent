"""Tests for the Bin handler (SA-30 acceptance criteria)."""

from collections.abc import Iterator
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from telegram.error import TelegramError

from humblebrag.alerts import configure_alerts
from humblebrag.binning import BINNED_TEXT, FAILED_TEXT, BinService, install_bin
from humblebrag.buttons import ACTIONS_KEY, ALREADY_HANDLED_TEXT
from humblebrag.drafts import STATUS_BINNED, STATUS_PENDING, DraftStore
from humblebrag.preview import ACTION_BIN
from humblebrag.seed_queue import ReservationError, SeedQueue

CHAT = 12345
TODAY = date(2026, 11, 18)
SEEDS = "- [ ] First seed\n- [ ] Second seed\n"


@pytest.fixture(autouse=True)
def alerts() -> Iterator[list[str]]:
    """Capture alert texts, and unset the sender afterwards."""
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


class Harness:
    """A real store and seed queue in a temp dir, with a fake bot."""

    def __init__(self, tmp_path: Path) -> None:
        """Create the seed file, queue, store, and one pending draft."""
        seed_path = tmp_path / "seed.md"
        seed_path.write_text(SEEDS)
        self.seed_path = seed_path
        self.queue = SeedQueue(seed_path, lambda: TODAY)
        self.store = DraftStore(tmp_path / "d.db")
        self.bot = FakeBot()
        self.service = BinService(self.store, self.queue)
        seed = self.queue.pick("1")
        assert seed is not None
        self.draft = self.store.create(seed.text, "T", "S", "B")

    async def tap(self, message_id: int = 40) -> None:
        """Tap Bin on the draft's message."""
        update = SimpleNamespace(
            effective_chat=SimpleNamespace(id=CHAT),
            callback_query=SimpleNamespace(
                message=SimpleNamespace(message_id=message_id)
            ),
        )
        await self.service.on_bin_tap(update, SimpleNamespace(bot=self.bot), self.draft)


@pytest.fixture
def h(tmp_path: Path) -> Harness:
    """A fresh harness with draft 1 holding "First seed"."""
    return Harness(tmp_path)


async def test_bin_marks_the_draft_binned(h: Harness) -> None:
    """AC1: the draft's status becomes binned, with a resolved time."""
    await h.tap()
    stored = h.store.get(h.draft.id)
    assert stored.status == STATUS_BINNED
    assert stored.resolved_at is not None


async def test_the_seed_goes_back_to_the_front_of_the_queue(h: Harness) -> None:
    """AC1: the next pick returns the binned draft's seed, unmarked."""
    other = h.queue.pick("2")
    assert other is not None
    assert other.text == "Second seed"  # First seed is reserved by draft 1
    h.queue.bin_draft("2")
    await h.tap()
    nxt = h.queue.pick("3")
    assert nxt is not None
    assert nxt.text == "First seed"
    assert "First seed (used" not in h.seed_path.read_text()
    assert h.seed_path.read_text() == SEEDS


async def test_bin_confirms_with_the_exact_text(h: Harness) -> None:
    """AC2: "Binned, seed requeued"."""
    await h.tap()
    assert h.bot.sent == ["Binned, seed requeued"]
    assert BINNED_TEXT == "Binned, seed requeued"


async def test_old_buttons_are_removed(h: Harness) -> None:
    """The tapped message loses its buttons so they cannot be tapped again."""
    await h.tap(message_id=77)
    assert h.bot.edits == [{"chat_id": CHAT, "message_id": 77, "reply_markup": None}]


@pytest.mark.parametrize("error", [TelegramError("too old"), RuntimeError("odd")])
async def test_failing_to_remove_buttons_still_confirms(
    h: Harness, error: Exception, caplog: pytest.LogCaptureFixture
) -> None:
    """ERR-15: any error on the old message is logged and cannot hide success."""
    h.bot.edit_error = error
    await h.tap()
    assert h.bot.sent == [BINNED_TEXT]
    assert "Could not remove old buttons" in caplog.text


async def test_binning_twice_says_already_handled(h: Harness) -> None:
    """A second tap (e.g. a stale button) is "Already handled."."""
    await h.tap()
    await h.tap()
    assert h.bot.sent == [BINNED_TEXT, ALREADY_HANDLED_TEXT]
    assert h.store.get(h.draft.id).status == STATUS_BINNED


async def test_tapping_bin_again_after_a_failed_seed_release_recovers(
    h: Harness, alerts: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A binned draft whose seed is stuck can be fixed by tapping Bin again."""
    real = h.queue.bin_draft

    def broken(_draft_id: str) -> bool:
        raise ReservationError("lock stuck")

    monkeypatch.setattr(h.queue, "bin_draft", broken)
    await h.tap()
    assert h.bot.sent == [FAILED_TEXT]
    assert h.store.get(h.draft.id).status == STATUS_BINNED
    assert h.bot.edits == []  # buttons stay so Bin can be tapped again
    monkeypatch.setattr(h.queue, "bin_draft", real)
    await h.tap()
    assert h.bot.sent[-1] == BINNED_TEXT
    nxt = h.queue.pick("9")
    assert nxt is not None
    assert nxt.text == "First seed"
    await h.tap()
    assert h.bot.sent[-1] == ALREADY_HANDLED_TEXT


async def test_binning_an_already_published_draft_leaves_the_seed_alone(
    h: Harness,
) -> None:
    """A draft resolved elsewhere is not binned and its reservation is untouched."""
    h.store.mark_published(h.draft.id, "https://example.substack.com/p/x")
    await h.tap()
    assert h.bot.sent == [ALREADY_HANDLED_TEXT]
    assert h.queue.pick("1") is not None  # reservation for draft 1 still held


async def test_a_missing_reservation_still_bins_with_a_warning(
    h: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    """If nothing held the seed it is already available: bin succeeds, warns."""
    h.queue.bin_draft("1")
    await h.tap()
    assert h.bot.sent == [BINNED_TEXT]
    assert "held no seed reservation" in caplog.text


async def test_store_failure_alerts_and_tells_the_chat(
    h: Harness, alerts: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """ERR-01: an unexpected store error alerts Stuart; no confirmation is sent."""

    def broken(_draft_id: int) -> None:
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(h.store, "mark_binned", broken)
    await h.tap()
    assert len(alerts) == 1
    assert "Binning draft 1 failed" in alerts[0]
    assert "disk on fire" in alerts[0]
    assert h.bot.sent == [FAILED_TEXT]
    assert h.store.get(h.draft.id).status == STATUS_PENDING


async def test_seed_release_failure_alerts_and_tells_the_chat(
    h: Harness, alerts: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """ERR-01: if the seed cannot be released Stuart is told, not "requeued"."""

    def broken(_draft_id: str) -> bool:
        raise ReservationError("lock stuck")

    monkeypatch.setattr(h.queue, "bin_draft", broken)
    await h.tap()
    assert len(alerts) == 1
    assert "lock stuck" in alerts[0]
    assert h.bot.sent == [FAILED_TEXT]


def test_install_registers_the_bin_action() -> None:
    """The Bin button is wired to the service."""
    application = SimpleNamespace(bot_data={})
    service = BinService(None, None)  # type: ignore[arg-type]
    install_bin(application, service)  # type: ignore[arg-type]
    assert application.bot_data[ACTIONS_KEY][ACTION_BIN] == service.on_bin_tap
