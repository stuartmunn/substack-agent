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
    MAX_TEASER_CHARS,
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
SCENE = "Sir Humblebrag III ponders a spilled coffee in a grand library."
TEASER = "A cancelled train made me a locomotive of leadership. I am humbled."


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
        "note_teaser": TEASER,
        "image_scene": SCENE,
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


async def test_output_has_title_subtitle_body_and_teaser() -> None:
    """AC2 and SA-47 AC1: structured JSON becomes a post with all four fields."""
    post = await _generator(StubClient([_reply(300)])).generate(SEED)
    assert post == GeneratedPost(
        title="Coffee, Leadership and Me",
        subtitle="A humble reflection",
        body_markdown=_body(300),
        note_teaser=TEASER,
        image_scene=SCENE,
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
        "note_teaser",
        "image_scene",
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


@pytest.mark.parametrize(
    "opening", ["# Coffee, Leadership and Me", "coffee, leadership and me"]
)
async def test_body_repeating_the_title_is_rejected(opening: str) -> None:
    """The prompt forbids repeating the title, so a repeat is malformed."""
    repeated = _reply(body_markdown=f"{opening}\n\n{_body(300)}")
    client = StubClient([repeated, _reply()])
    post = await _generator(client).generate(SEED)
    assert "title" in client.calls[1]["prompt"]
    assert post.body_markdown == _body(300)


async def test_title_words_inside_the_body_are_fine() -> None:
    """Near-miss: mentioning the title in a longer line is not a repeat."""
    body = "Coffee, Leadership and Me is my favourite " + _body(300)
    client = StubClient([_reply(body_markdown=body)])
    await _generator(client).generate(SEED)
    assert len(client.calls) == 1


# SA-47: note teaser

BAD_TEASERS = {
    "missing": None,
    "empty": "",
    "blank": "   ",
    "not a string": 7,
    "too long": "A" + "b" * 280 + ".",
    "three sentences": "One. Two. Three.",
    "markdown link": "Read [more](https://example.com) now.",
    "bare link": "See https://example.com for more.",
    "image": "Look ![pic](x.png) here.",
    "hashtag": "Humbled again. #Leadership",
    "sign-off": "Humbled again. Agree?",
    "copied sentence": "I am humbled by this extraordinary coffee based tragedy.",
}


def _reply_with_teaser(teaser: Any) -> str:
    """A reply whose body holds a known sentence and whose teaser is ``teaser``."""
    data = json.loads(_reply())
    data["body_markdown"] = (
        "I am humbled by this extraordinary coffee based tragedy.\n" + _body(300)
    )
    if teaser is None:
        del data["note_teaser"]
    else:
        data["note_teaser"] = teaser
    return json.dumps(data)


@pytest.mark.parametrize("teaser", BAD_TEASERS.values(), ids=BAD_TEASERS.keys())
async def test_a_bad_teaser_is_retried_once_then_succeeds(teaser: Any) -> None:
    """SA-47 AC4: each rejection case takes the existing retry path."""
    client = StubClient([_reply_with_teaser(teaser), _reply()])
    post = await _generator(client).generate(SEED)
    assert len(client.calls) == 2
    assert "note_teaser" in client.calls[1]["prompt"]
    assert post.note_teaser == TEASER


@pytest.mark.parametrize("teaser", BAD_TEASERS.values(), ids=BAD_TEASERS.keys())
async def test_a_teaser_that_fails_twice_raises_post_generation_error(
    teaser: Any,
) -> None:
    """SA-47 AC4: if the retry also fails, PostGenerationError is raised."""
    client = StubClient([_reply_with_teaser(teaser)] * 2)
    with pytest.raises(PostGenerationError):
        await _generator(client).generate(SEED)


@pytest.mark.parametrize(
    "teaser",
    [
        "Short and sweet.",
        "A" * 279 + ".",
        "What a day! I am humbled.",
        "I spilled coffee and found leadership.",
    ],
)
async def test_valid_teasers_are_accepted(teaser: str) -> None:
    """SA-47 AC2: up to two sentences and 280 characters pass."""
    post = await _generator(StubClient([_reply_with_teaser(teaser)])).generate(SEED)
    assert post.note_teaser == teaser


async def test_the_teaser_is_stripped() -> None:
    """A teaser with stray whitespace is stored trimmed."""
    post = await _generator(StubClient([_reply(note_teaser=f"  {TEASER}\n")])).generate(
        SEED
    )
    assert post.note_teaser == TEASER


async def test_the_prompt_describes_the_teaser_rules() -> None:
    """SA-47 AC1/AC2/AC3: the format override asks for the field and its limits."""
    client = StubClient([_reply()])
    await _generator(client).generate(SEED)
    prompt = client.calls[0]["prompt"]
    assert '"note_teaser"' in prompt
    assert "280 characters" in prompt
    assert "2 sentences" in prompt
    assert "hashtags" in prompt


async def test_the_output_budget_has_headroom_for_the_teaser() -> None:
    """SA-47 risk: the token budget still covers the maximum body plus a teaser."""
    config = load_config(ENV)
    client = StubClient([_reply()])
    await _generator(client, config).generate(SEED)
    teaser_tokens = MAX_TEASER_CHARS  # a token is at least one character
    body_tokens = config.max_post_words * TOKENS_PER_WORD_ALLOWANCE
    assert client.calls[0]["max_tokens"] >= body_tokens + teaser_tokens


# SA-49: the image scene

BAD_SCENES: dict[str, Any] = {
    "missing": None,
    "empty": "   ",
    "not a string": 7,
    "too long": "S" * 301,
    "link": "He reads https://example.com in a library.",
    "markdown link": "He reads [a book](http://x.y) in a library.",
    "image": "He stands before ![a pic](http://x.y/z.png) in a library.",
}


def _reply_with_scene(scene: Any) -> str:
    """A reply whose image scene is ``scene`` (or missing if None)."""
    data = json.loads(_reply())
    if scene is None:
        del data["image_scene"]
    else:
        data["image_scene"] = scene
    return json.dumps(data)


@pytest.mark.parametrize("scene", BAD_SCENES.values(), ids=BAD_SCENES.keys())
async def test_a_bad_scene_is_retried_once_then_succeeds(scene: Any) -> None:
    """SA-49 AC2: each rejection case takes the existing retry path."""
    client = StubClient([_reply_with_scene(scene), _reply()])
    post = await _generator(client).generate(SEED)
    assert len(client.calls) == 2
    assert "image_scene" in client.calls[1]["prompt"]
    assert post.image_scene == SCENE


@pytest.mark.parametrize("scene", BAD_SCENES.values(), ids=BAD_SCENES.keys())
async def test_a_scene_that_fails_twice_raises_post_generation_error(
    scene: Any,
) -> None:
    """SA-49 AC2: if the retry also fails, PostGenerationError is raised."""
    client = StubClient([_reply_with_scene(scene)] * 2)
    with pytest.raises(PostGenerationError):
        await _generator(client).generate(SEED)


@pytest.mark.parametrize("scene", ["Short.", "S" * 300, "He reads a book."])
async def test_valid_scenes_are_accepted_and_stripped(scene: str) -> None:
    """SA-49 AC1/AC2: up to 300 characters pass, with whitespace trimmed."""
    post = await _generator(StubClient([_reply(image_scene=f" {scene}\n")])).generate(
        SEED
    )
    assert post.image_scene == scene.strip()


async def test_the_prompt_describes_the_scene_rules() -> None:
    """SA-49 AC1: the format override asks for the field and its limits."""
    client = StubClient([_reply()])
    await _generator(client).generate(SEED)
    prompt = client.calls[0]["prompt"]
    assert '"image_scene"' in prompt
    assert "300 characters" in prompt
    assert "no real people, brands, logos or text" in prompt
