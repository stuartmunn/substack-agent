"""Tests for the Claude API client (SA-21 acceptance criteria).

Every test mocks the HTTP transport, so the SDK's real retry logic runs but
nothing touches the network.
"""

import json
import logging
from collections.abc import Callable, Iterator
from typing import Any

import anthropic
import anyio
import httpx2
import pytest

from humblebrag.claude_client import (
    ERROR_TEXT_PREVIEW_CHARS,
    MAX_RETRIES,
    REQUEST_TIMEOUT_SECONDS,
    ClaudeAPIError,
    ClaudeClient,
    ClaudeError,
    ClaudeRateLimitError,
    ClaudeResponseError,
    ClaudeTimeoutError,
    Flow,
)
from humblebrag.config import Config, load_config
from humblebrag.logging_setup import redactor

API_KEY = "sk-ant-SECRET-key"
MODEL = "claude-test-model-from-env"
ENV = {
    "TELEGRAM_BOT_TOKEN": "123456:TESTTOKEN",
    "TELEGRAM_CHAT_ID": "1",
    "ANTHROPIC_API_KEY": API_KEY,
    "CLAUDE_MODEL": MODEL,
    "SUBSTACK_PUBLICATION_URL": "https://example.substack.com",
    "SUBSTACK_COOKIE": "cookie",
}
Handler = Callable[[httpx2.Request], httpx2.Response]


@pytest.fixture
def config() -> Config:
    """A valid config naming the test model."""
    return load_config(ENV)


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[float]]:
    """Replace the SDK's retry sleep with a recorder, so tests are instant."""
    delays: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        delays.append(seconds)

    monkeypatch.setattr(anyio, "sleep", fake_sleep)
    yield delays


def _message(
    text: str = "Humbled to announce my toast.",
    stop_reason: str = "end_turn",
    content: list[dict[str, Any]] | None = None,
    stop_details: dict[str, Any] | None = None,
) -> httpx2.Response:
    """A well-formed Messages API response."""
    body: dict[str, Any] = {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": MODEL,
        "content": content if content is not None else [{"type": "text", "text": text}],
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": 3, "output_tokens": 5},
    }
    if stop_details is not None:
        body["stop_details"] = stop_details
    return httpx2.Response(200, json=body)


def _error(status: int, kind: str, message: str = "nope") -> httpx2.Response:
    """An API error response with a request id header."""
    return httpx2.Response(
        status,
        headers={"request-id": "req_test123"},
        json={"type": "error", "error": {"type": kind, "message": message}},
    )


class Recorder:
    """A mock transport handler that records requests and replays responses."""

    def __init__(self, responses: list[Callable[[httpx2.Request], httpx2.Response]]):
        """Replay ``responses`` in order, repeating the last one."""
        self._responses = responses
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        """Record the request and produce the next response (or raise)."""
        self.requests.append(request)
        index = min(len(self.requests) - 1, len(self._responses) - 1)
        return self._responses[index](request)


def _client(config: Config, handler: Handler) -> ClaudeClient:
    """A ClaudeClient wired to a mock transport."""
    http_client = anthropic.DefaultAsyncHttpxClient(
        transport=httpx2.MockTransport(handler)
    )
    return ClaudeClient(config, http_client=http_client)


def _ok(response: httpx2.Response) -> Callable[[httpx2.Request], httpx2.Response]:
    """Wrap a fixed response as a handler step."""
    return lambda _request: response


def _timeout(request: httpx2.Request) -> httpx2.Response:
    """A handler step that simulates a read timeout."""
    raise httpx2.ReadTimeout("timed out", request=request)


async def test_reply_text_is_returned(config: Config, sleeps: list[float]) -> None:
    """A normal reply comes back as plain text."""
    recorder = Recorder([_ok(_message("Hello there"))])
    text = await _client(config, recorder).complete(
        "be funny", "write a post", Flow.REPLY
    )
    assert text == "Hello there"
    assert sleeps == []


async def test_model_comes_from_config_and_request_is_well_formed(
    config: Config, sleeps: list[float]
) -> None:
    """AC1: the model in the request is CLAUDE_MODEL; no sampling parameters that
    current models reject are sent, and thinking is explicitly disabled (SA-56 AC6)."""
    recorder = Recorder([_ok(_message())])
    await _client(config, recorder).complete("be funny", "write a post", Flow.REPLY)
    body = json.loads(recorder.requests[0].content)
    assert body["model"] == MODEL
    assert body["max_tokens"] == config.reply_max_tokens
    assert body["thinking"] == {"type": "disabled"}
    assert body["system"] == "be funny"
    assert body["messages"] == [{"role": "user", "content": "write a post"}]
    for rejected in ("temperature", "top_p", "top_k", "budget_tokens"):
        assert rejected not in body
    assert recorder.requests[0].headers["authorization"] == f"Bearer {API_KEY}"


async def test_a_different_configured_model_is_used(sleeps: list[float]) -> None:
    """AC1: changing CLAUDE_MODEL changes the request, with no code change."""
    other = load_config({**ENV, "CLAUDE_MODEL": "another-model"})
    recorder = Recorder([_ok(_message())])
    await _client(other, recorder).complete("s", "p", Flow.REPLY)
    assert json.loads(recorder.requests[0].content)["model"] == "another-model"


async def test_rate_limit_is_retried_then_succeeds(
    config: Config, sleeps: list[float]
) -> None:
    """AC2: two 429s then success means three requests and a good result."""
    limited = _ok(_error(429, "rate_limit_error"))
    recorder = Recorder([limited, limited, _ok(_message("finally"))])
    assert await _client(config, recorder).complete("s", "p", Flow.REPLY) == "finally"
    assert len(recorder.requests) == 3
    assert len(sleeps) == 2


async def test_timeout_is_retried_then_succeeds(
    config: Config, sleeps: list[float]
) -> None:
    """AC2: a timeout followed by success is retried."""
    recorder = Recorder([_timeout, _ok(_message("recovered"))])
    assert await _client(config, recorder).complete("s", "p", Flow.REPLY) == "recovered"
    assert len(recorder.requests) == 2


async def test_retries_are_capped_at_three_with_growing_backoff(
    config: Config, sleeps: list[float]
) -> None:
    """AC2: persistent timeouts give 1 try + 3 retries, waiting longer each time."""
    recorder = Recorder([_timeout])
    with pytest.raises(ClaudeTimeoutError):
        await _client(config, recorder).complete("s", "p", Flow.REPLY)
    assert MAX_RETRIES == 3
    assert len(recorder.requests) == MAX_RETRIES + 1
    assert len(sleeps) == MAX_RETRIES
    assert sleeps[0] < sleeps[1] < sleeps[2]


async def test_persistent_rate_limit_raises_a_typed_error(
    config: Config, sleeps: list[float]
) -> None:
    """AC2/AC3: after the last retry a 429 becomes ClaudeRateLimitError."""
    recorder = Recorder([_ok(_error(429, "rate_limit_error"))])
    with pytest.raises(ClaudeRateLimitError) as error:
        await _client(config, recorder).complete("s", "p", Flow.REPLY)
    assert len(recorder.requests) == MAX_RETRIES + 1
    assert isinstance(error.value, ClaudeError)
    assert "req_test123" in str(error.value)
    assert isinstance(error.value.__cause__, anthropic.RateLimitError)


async def test_persistent_timeout_raises_a_typed_error(
    config: Config, sleeps: list[float]
) -> None:
    """AC3: exhausted timeouts raise ClaudeTimeoutError, chained to the cause."""
    with pytest.raises(ClaudeTimeoutError) as error:
        await _client(config, Recorder([_timeout])).complete("s", "p", Flow.REPLY)
    assert isinstance(error.value, ClaudeError)
    assert isinstance(error.value.__cause__, anthropic.APITimeoutError)


async def test_client_errors_are_not_retried(
    config: Config, sleeps: list[float]
) -> None:
    """AC3: a 400 fails at once as ClaudeAPIError carrying the status."""
    recorder = Recorder([_ok(_error(400, "invalid_request_error"))])
    with pytest.raises(ClaudeAPIError) as error:
        await _client(config, recorder).complete("s", "p", Flow.REPLY)
    assert len(recorder.requests) == 1
    assert sleeps == []
    assert error.value.status_code == 400
    assert "req_test123" in str(error.value)


async def test_api_error_detail_is_logged_and_in_the_message(
    config: Config,
    sleeps: list[float],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """SA-58 AC1/AC6: a 400's type and message reach the log and the error text."""
    prompt = "SECRET-PROMPT-TEXT"
    recorder = Recorder(
        [_ok(_error(400, "invalid_request_error", "messages: text is bad"))]
    )
    with caplog.at_level(logging.DEBUG), pytest.raises(ClaudeAPIError) as error:
        await _client(config, recorder).complete("s", prompt, Flow.REPLY)
    text = str(error.value)
    ours = "\n".join(
        record.getMessage()
        for record in caplog.records
        if record.name == "humblebrag.claude_client"
    )
    assert "HTTP 400" in text
    assert "status=400" in ours
    for part in ("req_test123", "invalid_request_error", "text is bad"):
        assert part in text
        assert part in ours
    assert "flow reply" in text
    assert "max_tokens=" in ours
    assert prompt not in text
    assert prompt not in ours
    assert API_KEY not in ours


async def test_api_error_message_is_capped_at_200_characters(
    config: Config, sleeps: list[float]
) -> None:
    """SA-58 AC2: a long API message is cut to 200 characters."""
    recorder = Recorder([_ok(_error(400, "invalid_request_error", "x" * 500))])
    with pytest.raises(ClaudeAPIError) as error:
        await _client(config, recorder).complete("s", "p", Flow.REPLY)
    assert "x" * ERROR_TEXT_PREVIEW_CHARS in str(error.value)
    assert "x" * (ERROR_TEXT_PREVIEW_CHARS + 1) not in str(error.value)


async def test_api_error_message_is_redacted_before_it_is_capped(
    config: Config, sleeps: list[float]
) -> None:
    """SEC-14: a secret straddling the cap is masked whole, not cut in half."""
    secret = "sk-ant-STRADDLES-THE-CAP"
    padding = "y" * (ERROR_TEXT_PREVIEW_CHARS - 8)
    recorder = Recorder([_ok(_error(400, "invalid_request_error", padding + secret))])
    redactor.add([secret])
    try:
        with pytest.raises(ClaudeAPIError) as error:
            await _client(config, recorder).complete("s", "p", Flow.REPLY)
    finally:
        redactor.clear()
    assert secret not in str(error.value)
    assert "sk-ant" not in str(error.value)


async def test_api_error_without_a_body_reads_as_unknown(
    config: Config, sleeps: list[float]
) -> None:
    """SA-58 AC1: a status error with no JSON body still raises cleanly."""
    bare = httpx2.Response(400, headers={"request-id": "req_test123"}, text="oops")
    with pytest.raises(ClaudeAPIError, match="type unknown"):
        await _client(config, Recorder([_ok(bare)])).complete("s", "p", Flow.REPLY)


async def test_server_errors_are_retried_then_raise_a_typed_error(
    config: Config, sleeps: list[float]
) -> None:
    """AC3: a persistent 500 is retried, then raises ClaudeAPIError."""
    recorder = Recorder([_ok(_error(500, "api_error"))])
    with pytest.raises(ClaudeAPIError) as error:
        await _client(config, recorder).complete("s", "p", Flow.REPLY)
    assert len(recorder.requests) == MAX_RETRIES + 1
    assert error.value.status_code == 500


async def test_connection_failure_raises_a_typed_error(
    config: Config, sleeps: list[float]
) -> None:
    """AC3: a connection error becomes ClaudeAPIError with no status."""

    def refuse(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("refused", request=request)

    with pytest.raises(ClaudeAPIError) as error:
        await _client(config, Recorder([refuse])).complete("s", "p", Flow.REPLY)
    assert error.value.status_code is None


async def test_errors_never_contain_the_api_key(
    config: Config,
    sleeps: list[float],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """SEC-03: an auth failure's typed error and logs hold no secret."""
    recorder = Recorder([_ok(_error(401, "authentication_error", "invalid x-api-key"))])
    with caplog.at_level(logging.DEBUG), pytest.raises(ClaudeAPIError) as error:
        await _client(config, recorder).complete("s", "p", Flow.REPLY)
    assert error.value.status_code == 401
    assert API_KEY not in str(error.value)
    assert API_KEY not in caplog.text


async def test_refusal_is_a_typed_error_not_empty_text(
    config: Config, sleeps: list[float]
) -> None:
    """AC3: a safety refusal raises ClaudeResponseError naming the category."""
    refusal = _message(
        content=[],
        stop_reason="refusal",
        stop_details={"type": "refusal", "category": "cyber", "explanation": "x"},
    )
    with pytest.raises(ClaudeResponseError, match="cyber"):
        await _client(config, Recorder([_ok(refusal)])).complete("s", "p", Flow.REPLY)


async def test_truncated_reply_is_a_typed_error(
    config: Config, sleeps: list[float]
) -> None:
    """A reply cut off at max_tokens is not silently used as a finished post."""
    cut = _message("half a sent", stop_reason="max_tokens")
    with pytest.raises(ClaudeResponseError, match="max_tokens"):
        await _client(config, Recorder([_ok(cut)])).complete("s", "p", Flow.REPLY)


def _cut_off(text: str = "half a sent", output_tokens: int = 5) -> httpx2.Response:
    """A reply that stopped at max_tokens, reporting ``output_tokens``."""
    response = _message(text, stop_reason="max_tokens")
    body = json.loads(response.content)
    body["usage"]["output_tokens"] = output_tokens
    return httpx2.Response(200, json=body)


def _sent_limits(recorder: Recorder) -> list[int]:
    """The max_tokens of every request the recorder saw."""
    return [json.loads(request.content)["max_tokens"] for request in recorder.requests]


@pytest.mark.parametrize(
    ("flow", "setting", "value"),
    [
        (Flow.POST, "POST_MAX_TOKENS", 1111),
        (Flow.REPLY, "REPLY_MAX_TOKENS", 2222),
        (Flow.GUARD, "GUARD_MAX_TOKENS", 333),
        (Flow.SEED, "SEED_MAX_TOKENS", 44),
    ],
)
async def test_each_flow_sends_its_own_max_tokens(
    flow: Flow, setting: str, value: int, sleeps: list[float]
) -> None:
    """SA-56 AC3: each flow's limit comes from its own setting."""
    recorder = Recorder([_ok(_message())])
    client = _client(load_config({**ENV, setting: str(value)}), recorder)
    await client.complete("s", "p", flow)
    assert _sent_limits(recorder) == [value]


async def test_the_ceiling_caps_every_flow(sleeps: list[float]) -> None:
    """SA-56 AC4: a setting above CLAUDE_MAX_TOKENS_CEILING is sent as the ceiling."""
    config = load_config(
        {**ENV, "POST_MAX_TOKENS": "9000", "CLAUDE_MAX_TOKENS_CEILING": "5000"}
    )
    recorder = Recorder([_ok(_message())])
    await _client(config, recorder).complete("s", "p", Flow.POST)
    assert _sent_limits(recorder) == [5000]


async def test_a_max_tokens_stop_is_retried_once_at_double(
    config: Config, sleeps: list[float]
) -> None:
    """SA-56 AC5: a cut-off reply is retried once at 2x and the retry's text used."""
    recorder = Recorder([_ok(_cut_off()), _ok(_message("whole reply"))])
    text = await _client(config, recorder).complete("s", "p", Flow.REPLY)
    assert text == "whole reply"
    assert _sent_limits(recorder) == [
        config.reply_max_tokens,
        config.reply_max_tokens * 2,
    ]


async def test_the_retry_is_capped_by_the_ceiling(sleeps: list[float]) -> None:
    """SA-56 AC4/AC5: 2x never exceeds the ceiling."""
    config = load_config(
        {
            **ENV,
            "REPLY_MAX_TOKENS": "1500",
            "CLAUDE_MAX_TOKENS_CEILING": "2000",
            "POST_MAX_TOKENS": "100",
        }
    )
    recorder = Recorder([_ok(_cut_off()), _ok(_message())])
    await _client(config, recorder).complete("s", "p", Flow.REPLY)
    assert _sent_limits(recorder) == [1500, 2000]


async def test_a_limit_above_the_ceiling_is_warned_about(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A configured limit that the ceiling silently lowers is logged at start-up."""
    config = load_config(
        {
            **ENV,
            "REPLY_MAX_TOKENS": "3000",
            "CLAUDE_MAX_TOKENS_CEILING": "2000",
            "POST_MAX_TOKENS": "100",
        }
    )
    with caplog.at_level("WARNING"):
        _client(config, Recorder([]))
    assert "exceeds CLAUDE_MAX_TOKENS_CEILING" in caplog.text


async def test_no_retry_when_already_at_the_ceiling(sleeps: list[float]) -> None:
    """SA-56 AC4: with no headroom left the cut-off raises without a pointless retry."""
    config = load_config(
        {
            **ENV,
            "REPLY_MAX_TOKENS": "2000",
            "CLAUDE_MAX_TOKENS_CEILING": "2000",
            "POST_MAX_TOKENS": "100",
        }
    )
    recorder = Recorder([_ok(_cut_off())])
    with pytest.raises(ClaudeResponseError):
        await _client(config, recorder).complete("s", "p", Flow.REPLY)
    assert _sent_limits(recorder) == [2000]


async def test_a_second_cut_off_raises_and_never_returns_the_text(
    config: Config, sleeps: list[float]
) -> None:
    """SA-56 AC2/AC5: after the one retry the error names the flow, the output
    tokens and the first 200 characters; the truncated text is not returned."""
    long_text = "x" * 300
    recorder = Recorder([_ok(_cut_off("first")), _ok(_cut_off(long_text, 2048))])
    with pytest.raises(ClaudeResponseError) as error:
        await _client(config, recorder).complete("s", "p", Flow.REPLY)
    assert len(recorder.requests) == 2
    message = str(error.value)
    assert "reply" in message
    assert "2048 output tokens" in message
    assert "x" * 200 in message
    assert "x" * 201 not in message


async def test_every_call_logs_its_usage(
    config: Config, sleeps: list[float], caplog: pytest.LogCaptureFixture
) -> None:
    """SA-56 AC1: flow, model, max_tokens, stop_reason and token counts are logged."""
    with caplog.at_level(logging.INFO, logger="humblebrag.claude_client"):
        await _client(config, Recorder([_ok(_message())])).complete(
            "s", "p", Flow.GUARD
        )
    line = caplog.messages[-1]
    for expected in (
        "flow=guard",
        f"model={MODEL}",
        f"max_tokens={config.guard_max_tokens}",
        "stop_reason=end_turn",
        "input_tokens=3",
        "output_tokens=5",
    ):
        assert expected in line


async def test_a_retried_call_logs_both_attempts(
    config: Config, sleeps: list[float], caplog: pytest.LogCaptureFixture
) -> None:
    """SA-56 AC1: the cut-off attempt is logged too, so the cause is diagnosable."""
    recorder = Recorder([_ok(_cut_off()), _ok(_message())])
    with caplog.at_level(logging.INFO, logger="humblebrag.claude_client"):
        await _client(config, recorder).complete("s", "p", Flow.REPLY)
    calls = [m for m in caplog.messages if m.startswith("Claude call:")]
    assert len(calls) == 2
    assert "stop_reason=max_tokens" in calls[0]


async def test_empty_reply_is_a_typed_error(
    config: Config, sleeps: list[float]
) -> None:
    """A reply with no text raises instead of returning an empty string."""
    with pytest.raises(ClaudeResponseError, match="no text"):
        await _client(config, Recorder([_ok(_message(content=[]))])).complete(
            "s", "p", Flow.REPLY
        )


async def test_only_text_blocks_are_returned(
    config: Config, sleeps: list[float]
) -> None:
    """Non-text blocks (e.g. thinking) are ignored; text is joined and trimmed."""
    mixed = _message(
        content=[
            {"type": "thinking", "thinking": "hmm", "signature": "sig"},
            {"type": "text", "text": "Part one. "},
            {"type": "text", "text": "Part two.\n"},
        ]
    )
    text = await _client(config, Recorder([_ok(mixed)])).complete("s", "p", Flow.REPLY)
    assert text == "Part one. Part two."


def test_client_has_an_explicit_timeout_and_retry_cap(config: Config) -> None:
    """ERR-02/ERR-03: timeout and retries are set explicitly on the SDK client."""
    sdk_client = ClaudeClient(config)._client
    assert sdk_client.max_retries == MAX_RETRIES
    assert sdk_client.timeout == REQUEST_TIMEOUT_SECONDS
