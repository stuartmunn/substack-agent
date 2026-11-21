"""Tests for adding a replied-with image to a Substack draft (SA-50).

No network: the Substack API and Telegram are fakes. The fake API forbids every
call but the three the feature may use, so a publish call fails the test.
"""

import json
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from substack.exceptions import SubstackAPIException
from telegram.ext import Application, MessageHandler

from humblebrag.alerts import configure_alerts
from humblebrag.config import load_config
from humblebrag.drafts import STATUS_PENDING, DraftStore
from humblebrag.image_reply import (
    ADDED_TEXT,
    BAD_TYPE_TEXT,
    FAILED_TEXT,
    HOW_TO_TEXT,
    MAX_IMAGE_BYTES,
    NOT_EDITABLE_TEXT,
    TOO_BIG_TEXT,
    ImageReplyService,
    install_image_reply,
)
from humblebrag.publisher import (
    DraftError,
    DraftNotEditableError,
    SubstackPublisher,
    with_header_image,
)

CHAT = 12345
EDITOR = "https://humblebrag.substack.com/publish/post/5001"
IMAGE_URL = "https://substackcdn.com/image/new.png"
OLD_URL = "https://substackcdn.com/image/old.png"
PROMPT_MESSAGE = 77
ENV = {
    "TELEGRAM_BOT_TOKEN": "123456:TESTTOKEN",
    "TELEGRAM_CHAT_ID": str(CHAT),
    "ANTHROPIC_API_KEY": "sk-ant-key",
    "CLAUDE_MODEL": "claude-test-model",
    "SUBSTACK_PUBLICATION_URL": "https://humblebrag.substack.com",
    "SUBSTACK_COOKIE": "s%3ASECRET-SID",
}
PNG = b"\x89PNG\r\n\x1a\n" + b"data"
JPEG = b"\xff\xd8\xff\xe0" + b"data"
WEBP = b"RIFF\x00\x00\x00\x00WEBPdata"
PARAGRAPH = {"type": "paragraph", "content": [{"type": "text", "text": "Hello"}]}
HEADING = {"type": "heading", "attrs": {"level": 2}, "content": []}


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
    """The only Substack calls allowed are get_draft, get_image and put_draft."""

    def __init__(self, draft: dict[str, Any] | None = None) -> None:
        """A draft whose body is a heading then a paragraph, unless given."""
        body = {"type": "doc", "content": [HEADING, PARAGRAPH]}
        self.draft = draft or {
            "id": 5001,
            "draft_title": "The Title",
            "draft_subtitle": "The Sub",
            "draft_body": json.dumps(body),
            "post_date": None,
            "is_published": False,
        }
        self.calls: list[tuple[str, tuple, dict]] = []
        self.get_error: Exception | None = None
        self.upload_error: Exception | None = None
        self.put_error: Exception | None = None
        self.upload_reply: Any = {"url": IMAGE_URL}
        self.uploaded: list[bytes] = []

    def get_draft(self, draft_id: Any) -> dict[str, Any]:
        """Return the draft, or raise the configured error."""
        self.calls.append(("get_draft", (draft_id,), {}))
        if self.get_error:
            raise self.get_error
        return self.draft

    def get_image(self, path: str) -> Any:
        """Record the upload (its bytes, since the file is temporary)."""
        self.calls.append(("get_image", (path,), {}))
        if self.upload_error:
            raise self.upload_error
        self.uploaded.append(Path(path).read_bytes())
        return self.upload_reply

    def put_draft(self, draft_id: Any, **kwargs: Any) -> dict[str, Any]:
        """Record a field update, or raise the configured error."""
        self.calls.append(("put_draft", (draft_id,), kwargs))
        if self.put_error:
            raise self.put_error
        return {}

    def names(self) -> list[str]:
        """The names of the calls made, in order."""
        return [name for name, _a, _k in self.calls]

    def __getattr__(self, name: str) -> Any:
        """Publishing, scheduling, Notes and the rest are forbidden."""
        raise AssertionError(f"the agent must never call {name}")


def _image_block(url: str) -> dict[str, Any]:
    """A header image block like the one the feature writes."""
    return with_header_image({"content": []}, url)["content"][0]


def _blocks(api: FakeApi) -> list[dict[str, Any]]:
    """The blocks of the body the last put_draft sent."""
    sent = [kwargs for name, _a, kwargs in api.calls if name == "put_draft"][-1]
    return json.loads(sent["draft_body"])["content"]


def _publisher(api: FakeApi) -> SubstackPublisher:
    """A real publisher over the fake API."""
    return SubstackPublisher(load_config(ENV), lambda **_k: api)


# with_header_image: the body edit itself


def test_an_image_is_inserted_as_the_first_block_and_the_rest_is_kept() -> None:
    """AC1/AC8: the image goes first; every other block is untouched."""
    body = {"type": "doc", "content": [HEADING, PARAGRAPH], "extra": 1}
    result = with_header_image(body, IMAGE_URL)
    assert result["content"][1:] == [HEADING, PARAGRAPH]
    assert result["content"][0]["type"] == "captionedImage"
    assert result["content"][0]["content"][0]["attrs"]["src"] == IMAGE_URL
    assert result["extra"] == 1
    assert body["content"] == [HEADING, PARAGRAPH]  # the input is not changed


def test_an_existing_header_image_is_replaced_not_added_to() -> None:
    """AC3: a second image replaces the first, so there is only one."""
    body = {"type": "doc", "content": [_image_block(OLD_URL), HEADING, PARAGRAPH]}
    result = with_header_image(body, IMAGE_URL)
    assert len(result["content"]) == 3
    assert result["content"][0]["content"][0]["attrs"]["src"] == IMAGE_URL
    assert result["content"][1:] == [HEADING, PARAGRAPH]


def test_an_image_is_added_to_an_empty_body() -> None:
    """An empty body just gets the image."""
    assert len(with_header_image({"content": []}, IMAGE_URL)["content"]) == 1


# the publisher


async def test_the_publisher_uploads_then_writes_only_the_body(tmp_path: Path) -> None:
    """AC1/AC8: read, upload, then put only draft_body; no publish call."""
    api = FakeApi()
    path = tmp_path / "pic.png"
    path.write_bytes(PNG)
    await _publisher(api).add_header_image("5001", str(path))
    assert api.names() == ["get_draft", "get_image", "put_draft"]
    [put] = [kwargs for name, _a, kwargs in api.calls if name == "put_draft"]
    assert list(put) == ["draft_body"]  # title and subtitle are not sent
    assert _blocks(api)[1:] == [HEADING, PARAGRAPH]
    assert api.uploaded == [PNG]


async def test_the_publisher_replaces_an_existing_header_image(tmp_path: Path) -> None:
    """AC3: with an image already first, the new one takes its place."""
    api = FakeApi()
    body = {"type": "doc", "content": [_image_block(OLD_URL), HEADING]}
    api.draft["draft_body"] = json.dumps(body)
    path = tmp_path / "pic.png"
    path.write_bytes(PNG)
    await _publisher(api).add_header_image("5001", str(path))
    blocks = _blocks(api)
    assert len(blocks) == 2
    assert blocks[0]["content"][0]["attrs"]["src"] == IMAGE_URL


@pytest.mark.parametrize(
    "state", [{"is_published": True}, {"post_date": "2026-11-22T09:00:00Z"}]
)
async def test_a_published_or_scheduled_draft_is_not_changed(
    tmp_path: Path, state: dict[str, Any]
) -> None:
    """AC5: nothing is uploaded or written."""
    api = FakeApi()
    api.draft.update(state)
    with pytest.raises(DraftNotEditableError):
        await _publisher(api).add_header_image("5001", str(tmp_path / "x.png"))
    assert api.names() == ["get_draft"]


async def test_a_deleted_draft_is_not_changed(tmp_path: Path) -> None:
    """AC5: a 404 from Substack means the draft is gone."""
    api = FakeApi()
    api.get_error = SubstackAPIException(404, "{}")
    with pytest.raises(DraftNotEditableError):
        await _publisher(api).add_header_image("5001", str(tmp_path / "x.png"))
    assert api.names() == ["get_draft"]


@pytest.mark.parametrize(
    "stage", ["get_error", "upload_error", "put_error", "bad_url", "bad_body"]
)
async def test_any_failure_is_a_draft_error_and_the_body_is_not_written(
    tmp_path: Path, stage: str
) -> None:
    """AC7: every failure is typed; an upload or read failure never writes."""
    api = FakeApi()
    if stage == "bad_url":
        api.upload_reply = {"url": "javascript:alert(1)"}
    elif stage == "bad_body":
        api.draft["draft_body"] = "not json"
    else:
        setattr(api, stage, SubstackAPIException(500, "boom"))
    path = tmp_path / "pic.png"
    path.write_bytes(PNG)
    with pytest.raises(DraftError) as raised:
        await _publisher(api).add_header_image("5001", str(path))
    assert not isinstance(raised.value, DraftNotEditableError)
    assert "Nothing was published" in str(raised.value)
    if stage != "put_error":
        assert "put_draft" not in api.names()


# the Telegram handler


class FakeTelegramFile:
    """A Telegram file that writes fixed bytes to disk."""

    def __init__(self, data: bytes) -> None:
        """Hold the bytes to write."""
        self.data = data
        self.paths: list[Path] = []

    async def download_to_drive(self, path: Path) -> None:
        """Write the bytes to ``path``."""
        self.paths.append(path)
        path.write_bytes(self.data)


class FakeBot:
    """Serves one fake file."""

    def __init__(self, data: bytes) -> None:
        """Hold the file to serve."""
        self.file = FakeTelegramFile(data)
        self.requested: list[str] = []

    async def get_file(self, file_id: str) -> FakeTelegramFile:
        """Return the file."""
        self.requested.append(file_id)
        return self.file


class Reply:
    """A message that records what the bot replies."""

    def __init__(self, **fields: Any) -> None:
        """Set the message's fields."""
        self.photo = fields.get("photo")
        self.document = fields.get("document")
        self.reply_to_message = fields.get("reply_to_message")
        self.sent: list[str] = []

    async def reply_text(self, text: str) -> None:
        """Record a reply."""
        self.sent.append(text)


def _document(mime: str = "image/png", size: int = 1000) -> Any:
    """A Telegram document."""
    return SimpleNamespace(mime_type=mime, file_size=size, file_id="f1")


class Rig:
    """A service over a real store and publisher, with a handed-over draft."""

    def __init__(self, tmp_path: Path, data: bytes = PNG) -> None:
        """One draft that is handed over, with its image prompt message saved."""
        self.api = FakeApi()
        self.store = DraftStore(tmp_path / "d.db")
        draft = self.store.create("seed", "The Title", "The Sub", "# Body")
        self.store.mark_handed_over(draft.id, "5001", EDITOR)
        self.store.set_image_prompt_message_id(draft.id, PROMPT_MESSAGE)
        self.draft = draft
        self.service = ImageReplyService(self.store, _publisher(self.api), CHAT)
        self.bot = FakeBot(data)

    async def send(self, message: Reply, chat: int = CHAT) -> Reply:
        """Deliver ``message`` to the handler."""
        update = SimpleNamespace(
            effective_message=message, effective_chat=SimpleNamespace(id=chat)
        )
        await self.service.on_image(update, SimpleNamespace(bot=self.bot))  # type: ignore[arg-type]
        return message

    def to_prompt(self, **fields: Any) -> Reply:
        """A message that replies to the image prompt."""
        return Reply(
            reply_to_message=SimpleNamespace(message_id=PROMPT_MESSAGE), **fields
        )


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    """A fresh rig."""
    return Rig(tmp_path)


async def test_a_reply_with_an_image_file_adds_it_and_links_the_draft(rig: Rig) -> None:
    """AC1/AC2: the image is uploaded and inserted; Stuart gets the editor link."""
    message = await rig.send(rig.to_prompt(document=_document()))
    assert message.sent == [ADDED_TEXT.format(url=EDITOR)]
    assert rig.api.names() == ["get_draft", "get_image", "put_draft"]
    assert rig.api.uploaded == [PNG]
    assert _blocks(rig.api)[0]["type"] == "captionedImage"


async def test_a_compressed_photo_is_accepted_as_jpeg(tmp_path: Path) -> None:
    """AC1: the largest size of a photo is used."""
    rig = Rig(tmp_path, JPEG)
    small = SimpleNamespace(file_id="small", file_size=10)
    large = SimpleNamespace(file_id="large", file_size=500)
    message = await rig.send(rig.to_prompt(photo=[small, large]))
    assert rig.bot.requested == ["large"]
    assert message.sent == [ADDED_TEXT.format(url=EDITOR)]


async def test_a_webp_file_is_accepted(tmp_path: Path) -> None:
    """AC6: WebP is allowed."""
    rig = Rig(tmp_path, WEBP)
    message = await rig.send(rig.to_prompt(document=_document("image/webp")))
    assert message.sent == [ADDED_TEXT.format(url=EDITOR)]


async def test_a_second_image_replaces_the_first(rig: Rig) -> None:
    """AC3: sending another image leaves one header image, the newest."""
    await rig.send(rig.to_prompt(document=_document()))
    rig.api.draft["draft_body"] = json.dumps(
        {"type": "doc", "content": _blocks(rig.api)}
    )
    rig.api.upload_reply = {"url": OLD_URL}
    await rig.send(rig.to_prompt(document=_document()))
    blocks = _blocks(rig.api)
    assert len(blocks) == 3
    assert blocks[0]["content"][0]["attrs"]["src"] == OLD_URL


async def test_an_image_that_is_not_a_reply_is_not_matched_to_a_draft(
    rig: Rig,
) -> None:
    """AC4: no reply means no guess; Stuart is told how to use it."""
    message = await rig.send(Reply(document=_document()))
    assert message.sent == [HOW_TO_TEXT]
    assert rig.api.calls == []


async def test_a_reply_to_some_other_message_is_not_matched(rig: Rig) -> None:
    """AC4: a reply to a message that is not an image prompt does nothing."""
    other = Reply(
        document=_document(), reply_to_message=SimpleNamespace(message_id=999)
    )
    await rig.send(other)
    assert other.sent == [HOW_TO_TEXT]
    assert rig.api.calls == []


async def test_a_pending_draft_is_not_given_an_image(rig: Rig) -> None:
    """A draft that is not handed over has no Substack draft to edit."""
    pending = rig.store.create("seed 2", "T", "S", "B")
    rig.store.set_image_prompt_message_id(pending.id, 88)
    message = Reply(
        document=_document(), reply_to_message=SimpleNamespace(message_id=88)
    )
    await rig.send(message)
    assert pending.status == STATUS_PENDING
    assert message.sent == [HOW_TO_TEXT]
    assert rig.api.calls == []


async def test_a_published_draft_replies_that_it_cannot_and_changes_nothing(
    rig: Rig,
) -> None:
    """AC5/AC12: the reply says so; nothing is uploaded or written."""
    rig.api.draft["is_published"] = True
    message = await rig.send(rig.to_prompt(document=_document()))
    assert message.sent == [NOT_EDITABLE_TEXT]
    assert rig.api.names() == ["get_draft"]


@pytest.mark.parametrize("mime", ["image/gif", "application/pdf", "text/plain"])
async def test_a_file_of_the_wrong_type_is_rejected(rig: Rig, mime: str) -> None:
    """AC6: only JPEG, PNG and WebP are accepted."""
    message = await rig.send(rig.to_prompt(document=_document(mime)))
    assert message.sent == [BAD_TYPE_TEXT]
    assert rig.api.calls == []


async def test_a_message_with_no_image_is_rejected(rig: Rig) -> None:
    """AC6: a reply carrying neither a photo nor a document is refused."""
    message = await rig.send(rig.to_prompt())
    assert message.sent == [BAD_TYPE_TEXT]


async def test_a_file_over_10mb_is_rejected_before_it_is_downloaded(rig: Rig) -> None:
    """AC6: the size Telegram reports is checked first."""
    message = await rig.send(
        rig.to_prompt(document=_document(size=MAX_IMAGE_BYTES + 1))
    )
    assert message.sent == [TOO_BIG_TEXT]
    assert rig.bot.requested == []


async def test_a_file_that_is_bigger_than_it_claimed_is_rejected(
    tmp_path: Path,
) -> None:
    """AC6: the downloaded size is checked too."""
    rig = Rig(tmp_path, PNG + b"x" * MAX_IMAGE_BYTES)
    message = await rig.send(rig.to_prompt(document=_document()))
    assert message.sent == [TOO_BIG_TEXT]
    assert rig.api.calls == []


async def test_a_file_that_is_not_the_type_it_says_is_rejected(tmp_path: Path) -> None:
    """AC6: a PDF renamed to PNG fails the content check."""
    rig = Rig(tmp_path, b"%PDF-1.7 not an image")
    message = await rig.send(rig.to_prompt(document=_document()))
    assert message.sent == [BAD_TYPE_TEXT]
    assert rig.api.calls == []


async def test_a_riff_file_that_is_not_webp_is_rejected(tmp_path: Path) -> None:
    """A WAV file starts with RIFF too, so the WEBP mark is checked."""
    rig = Rig(tmp_path, b"RIFF\x00\x00\x00\x00WAVEdata")
    message = await rig.send(rig.to_prompt(document=_document("image/webp")))
    assert message.sent == [BAD_TYPE_TEXT]


@pytest.mark.parametrize("stage", ["get_error", "upload_error", "put_error"])
async def test_a_failure_alerts_and_tells_stuart_the_draft_is_unchanged(
    rig: Rig, alerts: list[str], stage: str
) -> None:
    """AC7/TST-04: any upload or edit failure alerts; the body is not changed."""
    setattr(rig.api, stage, SubstackAPIException(500, "boom"))
    message = await rig.send(rig.to_prompt(document=_document()))
    [alert_text] = alerts
    assert "add the header image" in alert_text
    assert str(rig.draft.id) in alert_text
    assert message.sent[0].startswith(FAILED_TEXT[:20])
    if stage != "put_error":
        assert "put_draft" not in rig.api.names()


async def test_a_session_failure_alerts_and_the_draft_is_unchanged(
    rig: Rig, alerts: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC7: an expired session is alerted and nothing is written."""

    def expired(**_k: Any) -> Any:
        raise SubstackAPIException(401, "{}")

    rig.service._publisher._api_factory = expired
    message = await rig.send(rig.to_prompt(document=_document()))
    assert rig.api.calls == []
    assert any("session expired" in text.lower() for text in alerts)
    assert message.sent[0].startswith("Could not add the image")


async def test_a_draft_without_a_substack_id_alerts(
    rig: Rig, alerts: list[str]
) -> None:
    """A handed-over draft with no stored ID cannot be edited: say so, alert."""
    rig.store._execute("UPDATE drafts SET substack_draft_id = NULL")
    message = await rig.send(rig.to_prompt(document=_document()))
    assert len(alerts) == 1
    assert message.sent[0].startswith("Could not add the image")
    assert rig.api.calls == []


async def test_the_temporary_file_is_deleted_after_success_and_failure(
    rig: Rig,
) -> None:
    """AC10: the downloaded file is gone either way."""
    await rig.send(rig.to_prompt(document=_document()))
    rig.api.put_error = SubstackAPIException(500, "boom")
    await rig.send(rig.to_prompt(document=_document()))
    assert len(rig.bot.file.paths) == 2
    assert not any(path.exists() or path.parent.exists() for path in rig.bot.file.paths)


async def test_only_the_allowed_chat_is_served(rig: Rig) -> None:
    """AC9/SEC-14: an image from another chat is ignored without a reply."""
    message = await rig.send(rig.to_prompt(document=_document()), chat=999)
    assert message.sent == []
    assert rig.api.calls == []


async def test_the_agent_makes_no_publish_call_end_to_end(rig: Rig) -> None:
    """TST-05: the whole flow touches only get_draft, get_image and put_draft."""
    await rig.send(rig.to_prompt(document=_document()))
    assert set(rig.api.names()) <= {"get_draft", "get_image", "put_draft"}


def test_install_registers_a_handler_for_images_and_files(tmp_path: Path) -> None:
    """The service is wired in as a message handler."""
    application = Application.builder().token("123456:TESTTOKEN").build()
    rig = Rig(tmp_path)
    install_image_reply(application, rig.service)
    handlers = [h for group in application.handlers.values() for h in group]
    assert any(isinstance(h, MessageHandler) for h in handlers)
