"""Turns a seed (plus an optional steer) into a structured post via Claude."""

import logging
import re
from dataclasses import dataclass

from humblebrag.claude_client import ClaudeClient
from humblebrag.config import Config
from humblebrag.json_reply import JsonReplyError, parse_json_object
from humblebrag.persona import Persona

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 2
MIN_GENERATION_TOKENS = 4096
TOKENS_PER_WORD_ALLOWANCE = 3
JSON_OVERHEAD_TOKENS = 512
REQUIRED_FIELDS = ("title", "subtitle", "body_markdown", "note_teaser")
MAX_TEASER_CHARS = 280
MAX_TEASER_SENTENCES = 2
MIN_COPIED_WORDS = 4
_SENTENCE_BREAK = re.compile(r"(?<=[.!?])[\"')\]]*\s+")
_HASHTAG = re.compile(r"(?:^|\s)#\w")
_SIGN_OFF = re.compile(r"\bagree\s*\?", re.IGNORECASE)
_NON_WORD = re.compile(r"[\W_]+")
_LINK = re.compile(
    r"\[[^\]]*\]\([^)]*\)|^\s*\[[^\]]+\]:\s*\S+\s*(?:[\"'(].*)?$|https?://|www\.|<a\b",
    re.IGNORECASE | re.MULTILINE,
)
_IMAGE = re.compile(r"!\[[^\]]*\](?:\(|\[)|<img\b", re.IGNORECASE)


class PostGenerationError(Exception):
    """Raised when no valid post could be produced; messages hold no secrets."""


class MalformedPostError(ValueError):
    """The model's reply is not a valid post (internal; triggers the retry)."""


@dataclass(frozen=True)
class GeneratedPost:
    """A generated Substack post and its Note teaser (text only, never posted
    by the agent). No cover image."""

    title: str
    subtitle: str
    body_markdown: str
    note_teaser: str


def _build_prompt(
    seed: str, steer: str | None, min_words: int, max_words: int, problem: str | None
) -> str:
    """Build the user prompt: seed, optional steer, length and JSON format."""
    parts = [
        "Write today's post from this seed.",
        f"<seed>\n{seed}\n</seed>",
    ]
    if steer:
        parts.append(f"Steer for this version:\n<steer>\n{steer}\n</steer>")
    parts.append(
        "Format override (this replaces the persona's Format section on "
        "length and title placement): reply with ONLY a JSON object with the "
        'string keys "title", "subtitle", "body_markdown" and "note_teaser". '
        f"body_markdown must be between {min_words} and {max_words} words, "
        "must not repeat the title, and must contain no links and no images. "
        f"note_teaser is a witty teaser for the post: at most "
        f"{MAX_TEASER_SENTENCES} sentences and {MAX_TEASER_CHARS} characters, "
        "in your voice, in your own words (never a sentence copied from the "
        'body), with no links, no images, no hashtags and no "Agree?" sign-off.'
    )
    if problem:
        parts.append(f"Your previous reply was rejected: {problem}. Fix it.")
    return "\n\n".join(parts)


def _repeats_title(body: str, title: str) -> bool:
    """True if the body's first line is just the title (optionally a heading)."""
    first_line = body.lstrip().split("\n", 1)[0].lstrip("#").strip()
    return first_line.casefold() == title.casefold()


def _normalise(text: str) -> str:
    """Lower-case ``text`` and collapse punctuation and spacing, for comparing."""
    return _NON_WORD.sub(" ", text.casefold()).strip()


def _copies_body(teaser: str, body: str) -> bool:
    """True if a sentence of the teaser (of MIN_COPIED_WORDS or more) is in the body."""
    flat_body = f" {_normalise(body)} "
    for sentence in _SENTENCE_BREAK.split(teaser):
        words = _normalise(sentence)
        if len(words.split()) >= MIN_COPIED_WORDS and f" {words} " in flat_body:
            return True
    return False


def _check_teaser(teaser: str, body: str) -> None:
    """Raise MalformedPostError unless ``teaser`` meets the teaser rules."""
    if len(teaser) > MAX_TEASER_CHARS:
        raise MalformedPostError(
            f"note_teaser has {len(teaser)} characters, expected at most "
            f"{MAX_TEASER_CHARS}"
        )
    if len(_SENTENCE_BREAK.split(teaser)) > MAX_TEASER_SENTENCES:
        raise MalformedPostError(
            f"note_teaser has more than {MAX_TEASER_SENTENCES} sentences"
        )
    if _IMAGE.search(teaser):
        raise MalformedPostError("note_teaser contains an image")
    if _LINK.search(teaser):
        raise MalformedPostError("note_teaser contains a link")
    if _HASHTAG.search(teaser):
        raise MalformedPostError("note_teaser contains a hashtag")
    if _SIGN_OFF.search(teaser):
        raise MalformedPostError('note_teaser ends with an "Agree?" sign-off')
    if _copies_body(teaser, body):
        raise MalformedPostError("note_teaser copies a sentence from the body")


def _parse_post(reply: str, min_words: int, max_words: int) -> GeneratedPost:
    """Validate the model's reply as a post, or raise MalformedPostError."""
    try:
        data = parse_json_object(reply)
    except JsonReplyError as error:
        raise MalformedPostError(str(error)) from None
    values: dict[str, str] = {}
    for name in REQUIRED_FIELDS:
        value = data.get(name)
        if not isinstance(value, str) or not value.strip():
            raise MalformedPostError(f"{name} is missing or not a non-empty string")
        values[name] = value.strip()
    body = values["body_markdown"]
    if _repeats_title(body, values["title"]):
        raise MalformedPostError("body_markdown repeats the title")
    if _IMAGE.search(body):
        raise MalformedPostError("body_markdown contains an image")
    if _LINK.search(body):
        raise MalformedPostError("body_markdown contains a link")
    word_count = len(body.split())
    if not min_words <= word_count <= max_words:
        raise MalformedPostError(
            f"body_markdown has {word_count} words, expected {min_words} to {max_words}"
        )
    _check_teaser(values["note_teaser"], body)
    return GeneratedPost(**values)


class PostGenerator:
    """Generates posts with the persona as system prompt."""

    def __init__(self, client: ClaudeClient, persona: Persona, config: Config) -> None:
        """Keep the client, persona and the configured word range."""
        self._client = client
        self._persona = persona
        self._min_words = config.min_post_words
        self._max_words = config.max_post_words
        self._max_tokens = max(
            MIN_GENERATION_TOKENS,
            config.max_post_words * TOKENS_PER_WORD_ALLOWANCE + JSON_OVERHEAD_TOKENS,
        )

    async def generate(self, seed: str, steer: str | None = None) -> GeneratedPost:
        """Return a post for ``seed``; retry once on a malformed reply.

        Raises PostGenerationError if both attempts are malformed. Claude API
        failures propagate as ClaudeError for the caller to alert on.
        """
        problem: str | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            prompt = _build_prompt(
                seed, steer, self._min_words, self._max_words, problem
            )
            reply = await self._client.complete(
                self._persona.text, prompt, self._max_tokens
            )
            try:
                return _parse_post(reply, self._min_words, self._max_words)
            except MalformedPostError as error:
                problem = str(error)
                logger.warning("Malformed post (attempt %d): %s", attempt, problem)
        raise PostGenerationError(
            f"No valid post after {MAX_ATTEMPTS} attempts; last problem: {problem}"
        )
