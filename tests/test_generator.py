"""Tests for the post generator (SA-23 acceptance criteria)."""

import dataclasses
import json
from typing import Any

import pytest

from humblebrag import generator as generator_module
from humblebrag.claude_client import ClaudeAPIError
from humblebrag.config import Config, load_config
from humblebrag.generator import (
    JSON_OVERHEAD_TOKENS,
    MAX_ATTEMPTS,
    MIN_GENERATION_TOKENS,
    TOKENS_PER_WORD_ALLOWANCE,
    GeneratedPost,
    PostGenerationError,
    PostGenerator,
)
from humblebrag.persona import Persona

ENV = {
    "TELEGRAM_BOT_TOKEN": "123456:TESTTOKEN",
    "TELEGRAM_CHAT_ID": "1",
    "ANTHROPIC_API_KEY": "sk-ant-SECRET-key",
    "CLAUDE_MODEL": "claude-test-model",
    "SUBSTACK_PUBLICATION_URL": "https://example.substack.com",
    "SUBSTACK_COOKIE": "cookie",
}
PERSONA = Persona(version=1, text="You are Sir Humblebrag.")
SEED = "I spilled my coffee."
STEER = "make it about trains"


class StubClient:
    """Replays canned replies and records every call to ``complete``."""

    def __init__(self, replies: list[str | Exception]) -> None:
        """Replay ``replies`` in order, raising any that are exceptions."""
        self._replies = list(replies)
        self.calls: list[dict[str, Any]] = []

    async def complete(self, system: str, prompt: str, max_tokens: int) -> str:
        """Record the call and return or raise the next canned reply."""
        self.calls.append(
            {"system": system, "prompt": prompt, "max_tokens": max_tokens}
        )
        reply = self._replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def _body(words: int) -> str:
    """A body of exactly ``words`` words."""
    return " ".join(["humbled"] * words)


def _reply(words: int = 300, **overrides: Any) -> str:
    """A well-formed JSON reply whose body has ``words`` words."""
    data: dict[str, Any] = {
        "title": "Coffee, Leadership and Me",
        "subtitle": "A humble reflection",
        "body_markdown": _body(words),
    }
    data.update(overrides)
    return json.dumps(data)


def _generator(client: StubClient, config: Config | None = None) -> PostGenerator:
    """A generator wired to a stub client."""
    cfg = config or load_config(ENV)
    return PostGenerator(client, PERSONA, cfg)  # type: ignore[arg-type]


async def test_input_is_seed_and_optional_steer() -> None:
    """AC1: the seed and steer reach the prompt; the steer is optional."""
    with_steer = StubClient([_reply()])
    await _generator(with_steer).generate(SEED, STEER)
    prompt = with_steer.calls[0]["prompt"]
    assert SEED in prompt
    assert STEER in prompt
    assert with_steer.calls[0]["system"] == PERSONA.text

    without_steer = StubClient([_reply()])
    await _generator(without_steer).generate(SEED)
    assert SEED in without_steer.calls[0]["prompt"]
    assert "<steer>" not in without_steer.calls[0]["prompt"]


async def test_output_has_title_subtitle_and_body() -> None:
    """AC2: structured JSON becomes a post with the three fields."""
    post = await _generator(StubClient([_reply(300)])).generate(SEED)
    assert post == GeneratedPost(
        title="Coffee, Leadership and Me",
        subtitle="A humble reflection",
        body_markdown=_body(300),
    )


async def test_json_in_a_code_fence_is_accepted() -> None:
    """AC2: a fenced JSON reply still parses."""
    post = await _generator(StubClient([f"```json\n{_reply()}\n```"])).generate(SEED)
    assert post.title == "Coffee, Leadership and Me"


@pytest.mark.parametrize(
    "bad",
    [
        "Not JSON at all",
        "[1, 2, 3]",
        json.dumps({"title": "T", "subtitle": "S"}),
        _reply(title=""),
        _reply(subtitle=7),
    ],
)
async def test_malformed_reply_is_retried_once_then_succeeds(bad: str) -> None:
    """AC3: one malformed reply triggers exactly one retry."""
    client = StubClient([bad, _reply()])
    post = await _generator(client).generate(SEED)
    assert len(client.calls) == 2
    assert "rejected" in client.calls[1]["prompt"]
    assert post.title == "Coffee, Leadership and Me"


async def test_two_malformed_replies_raise_typed_error() -> None:
    """AC3: after the single retry the failure is a typed error."""
    client = StubClient(["nope", "still nope"])
    with pytest.raises(PostGenerationError, match="2 attempts"):
        await _generator(client).generate(SEED)
    assert len(client.calls) == MAX_ATTEMPTS == 2


async def test_claude_errors_are_not_swallowed_or_retried() -> None:
    """AC3: API failures propagate as their own typed error, untouched."""
    client = StubClient([ClaudeAPIError("boom", status_code=500)])
    with pytest.raises(ClaudeAPIError):
        await _generator(client).generate(SEED)
    assert len(client.calls) == 1


@pytest.mark.parametrize("words", [250, 600])
async def test_default_range_accepts_the_limits(words: int) -> None:
    """AC4: 250 and 600 words are both inside the default range."""
    post = await _generator(StubClient([_reply(words)])).generate(SEED)
    assert len(post.body_markdown.split()) == words


@pytest.mark.parametrize("words", [249, 601])
async def test_default_range_rejects_just_outside(words: int) -> None:
    """AC4: 249 and 601 words fail both attempts."""
    client = StubClient([_reply(words), _reply(words)])
    with pytest.raises(PostGenerationError, match=f"{words} words"):
        await _generator(client).generate(SEED)


async def test_out_of_range_then_in_range_retries() -> None:
    """AC4: a length miss is retried like any malformed reply."""
    client = StubClient([_reply(100), _reply(300)])
    post = await _generator(client).generate(SEED)
    assert len(post.body_markdown.split()) == 300


async def test_range_is_configurable_and_stated_in_the_prompt() -> None:
    """AC4: custom limits are enforced and told to the model."""
    config = load_config({**ENV, "POST_MIN_WORDS": "10", "POST_MAX_WORDS": "20"})
    client = StubClient([_reply(15), _reply(21), _reply(21)])
    generator = _generator(client, config)
    await generator.generate(SEED)
    assert "between 10 and 20 words" in client.calls[0]["prompt"]
    with pytest.raises(PostGenerationError):
        await generator.generate(SEED)


@pytest.mark.parametrize(
    "image",
    ["![cover](http://x/y.png)", "![cover][ref]", '<IMG src="http://x/y.png">'],
)
async def test_body_with_an_image_is_rejected(image: str) -> None:
    """AC5: no cover images, so any image syntax is malformed."""
    with_image = _reply(body_markdown=_body(300) + " " + image)
    client = StubClient([with_image, _reply()])
    post = await _generator(client).generate(SEED)
    assert "image" in client.calls[1]["prompt"]
    assert post.body_markdown == _body(300)


async def test_extra_keys_are_dropped_so_only_a_post_comes_out() -> None:
    """AC5: a cover image or note in the JSON never reaches the result."""
    reply = _reply(cover_image="http://x/y.png", note="a Substack Note")
    post = await _generator(StubClient([reply])).generate(SEED)
    assert [f.name for f in dataclasses.fields(post)] == [
        "title",
        "subtitle",
        "body_markdown",
    ]


def test_module_offers_posts_only() -> None:
    """AC5: the public surface has no Notes or cover image generation."""
    public = {n.lower() for n in dir(generator_module) if not n.startswith("_")}
    assert not any("note" in n or "cover" in n or "image" in n for n in public)


@pytest.mark.parametrize(
    "link",
    [
        "[here](http://x.com)",
        "[here][ref]\n\n[ref]: example.com",
        '\n\n[ref]: example.com "A title"',
        "see https://x.com now",
        "see www.x.com now",
        '<a href="http://x.com">here</a>',
    ],
)
async def test_body_with_a_link_is_rejected(link: str) -> None:
    """The prompt forbids links, so any link syntax is malformed."""
    client = StubClient([_reply(body_markdown=_body(300) + " " + link), _reply()])
    post = await _generator(client).generate(SEED)
    assert "link" in client.calls[1]["prompt"]
    assert post.body_markdown == _body(300)


async def test_token_budget_has_a_floor_and_grows_with_the_word_range() -> None:
    """Large word ranges get a larger token budget so JSON is not cut off."""
    small = StubClient([_reply()])
    await _generator(small).generate(SEED)
    assert small.calls[0]["max_tokens"] == MIN_GENERATION_TOKENS

    config = load_config({**ENV, "POST_MIN_WORDS": "10", "POST_MAX_WORDS": "5000"})
    large = StubClient([_reply(20)])
    await _generator(large, config).generate(SEED)
    assert large.calls[0]["max_tokens"] == (
        5000 * TOKENS_PER_WORD_ALLOWANCE + JSON_OVERHEAD_TOKENS
    )


@pytest.mark.parametrize("footnote", ["[1][2]", "\n\n[1]: see page 5"])
async def test_footnote_style_text_is_not_a_link(footnote: str) -> None:
    """Near-miss: footnote markers are not links, so they must not retry."""
    client = StubClient([_reply(body_markdown=_body(300) + footnote)])
    post = await _generator(client).generate(SEED)
    assert len(client.calls) == 1
    assert post.body_markdown.endswith(footnote.strip())
