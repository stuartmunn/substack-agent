"""Reply to the image prompt with a picture: it becomes the draft's header image.

Stuart replies to the Gemini prompt message (best as a file, because Telegram
compresses photos). The bot checks the reply, downloads the picture to a
temporary file, and asks the publisher to put it first in that draft's body.
It only ever edits the draft; it never publishes. A picture that is not a reply
to an image prompt is never matched to a draft by guessing.
"""

import asyncio
import logging
import tempfile
from pathlib import Path
from typing import Any

from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters

from humblebrag.alerts import alert, describe_error
from humblebrag.drafts import STATUS_HANDED_OVER, DraftStore
from humblebrag.publisher import DraftNotEditableError, SubstackPublisher

logger = logging.getLogger(__name__)

ALERT_EVENT = "image reply"
MAX_IMAGE_BYTES = 10 * 1024 * 1024
# Allowed image types: MIME type -> (file extension, the file's first bytes).
IMAGE_TYPES: dict[str, tuple[str, bytes]] = {
    "image/jpeg": (".jpg", b"\xff\xd8\xff"),
    "image/png": (".png", b"\x89PNG\r\n\x1a\n"),
    "image/webp": (".webp", b"RIFF"),
}
WEBP_MARK = b"WEBP"
WEBP_MARK_OFFSET = 8
PHOTO_MIME_TYPE = "image/jpeg"
WEBP_MIME_TYPE = "image/webp"
HOW_TO_TEXT = (
    "To add an image, reply to the image prompt message with the picture. "
    "Send it as a file, not a photo, so Telegram does not compress it."
)
BAD_TYPE_TEXT = "That is not a JPEG, PNG or WebP image, so I did not use it."
TOO_BIG_TEXT = "That image is over 10 MB, so I did not use it."
NOT_EDITABLE_TEXT = (
    "I cannot add the image: the Substack draft is gone, scheduled or published. "
    "Nothing was changed."
)
FAILED_TEXT = "Could not add the image, the draft is unchanged. {detail}"
ADDED_TEXT = "Image added. {url}"
STEP_ADD = "add the header image"


class ImageReplyService:
    """Handles pictures sent as a reply to an image prompt."""

    def __init__(
        self, store: DraftStore, publisher: SubstackPublisher, chat_id: int
    ) -> None:
        """Hold the draft store, the publisher and the one allowed chat."""
        self._store = store
        self._publisher = publisher
        self._chat_id = chat_id
        self._lock = asyncio.Lock()  # one image edit at a time

    async def on_image(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Add a replied-with picture to its draft, or say why not."""
        message = update.effective_message
        chat = update.effective_chat
        if message is None or chat is None or chat.id != self._chat_id:  # SEC-14
            return
        replied = message.reply_to_message
        draft = (
            None
            if replied is None
            else await asyncio.to_thread(
                self._store.find_by_image_prompt_message, replied.message_id
            )
        )
        if draft is None or draft.status != STATUS_HANDED_OVER:
            await message.reply_text(HOW_TO_TEXT)
            return
        source = _image_source(message)
        if source is None:
            await message.reply_text(BAD_TYPE_TEXT)
            return
        mime_type, file_size, file_id = source
        if file_size is not None and file_size > MAX_IMAGE_BYTES:
            await message.reply_text(TOO_BIG_TEXT)
            return
        async with self._lock:
            await self._add(
                message, context, draft.id, draft.substack_draft_id, mime_type, file_id
            )

    async def _add(
        self,
        message: Any,
        context: ContextTypes.DEFAULT_TYPE,
        draft_id: int,
        substack_id: str | None,
        mime_type: str,
        file_id: str,
    ) -> None:
        """Download to a temporary file, add it, reply; the file is always removed."""
        if not substack_id:
            await self._fail(message, draft_id, "the draft has no Substack ID")
            return
        try:
            with tempfile.TemporaryDirectory() as folder:  # deleted on exit (AC10)
                path = Path(folder) / f"image{IMAGE_TYPES[mime_type][0]}"
                telegram_file = await context.bot.get_file(file_id)
                await telegram_file.download_to_drive(path)
                problem = _check_file(path, mime_type)
                if problem is not None:
                    await message.reply_text(problem)
                    return
                await self._publisher.add_header_image(substack_id, str(path))
        except DraftNotEditableError:
            logger.warning("Draft %d cannot take an image", draft_id)
            await message.reply_text(NOT_EDITABLE_TEXT)
            return
        except Exception as error:  # never leave Stuart without an answer
            logger.exception("Step '%s' failed for draft %d", STEP_ADD, draft_id)
            await self._fail(message, draft_id, describe_error(error))
            return
        await message.reply_text(
            ADDED_TEXT.format(url=self._publisher.editor_url(substack_id))
        )

    @staticmethod
    async def _fail(message: Any, draft_id: int, detail: str) -> None:
        """Alert Stuart, tell him the draft is unchanged."""
        await alert(
            ALERT_EVENT, f"Step '{STEP_ADD}' failed for draft {draft_id}: {detail}"
        )
        await message.reply_text(FAILED_TEXT.format(detail=detail))


def _image_source(message: Any) -> tuple[str, int | None, str] | None:
    """(MIME type, size, file id) of the message's photo or image file, if allowed."""
    if message.photo:
        best = message.photo[-1]  # Telegram lists the sizes smallest first
        return PHOTO_MIME_TYPE, best.file_size, best.file_id
    document = message.document
    if document is not None and document.mime_type in IMAGE_TYPES:
        return document.mime_type, document.file_size, document.file_id
    return None


def _check_file(path: Path, mime_type: str) -> str | None:
    """A message to send if the downloaded file is too big or not the type it says."""
    if path.stat().st_size > MAX_IMAGE_BYTES:
        return TOO_BIG_TEXT
    with path.open("rb") as file:
        head = file.read(WEBP_MARK_OFFSET + len(WEBP_MARK))
    expected = IMAGE_TYPES[mime_type][1]
    if not head.startswith(expected):
        return BAD_TYPE_TEXT
    if mime_type == WEBP_MIME_TYPE and head[WEBP_MARK_OFFSET:] != WEBP_MARK:
        return BAD_TYPE_TEXT
    return None


def install_image_reply(application: Application, service: ImageReplyService) -> None:
    """Wire photos and image files into the service."""
    application.add_handler(
        MessageHandler(filters.PHOTO | filters.Document.ALL, service.on_image)
    )
