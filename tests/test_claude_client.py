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
    MAX_RETRIES,
    REQUEST_TIMEOUT_SECONDS,
    ClaudeAPIError,
    ClaudeClient,
    ClaudeError,
    ClaudeRateLimitError,
    ClaudeResponseError,
    ClaudeTimeoutError,
)
from humblebrag.config import Config, load_config

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
    text = await _client(config, recorder).complete("be funny", "write a post")
    assert text == "Hello there"
    assert sleeps == []


async def test_model_comes_from_config_and_request_is_well_formed(
    config: Config, sleeps: list[float]
) -> None:
    """AC1: the model in the request is CLAUDE_MODEL; no sampling or thinking
    parameters that current models reject are sent."""
    recorder = Recorder([_ok(_message())])
    await _client(config, recorder).complete("be funny", "write a post", max_tokens=777)
    body = json.loads(recorder.requests[0].content)
    assert body["model"] == MODEL
    assert body["max_tokens"] == 777
    assert body["system"] == "be funny"
    assert body["messages"] == [{"role": "user", "content": "write a post"}]
    for rejected in ("temperature", "top_p", "top_k", "thinking", "budget_tokens"):
        assert rejected not in body
    assert recorder.requests[0].headers["authorization"] == f"Bearer {API_KEY}"


async def test_a_different_configured_model_is_used(sleeps: list[float]) -> None:
    """AC1: changing CLAUDE_MODEL changes the request, with no code change."""
    other = load_config({**ENV, "CLAUDE_MODEL": "another-model"})
    recorder = Recorder([_ok(_message())])
    await _client(other, recorder).complete("s", "p")
    assert json.loads(recorder.requests[0].content)["model"] == "another-model"


async def test_rate_limit_is_retried_then_succeeds(
    config: Config, sleeps: list[float]
) -> None:
    """AC2: two 429s then success means three requests and a good result."""
    limited = _ok(_error(429, "rate_limit_error"))
    recorder = Recorder([limited, limited, _ok(_message("finally"))])
    assert await _client(config, recorder).complete("s", "p") == "finally"
    assert len(recorder.requests) == 3
    assert len(sleeps) == 2


async def test_timeout_is_retried_then_succeeds(
    config: Config, sleeps: list[float]
) -> None:
    """AC2: a timeout followed by success is retried."""
    recorder = Recorder([_timeout, _ok(_message("recovered"))])
    assert await _client(config, recorder).complete("s", "p") == "recovered"
    assert len(recorder.requests) == 2


async def test_retries_are_capped_at_three_with_growing_backoff(
    config: Config, sleeps: list[float]
) -> None:
    """AC2: persistent timeouts give 1 try + 3 retries, waiting longer each time."""
    recorder = Recorder([_timeout])
    with pytest.raises(ClaudeTimeoutError):
        await _client(config, recorder).complete("s", "p")
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
        await _client(config, recorder).complete("s", "p")
    assert len(recorder.requests) == MAX_RETRIES + 1
    assert isinstance(error.value, ClaudeError)
    assert "req_test123" in str(error.value)
    assert isinstance(error.value.__cause__, anthropic.RateLimitError)


async def test_persistent_timeout_raises_a_typed_error(
    config: Config, sleeps: list[float]
) -> None:
    """AC3: exhausted timeouts raise ClaudeTimeoutError, chained to the cause."""
    with pytest.raises(ClaudeTimeoutError) as error:
        await _client(config, Recorder([_timeout])).complete("s", "p")
    assert isinstance(error.value, ClaudeError)
    assert isinstance(error.value.__cause__, anthropic.APITimeoutError)


async def test_client_errors_are_not_retried(
    config: Config, sleeps: list[float]
) -> None:
    """AC3: a 400 fails at once as ClaudeAPIError carrying the status."""
    recorder = Recorder([_ok(_error(400, "invalid_request_error"))])
    with pytest.raises(ClaudeAPIError) as error:
        await _client(config, recorder).complete("s", "p")
    assert len(recorder.requests) == 1
    assert sleeps == []
    assert error.value.status_code == 400
    assert "req_test123" in str(error.value)


async def test_server_errors_are_retried_then_raise_a_typed_error(
    config: Config, sleeps: list[float]
) -> None:
    """AC3: a persistent 500 is retried, then raises ClaudeAPIError."""
    recorder = Recorder([_ok(_error(500, "api_error"))])
    with pytest.raises(ClaudeAPIError) as error:
        await _client(config, recorder).complete("s", "p")
    assert len(recorder.requests) == MAX_RETRIES + 1
    assert error.value.status_code == 500


async def test_connection_failure_raises_a_typed_error(
    config: Config, sleeps: list[float]
) -> None:
    """AC3: a connection error becomes ClaudeAPIError with no status."""

    def refuse(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("refused", request=request)

    with pytest.raises(ClaudeAPIError) as error:
        await _client(config, Recorder([refuse])).complete("s", "p")
    assert error.value.status_code is None


async def test_errors_never_contain_the_api_key(
    config: Config,
    sleeps: list[float],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """SEC-03: an auth failure's typed error and logs hold no secret."""
    recorder = Recorder([_ok(_error(401, "authentication_error", "invalid x-api-key"))])
    with caplog.at_level(logging.DEBUG), pytest.raises(ClaudeAPIError) as error:
        await _client(config, recorder).complete("s", "p")
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
        await _client(config, Recorder([_ok(refusal)])).complete("s", "p")


async def test_truncated_reply_is_a_typed_error(
    config: Config, sleeps: list[float]
) -> None:
    """A reply cut off at max_tokens is not silently used as a finished post."""
    cut = _message("half a sent", stop_reason="max_tokens")
    with pytest.raises(ClaudeResponseError, match="max_tokens"):
        await _client(config, Recorder([_ok(cut)])).complete("s", "p")


async def test_empty_reply_is_a_typed_error(
    config: Config, sleeps: list[float]
) -> None:
    """A reply with no text raises instead of returning an empty string."""
    with pytest.raises(ClaudeResponseError, match="no text"):
        await _client(config, Recorder([_ok(_message(content=[]))])).complete("s", "p")


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
    text = await _client(config, Recorder([_ok(mixed)])).complete("s", "p")
    assert text == "Part one. Part two."


def test_client_has_an_explicit_timeout_and_retry_cap(config: Config) -> None:
    """ERR-02/ERR-03: timeout and retries are set explicitly on the SDK client."""
    sdk_client = ClaudeClient(config)._client
    assert sdk_client.max_retries == MAX_RETRIES
    assert sdk_client.timeout == REQUEST_TIMEOUT_SECONDS
