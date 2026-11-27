"""Integration tests for the end-to-end run (SA-35 acceptance criteria).

Real DraftStore, SeedQueue, SeedProvider, PostGenerator, SafetyGuard, RunGate,
RunPipeline, Scheduler, ApproveService and SubstackPublisher, in a temp dir.
Mocked at the edges only: Claude (a scripted client), Telegram (a recording
bot) and Substack (a recording API). Nothing is ever published for real.
"""

import asyncio
import json
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from humblebrag.alerts import configure_alerts, telegram_sender
from humblebrag.approval import HANDED_OVER_TEXT, ApproveService
from humblebrag.claude_client import ClaudeAPIError, Flow
from humblebrag.config import Config, load_config
from humblebrag.drafts import (
    STATUS_BINNED,
    STATUS_HANDED_OVER,
    STATUS_PENDING,
    Draft,
    DraftStore,
    RunGate,
)
from humblebrag.generator import PostGenerator
from humblebrag.guard import MAX_GUARD_REGENERATIONS, SafetyGuard, generate_checked
from humblebrag.image_prompt import build_image_prompt
from humblebrag.persona import Persona
from humblebrag.pipeline import (
    PROVISIONAL_PREFIX,
    STEP_GENERATE,
    STEP_PREVIEW,
    STEP_SAVE,
    STEP_SEED,
    RunPipeline,
)
from humblebrag.preview import send_preview
from humblebrag.publisher import SubstackPublisher
from humblebrag.scheduler import Scheduler, SchedulerState
from humblebrag.seed_provider import SEED_SYSTEM, SeedProvider
from humblebrag.seed_queue import SeedQueue

CHAT = 12345
TODAY = date(2026, 11, 18)
EDITOR_URL = "https://humblebrag.substack.com/publish/post/77"
SEEDS = "- [ ] First seed\n- [ ] Second seed\n"
ENV = {
    "TELEGRAM_BOT_TOKEN": "123456:TESTTOKEN",
    "TELEGRAM_CHAT_ID": str(CHAT),
    "ANTHROPIC_API_KEY": "sk-ant-key",
    "CLAUDE_MODEL": "claude-test-model",
    "SUBSTACK_PUBLICATION_URL": "https://humblebrag.substack.com",
    "SUBSTACK_COOKIE": "s%3ASECRET-SID",
    "POST_MIN_WORDS": "3",
    "POST_MAX_WORDS": "50",
    "MAX_PENDING_DRAFTS": "2",
}
GOOD_POST = {
    "title": "A Humble Brew",
    "subtitle": "Quietly brilliant",
    "body_markdown": "I spilled my coffee today and learned nothing at all.",
    "note_teaser": "Coffee spilled, leadership gained.",
    "image_scene": "He sips coffee in a grand library.",
}
PASS = json.dumps({"verdict": "pass", "reason": "obvious satire"})
FAIL = json.dumps({"verdict": "fail", "reason": "too close to a real person"})


@pytest.fixture(autouse=True)
def alerts() -> Iterator[list[str]]:
    """Capture alert texts (these also stand in for what Telegram would show)."""
    sent: list[str] = []

    async def sender(text: str) -> None:
        sent.append(text)

    configure_alerts(sender)
    yield sent
    configure_alerts(None)


class FakeClaude:
    """A scripted Claude: routes by system prompt, can fail a given kind."""

    def __init__(self) -> None:
        """Healthy by default."""
        self.post = dict(GOOD_POST)
        self.verdicts: list[str] = [PASS]
        self.fail_kind: str | None = None
        self.calls: list[str] = []

    async def complete(self, system: str, prompt: str, flow: Flow) -> str:
        """Answer a seed, classifier or generation request."""
        if system == SEED_SYSTEM:
            kind, reply = "seed", "A lukewarm cup of tea"
        elif "verdict" in system:
            kind = "guard"
            reply = self.verdicts.pop(0) if len(self.verdicts) > 1 else self.verdicts[0]
        else:
            kind, reply = "generate", json.dumps(self.post)
        self.calls.append(kind)
        if self.fail_kind == kind:
            raise ClaudeAPIError("claude is down")
        return reply


class FakeBot:
    """Records messages (with their buttons) like Telegram would show them."""

    def __init__(self) -> None:
        """Start empty."""
        self.messages: list[dict[str, Any]] = []
        self.fail_send = False

    async def send_message(self, **kwargs: Any) -> SimpleNamespace:
        """Record a message and give it an id, or fail."""
        if self.fail_send:
            raise RuntimeError("telegram down")
        self.messages.append(kwargs)
        return SimpleNamespace(message_id=len(self.messages))

    async def edit_message_reply_markup(self, **_kwargs: Any) -> None:
        """Accept a button removal."""


class FakeSubstackApi:
    """Records every Substack call, so tests can prove nothing was published."""

    def __init__(self) -> None:
        """Start empty."""
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def create_draft_from_markdown(self, *_a: Any, **kwargs: Any) -> dict[str, Any]:
        """Record a draft creation."""
        self.calls.append(("create", kwargs))
        return {"draft": {"id": 77}}

    def __getattr__(self, name: str) -> Any:
        """Publishing, prepublishing, scheduling or Notes are forbidden."""
        raise AssertionError(f"the agent must never call {name}")


class Rig:
    """The whole pipeline with only Claude, Telegram and Substack mocked."""

    def __init__(self, tmp_path: Path, seeds: str = SEEDS) -> None:
        """Assemble real services over a temp data dir."""
        self.config: Config = load_config(ENV)
        tmp_path.mkdir(parents=True, exist_ok=True)
        self.seed_path = tmp_path / "seed.md"
        self.seed_path.write_text(seeds)
        self.claude = FakeClaude()
        self.bot = FakeBot()
        self.api = FakeSubstackApi()
        self.store = DraftStore(tmp_path / "d.db")
        self.queue = SeedQueue(self.seed_path, lambda: TODAY)
        persona = Persona(version=1, text="You are Sir Humblebrag.")
        generator = PostGenerator(self.claude, persona, self.config)  # type: ignore[arg-type]
        guard = SafetyGuard(self.claude)  # type: ignore[arg-type]
        self.publisher = SubstackPublisher(self.config, lambda **_k: self.api)

        async def generate(seed: str, steer: str | None) -> Any:
            return await generate_checked(generator, guard, seed, steer)

        async def preview(draft: Draft, line: str | None) -> None:
            await send_preview(self.bot, CHAT, draft, line)  # type: ignore[arg-type]

        notify = telegram_sender(self.bot, CHAT)  # type: ignore[arg-type]
        self.pipeline = RunPipeline(
            gate=RunGate(self.store, self.config.max_pending_drafts, notify),
            store=self.store,
            queue=self.queue,
            seed_for=SeedProvider(self.queue, self.claude).seed_for,  # type: ignore[arg-type]
            generate=generate,
            send_preview=preview,
            max_pending=self.config.max_pending_drafts,
        )
        self.approve = ApproveService(self.store, self.queue, self.publisher)

    def reservations(self) -> dict[str, str]:
        """The seed reservations currently held."""
        return self.queue._load_reservations()

    def assert_nothing_published(self) -> None:
        """No publish (or even draft) call reached Substack."""
        assert self.api.calls == []

    async def tap_approve(self, draft: Draft) -> None:
        """Tap Approve on a draft."""
        update = SimpleNamespace(
            effective_chat=SimpleNamespace(id=CHAT), callback_query=None
        )
        await self.approve.on_approve_tap(
            update,  # type: ignore[arg-type]
            SimpleNamespace(bot=self.bot),  # type: ignore[arg-type]
            draft,
        )


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    """A fresh rig with two unused seeds."""
    return Rig(tmp_path)


# AC1: picks a seed (or invents one), generates, guards, saves, sends the preview


async def test_happy_path_goes_from_seed_to_preview(
    rig: Rig, alerts: list[str]
) -> None:
    """AC1: one run ends with a pending draft, a reserved seed and a preview."""
    draft = await rig.pipeline.run()
    assert draft is not None
    stored = rig.store.get(draft.id)
    assert stored.status == STATUS_PENDING
    assert (stored.title, stored.subtitle) == ("A Humble Brew", "Quietly brilliant")
    assert stored.note_teaser == "Coffee spilled, leadership gained."
    assert stored.image_scene == "He sips coffee in a grand library."
    assert stored.seed == "First seed"
    assert rig.claude.calls == ["generate", "guard"]
    [message] = rig.bot.messages
    assert "A Humble Brew" in message["text"]
    assert "Guard passed" in message["text"]
    assert message["reply_markup"] is not None
    assert alerts == []


async def test_happy_path_reserves_the_seed_under_the_drafts_id(rig: Rig) -> None:
    """AC1: Approve and Bin look the seed up by str(draft.id), so it is re-keyed."""
    draft = await rig.pipeline.run()
    assert draft is not None
    assert rig.reservations() == {str(draft.id): "First seed"}
    assert rig.seed_path.read_text() == SEEDS  # not marked used until Approve


async def test_happy_path_publishes_nothing(rig: Rig) -> None:
    """AC2 (nothing published): a run alone never touches Substack."""
    await rig.pipeline.run()
    rig.assert_nothing_published()


async def test_the_run_invents_a_seed_when_none_are_unused(tmp_path: Path) -> None:
    """AC1: with no unused seeds Claude invents one, and the run still completes."""
    rig = Rig(tmp_path / "x", seeds="- [x] First seed (used 2026-01-01)\n")
    draft = await rig.pipeline.run()
    assert draft is not None
    assert draft.seed == "A lukewarm cup of tea"
    assert rig.claude.calls[0] == "seed"
    assert rig.reservations() == {str(draft.id): "A lukewarm cup of tea"}


async def test_the_full_flow_approve_hands_over_one_draft_and_never_publishes(
    rig: Rig,
) -> None:
    """End to end: preview, then Approve creates one Substack draft and uses the seed."""
    draft = await rig.pipeline.run()
    assert draft is not None
    rig.assert_nothing_published()
    await rig.tap_approve(draft)
    assert [name for name, _ in rig.api.calls] == ["create"]
    stored = rig.store.get(draft.id)
    assert stored.status == STATUS_HANDED_OVER
    assert stored.substack_draft_id == "77"
    assert "2026-11-18" in rig.seed_path.read_text()
    assert rig.reservations() == {}
    assert [m["text"] for m in rig.bot.messages[-3:]] == [
        HANDED_OVER_TEXT.format(url=EDITOR_URL),
        "Coffee spilled, leadership gained.",
        build_image_prompt("He sips coffee in a grand library."),
    ]


async def test_a_run_is_paused_when_too_many_drafts_are_pending(
    rig: Rig, alerts: list[str]
) -> None:
    """The pending cap pauses the run: nothing generated, Stuart told."""
    for _ in range(2):
        rig.store.create("s", "t", "s", "b")
    assert await rig.pipeline.run() is None
    assert rig.claude.calls == []
    assert len(rig.bot.messages) == 1  # the paused message
    assert rig.reservations() == {}


async def test_the_scheduler_runs_the_pipeline_once_a_day(
    rig: Rig, tmp_path: Path
) -> None:
    """AC1: the scheduler drives the pipeline at 08:30 and only once per day."""

    async def run_once() -> None:
        await rig.pipeline.run()

    now = datetime(2026, 11, 18, 8, 30, tzinfo=UTC)
    scheduler = Scheduler(
        rig.config, SchedulerState(tmp_path / "s.json"), run_once, clock=lambda: now
    )
    await scheduler.tick()
    await scheduler.tick()
    assert len(rig.store.list_pending()) == 1


# AC2: any step fails -> seed released, alert with step name, nothing published


def _assert_failed_cleanly(rig: Rig, alerts: list[str], step: str) -> None:
    """Seed released, exactly one alert naming the step, nothing published."""
    assert rig.reservations() == {}
    assert rig.seed_path.read_text() == SEEDS
    assert len(alerts) == 1
    assert step in alerts[0]
    assert rig.store.list_pending() == []
    rig.assert_nothing_published()


async def test_failure_picking_a_seed(
    rig: Rig, alerts: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC2/AC3: a failure at "pick seed" alerts with that step."""

    def broken(_key: str) -> None:
        raise RuntimeError("seed file locked")

    monkeypatch.setattr(rig.queue, "pick", broken)
    assert await rig.pipeline.run() is None
    assert len(alerts) == 1
    assert STEP_SEED in alerts[0]
    assert "seed file locked" in alerts[0]
    rig.assert_nothing_published()


async def test_failure_inventing_a_seed_alerts_once(tmp_path: Path) -> None:
    """AC2: SeedProvider alerts for itself when Claude only repeats seeds; no 2nd alert."""
    rig = Rig(tmp_path / "y", seeds="- [x] A lukewarm cup of tea (used 2026-01-01)\n")
    sent: list[str] = []

    async def sender(text: str) -> None:
        sent.append(text)

    configure_alerts(sender)
    assert await rig.pipeline.run() is None
    assert len(sent) == 1
    assert "No unique seed" in sent[0]
    assert rig.reservations() == {}


async def test_failure_generating_the_post(rig: Rig, alerts: list[str]) -> None:
    """AC2/AC3: Claude failing during generation alerts "generate post"."""
    rig.claude.fail_kind = "generate"
    assert await rig.pipeline.run() is None
    _assert_failed_cleanly(rig, alerts, STEP_GENERATE)


async def test_failure_in_the_safety_guard_classifier_call(
    rig: Rig, alerts: list[str]
) -> None:
    """AC2/AC3: a failing guard call is alerted once and releases the seed."""
    rig.claude.fail_kind = "guard"
    assert await rig.pipeline.run() is None
    assert rig.reservations() == {}
    assert len(alerts) == 1
    rig.assert_nothing_published()


async def test_failure_the_guard_rejects_every_draft(
    rig: Rig, alerts: list[str]
) -> None:
    """AC2/AC3: guard rejection is alerted once by the guard (naming it), seed released."""
    rig.claude.verdicts = [FAIL]
    assert await rig.pipeline.run() is None
    assert rig.claude.calls.count("generate") == MAX_GUARD_REGENERATIONS + 1
    assert len(alerts) == 1
    assert "guard" in alerts[0].lower()
    assert rig.reservations() == {}
    assert rig.store.list_pending() == []
    rig.assert_nothing_published()


async def test_failure_saving_the_draft(
    rig: Rig, alerts: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC2/AC3: a store failure alerts "save draft" and releases the seed."""

    def broken(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("disk full")

    monkeypatch.setattr(rig.store, "create", broken)
    assert await rig.pipeline.run() is None
    _assert_failed_cleanly(rig, alerts, STEP_SAVE)
    assert "disk full" in alerts[0]


async def test_failure_rekeying_the_reservation_discards_the_draft(
    rig: Rig, alerts: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC2: if the seed cannot be tied to the draft, the draft is binned, not left pending."""

    def broken(_old: str, _new: str) -> None:
        raise RuntimeError("reservation file locked")

    monkeypatch.setattr(rig.queue, "rekey", broken)
    assert await rig.pipeline.run() is None
    assert rig.reservations() == {}
    assert rig.store.list_pending() == []
    assert rig.store.get(1).status == STATUS_BINNED
    assert STEP_SAVE in alerts[0]
    rig.assert_nothing_published()


async def test_failure_sending_the_preview(rig: Rig, alerts: list[str]) -> None:
    """AC2/AC3: if the preview cannot be sent the draft is binned and the seed released."""
    rig.bot.fail_send = True
    assert await rig.pipeline.run() is None
    assert rig.reservations() == {}
    assert rig.store.get(1).status == STATUS_BINNED
    assert rig.store.list_pending() == []
    assert len(alerts) == 1
    assert STEP_PREVIEW in alerts[0]
    assert "telegram down" in alerts[0]
    rig.assert_nothing_published()


async def test_a_released_seed_is_used_by_the_next_run(
    rig: Rig, alerts: list[str]
) -> None:
    """AC2: after a failed run the same seed is first in line again."""
    rig.claude.fail_kind = "generate"
    await rig.pipeline.run()
    rig.claude.fail_kind = None
    draft = await rig.pipeline.run()
    assert draft is not None
    assert draft.seed == "First seed"


async def test_a_failed_release_is_alerted_too(
    rig: Rig, alerts: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """ERR-01: if releasing the seed fails as well, Stuart is told about both."""
    rig.claude.fail_kind = "generate"

    def broken(_key: str) -> bool:
        raise RuntimeError("lock stuck")

    monkeypatch.setattr(rig.queue, "bin_draft", broken)
    assert await rig.pipeline.run() is None
    assert len(alerts) == 2
    assert any("release seed" in text and "lock stuck" in text for text in alerts)
    assert any(STEP_GENERATE in text for text in alerts)


async def test_a_failed_provisional_release_does_not_skip_the_draft_id_release(
    rig: Rig, alerts: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The release under the draft id still runs when the provisional one fails."""
    rig.bot.fail_send = True
    released: list[str] = []
    real = rig.queue.bin_draft

    def flaky(key: str) -> bool:
        released.append(key)
        if len(released) == 1:
            raise RuntimeError("lock stuck")
        return real(key)

    monkeypatch.setattr(rig.queue, "bin_draft", flaky)
    assert await rig.pipeline.run() is None
    assert len(released) == 2
    assert rig.reservations() == {}
    assert any("release seed" in text and "lock stuck" in text for text in alerts)


async def test_a_gate_failure_is_alerted_not_skipped_silently(
    rig: Rig, alerts: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """ERR-01: a broken pending-count check is an alert, not a quiet skip."""

    def broken() -> int:
        raise RuntimeError("db gone")

    monkeypatch.setattr(rig.store, "pending_count", broken)
    assert await rig.pipeline.run() is None
    assert len(alerts) == 1
    assert "db gone" in alerts[0]
    assert rig.claude.calls == []


async def test_cancellation_releases_the_seed_and_propagates(rig: Rig) -> None:
    """Shutdown during a run does not strand the seed reservation."""
    started = asyncio.Event()

    async def slow(_seed: str, _steer: str | None) -> Any:
        started.set()
        await asyncio.sleep(3600)

    rig.pipeline._generate = slow
    task = asyncio.create_task(rig.pipeline.run())
    async with asyncio.timeout(5):
        await started.wait()
    assert any(k.startswith(PROVISIONAL_PREFIX) for k in rig.reservations())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert rig.reservations() == {}


async def test_the_pipeline_never_calls_the_publisher(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hard rule: nothing publishes without Approve; the run has no publisher at all."""
    assert not hasattr(rig.pipeline, "_publisher")
    called: list[str] = []
    monkeypatch.setattr(
        rig.publisher, "create_draft", lambda *_a, **_k: called.append("create")
    )
    await rig.pipeline.run()
    assert called == []


async def test_a_failure_to_discard_the_draft_does_not_hide_the_rekey_error(
    rig: Rig, alerts: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """PR-Agent: if binning fails too, the re-key error is still reported."""

    def broken_rekey(_old: str, _new: str) -> None:
        raise RuntimeError("reservation file locked")

    def broken_bin(_draft_id: int) -> None:
        raise RuntimeError("db locked")

    monkeypatch.setattr(rig.queue, "rekey", broken_rekey)
    monkeypatch.setattr(rig.store, "mark_binned", broken_bin)
    assert await rig.pipeline.run() is None
    assert any("reservation file locked" in text for text in alerts)
    assert any("discard draft" in text and "db locked" in text for text in alerts)
    assert rig.reservations() == {}


async def test_cancelling_during_the_draft_write_still_bins_the_late_draft(
    rig: Rig,
) -> None:
    """PR-Agent: a thread cannot be interrupted, so a draft written after the
    cancel is found and binned, and the seed is released."""
    import threading
    import time

    inside = threading.Event()
    real_create = rig.store.create

    def slow_create(*args: Any, **kwargs: Any) -> Draft:
        inside.set()
        time.sleep(0.2)
        return real_create(*args, **kwargs)

    rig.store.create = slow_create  # type: ignore[method-assign]
    task = asyncio.create_task(rig.pipeline.run())
    async with asyncio.timeout(5):
        while not inside.is_set():
            await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert rig.reservations() == {}
    assert rig.store.list_pending() == []
    assert rig.store.get(1).status == STATUS_BINNED


async def test_a_failed_bin_after_a_failed_rekey_is_alerted_and_names_the_draft(
    rig: Rig, alerts: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """PR-Agent: a draft that cannot be binned is reported, never silently stuck."""

    def broken_rekey(_old: str, _new: str) -> None:
        raise RuntimeError("locked")

    def broken_bin(_draft_id: int) -> None:
        raise RuntimeError("db locked")

    monkeypatch.setattr(rig.queue, "rekey", broken_rekey)
    monkeypatch.setattr(rig.store, "mark_binned", broken_bin)
    assert await rig.pipeline.run() is None
    assert rig.store.get(1).status == STATUS_PENDING  # still there, and alerted
    assert any("discard draft" in text for text in alerts)
