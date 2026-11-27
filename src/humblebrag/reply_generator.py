"""Writes a Sir Humblebrag reply to a fetched or pasted Substack item via Claude.

The system prompt is the persona plus ``prompts/reply.md``, which overrides only
the post format; the persona's safety rules still apply. Every reply is
validated here and then judged by the safety guard (see ``reply``).
"""

import logging
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from humblebrag.claude_client import ClaudeClient, Flow
from humblebrag.fetcher import TargetType
from humblebrag.persona import Persona, load_persona

logger = logging.getLogger(__name__)

DEFAULT_REPLY_PROMPT_PATH = Path("prompts/reply.md")
SYSTEM_SEPARATOR = "\n\n---\n\n"
MAX_ATTEMPTS = 2
SHORT_WORDS = (20, 80)
SHORT_BULLETS = 2
LONG_WORDS = (80, 150)
LONG_BULLETS = 3
MIN_HASHTAGS = 3
MAX_HASHTAGS = 6
SIGN_OFF = "Agree?"
EMOJI_CATEGORY = "So"
TARGET_TAG = "target"
TITLE_TAG = "parent_title"
STEER_TAG = "steer"
_SENTENCE_BREAK = re.compile(r"(?<=[.!?])[\"')\]]*\s+")
_HEADING = re.compile(r"^\s*#{1,6}\s")
_HASHTAG = re.compile(r"#\w+")
_HASHTAG_LINE = re.compile(r"^\s*#\w+(?:\s+#\w+)*\s*$")
_LINK = re.compile(
    r"\[[^\]]*\]\([^)]*\)|^\s*\[[^\]]+\]:\s*\S+\s*(?:[\"'(].*)?$|https?://|www\.|<a\b",
    re.IGNORECASE | re.MULTILINE,
)
_IMAGE = re.compile(r"!\[[^\]]*\](?:\(|\[)|<img\b", re.IGNORECASE)
_MENTION = re.compile(r"(?:^|[^\w])@\w")
_TEXT_BULLET = re.compile(r"^\s*[-*•]\s")


class ReplyGenerationError(Exception):
    """No valid reply could be produced; messages hold no secrets."""


class MalformedReplyError(ValueError):
    """The model's reply breaks a format rule (internal; triggers the retry)."""


@dataclass(frozen=True)
class ReplyTarget:
    """What is being answered: its kind, its text, and a comment's post title."""

    kind: TargetType
    text: str
    parent_title: str | None = None


def neutralise_tag(text: str, tag: str) -> str:
    """Remove any copy of the ``tag`` delimiter so text cannot close it."""
    pattern = re.compile(rf"<\s*/?\s*{re.escape(tag)}\b[^>]*>?", re.IGNORECASE)
    return pattern.sub(f"[{tag} tag removed]", text)


def neutralise_all_tags(text: str) -> str:
    """Neutralise every delimiter this prompt uses, in untrusted text."""
    for tag in (TARGET_TAG, TITLE_TAG, STEER_TAG):
        text = neutralise_tag(text, tag)
    return text


def build_reply_prompt(
    target: ReplyTarget, steer: str | None, problem: str | None
) -> str:
    """Build the user prompt from the target, optional steer and any problem."""
    parts = [f"Write your reply to this {target.kind.value}."]
    if target.parent_title:
        title = neutralise_all_tags(target.parent_title)
        parts.append(f"<{TITLE_TAG}>\n{title}\n</{TITLE_TAG}>")
    parts.append(f"<{TARGET_TAG}>\n{neutralise_all_tags(target.text)}\n</{TARGET_TAG}>")
    if steer:
        parts.append(
            f"Steer for this version:\n<{STEER_TAG}>\n"
            f"{neutralise_all_tags(steer)}\n</{STEER_TAG}>"
        )
    if problem:
        parts.append(f"Your previous reply was rejected: {problem}. Fix it.")
    return "\n\n".join(parts)


def _is_bullet(line: str) -> bool:
    """True for an emoji or dash bullet line."""
    stripped = line.strip()
    if not stripped:
        return False
    if _TEXT_BULLET.match(line):
        return True
    return unicodedata.category(stripped[0]) == EMOJI_CATEGORY


def _check_ending(lines: list[str], text: str) -> None:
    """Raise unless the reply ends with "Agree?" or a hashtag pile, never both."""
    last = lines[-1].strip()
    hashtags = _HASHTAG.findall(text)
    if last == SIGN_OFF:
        if hashtags:
            raise MalformedReplyError('it has hashtags as well as "Agree?"')
        return
    if SIGN_OFF.lower() in text.lower():
        raise MalformedReplyError('"Agree?" must be the whole last line, on its own')
    if not _HASHTAG_LINE.match(last):
        raise MalformedReplyError('it must end with "Agree?" or a line of hashtags')
    if not MIN_HASHTAGS <= len(hashtags) <= MAX_HASHTAGS:
        raise MalformedReplyError(
            f"it has {len(hashtags)} hashtags, expected {MIN_HASHTAGS} to {MAX_HASHTAGS}"
        )
    if len(_HASHTAG.findall("\n".join(lines[:-1]))) > 0:
        raise MalformedReplyError("hashtags may only be on the last line")


def check_reply(text: str, kind: TargetType) -> str:
    """Return the tidied reply, or raise MalformedReplyError naming the problem."""
    reply = text.strip()
    if not reply:
        raise MalformedReplyError("it is empty")
    lines = [line.rstrip() for line in reply.splitlines() if line.strip()]
    if _HEADING.match(lines[0]):
        raise MalformedReplyError("it has a title or heading")
    if _IMAGE.search(reply):
        raise MalformedReplyError("it contains an image")
    if _LINK.search(reply):
        raise MalformedReplyError("it contains a link")
    if _MENTION.search(reply):
        raise MalformedReplyError("it contains an @mention")
    for line in lines:
        if len(_SENTENCE_BREAK.split(line.strip())) > 1:
            raise MalformedReplyError("a line has more than one sentence")
    (low, high), max_bullets = (
        (LONG_WORDS, LONG_BULLETS)
        if kind is TargetType.POST
        else (SHORT_WORDS, SHORT_BULLETS)
    )
    words = len(reply.split())
    if not low <= words <= high:
        raise MalformedReplyError(f"it has {words} words, expected {low} to {high}")
    bullets = sum(_is_bullet(line) for line in lines)
    if bullets > max_bullets:
        raise MalformedReplyError(
            f"it has {bullets} bullets, expected at most {max_bullets}"
        )
    _check_ending(lines, reply)
    return "\n".join(lines)


class ReplyGenerator:
    """Generates replies with the persona plus the reply prompt as system prompt."""

    def __init__(
        self,
        client: ClaudeClient,
        persona: Persona,
        reply_prompt: Persona | None = None,
    ) -> None:
        """Keep the client and build the combined system prompt."""
        self._client = client
        prompt = reply_prompt or load_persona(DEFAULT_REPLY_PROMPT_PATH)
        self._system = persona.text + SYSTEM_SEPARATOR + prompt.text

    async def generate(self, target: ReplyTarget, steer: str | None = None) -> str:
        """Return a reply to ``target``; retry once on a malformed reply.

        Raises ReplyGenerationError if both attempts are malformed. Claude API
        failures propagate as ClaudeError for the caller to alert on.
        """
        problem: str | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            prompt = build_reply_prompt(target, steer, problem)
            raw = await self._client.complete(self._system, prompt, Flow.REPLY)
            try:
                return check_reply(raw, target.kind)
            except MalformedReplyError as error:
                problem = str(error)
                logger.warning("Malformed reply (attempt %d): %s", attempt, problem)
        raise ReplyGenerationError(
            f"No valid reply after {MAX_ATTEMPTS} attempts; last problem: {problem}"
        )
