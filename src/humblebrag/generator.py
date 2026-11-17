"""Turns a seed (plus an optional steer) into a structured post via Claude."""

import json
import logging
import re
from dataclasses import dataclass

from humblebrag.claude_client import ClaudeClient
from humblebrag.config import Config
from humblebrag.persona import Persona

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 2
GENERATION_MAX_TOKENS = 4096
REQUIRED_FIELDS = ("title", "subtitle", "body_markdown")
_FENCE = re.compile(r"\A```(?:json)?\s*(?P<inner>.*?)\s*```\Z", re.DOTALL)
_MARKDOWN_IMAGE = re.compile(r"!\[[^\]]*\]\(")


class PostGenerationError(Exception):
    """Raised when no valid post could be produced; messages hold no secrets."""


class MalformedPostError(ValueError):
    """The model's reply is not a valid post (internal; triggers the retry)."""


@dataclass(frozen=True)
class GeneratedPost:
    """A generated Substack post. Posts only: no Notes, no cover image."""

    title: str
    subtitle: str
    body_markdown: str


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
        'string keys "title", "subtitle" and "body_markdown". '
        f"body_markdown must be between {min_words} and {max_words} words, "
        "must not repeat the title, and must contain no links and no images."
    )
    if problem:
        parts.append(f"Your previous reply was rejected: {problem}. Fix it.")
    return "\n\n".join(parts)


def _parse_post(reply: str, min_words: int, max_words: int) -> GeneratedPost:
    """Validate the model's reply as a post, or raise MalformedPostError."""
    fenced = _FENCE.match(reply.strip())
    text = fenced["inner"] if fenced else reply
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        raise MalformedPostError("reply is not valid JSON") from None
    if not isinstance(data, dict):
        raise MalformedPostError("reply is not a JSON object")
    values: dict[str, str] = {}
    for name in REQUIRED_FIELDS:
        value = data.get(name)
        if not isinstance(value, str) or not value.strip():
            raise MalformedPostError(f"{name} is missing or not a non-empty string")
        values[name] = value.strip()
    body = values["body_markdown"]
    if _MARKDOWN_IMAGE.search(body):
        raise MalformedPostError("body_markdown contains an image")
    word_count = len(body.split())
    if not min_words <= word_count <= max_words:
        raise MalformedPostError(
            f"body_markdown has {word_count} words, expected {min_words} to {max_words}"
        )
    return GeneratedPost(**values)


class PostGenerator:
    """Generates posts with the persona as system prompt."""

    def __init__(self, client: ClaudeClient, persona: Persona, config: Config) -> None:
        """Keep the client, persona and the configured word range."""
        self._client = client
        self._persona = persona
        self._min_words = config.min_post_words
        self._max_words = config.max_post_words

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
                self._persona.text, prompt, GENERATION_MAX_TOKENS
            )
            try:
                return _parse_post(reply, self._min_words, self._max_words)
            except MalformedPostError as error:
                problem = str(error)
                logger.warning("Malformed post (attempt %d): %s", attempt, problem)
        raise PostGenerationError(
            f"No valid post after {MAX_ATTEMPTS} attempts; last problem: {problem}"
        )
