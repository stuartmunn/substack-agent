"""Thin wrapper around the Anthropic API: model from config, bounded retries,
typed errors for the caller to alert on."""

import logging
from enum import StrEnum

import anthropic

from humblebrag.config import Config
from humblebrag.logging_setup import redactor

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
REQUEST_TIMEOUT_SECONDS = 120.0
USER_ROLE = "user"
RETRY_TOKEN_FACTOR = 2
ERROR_TEXT_PREVIEW_CHARS = 200
UNKNOWN_ERROR_PART = "unknown"


class Flow(StrEnum):
    """The kind of work a Claude call does; each has its own max_tokens setting."""

    POST = "post"
    REPLY = "reply"
    GUARD = "guard"
    SEED = "seed"


def _error_detail(error: anthropic.APIStatusError) -> tuple[str, str]:
    """The API's error type and message, from the response body, length-capped.

    Only the API's own ``error`` object is read, never our request. Known
    secrets are redacted before the cap, so a cut cannot split one past the
    redactor. The redactor does not know prompt text: if the API quotes a
    fragment of the request, up to the cap of it can show. Missing parts read
    as "unknown".
    """
    body = error.body if isinstance(error.body, dict) else {}
    detail = body.get("error")
    detail = detail if isinstance(detail, dict) else {}
    error_type = str(detail.get("type") or UNKNOWN_ERROR_PART)
    message = " ".join(str(detail.get("message") or UNKNOWN_ERROR_PART).split())
    message = redactor.redact(message)
    return error_type[:ERROR_TEXT_PREVIEW_CHARS], message[:ERROR_TEXT_PREVIEW_CHARS]


class ClaudeError(Exception):
    """Base class for every failure of a Claude call; messages never hold secrets."""


class ClaudeTimeoutError(ClaudeError):
    """The request timed out on every attempt."""


class ClaudeRateLimitError(ClaudeError):
    """The request was rate limited on every attempt."""


class ClaudeAPIError(ClaudeError):
    """The API failed in another way (HTTP error or connection failure)."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        """Record the HTTP status, if the API answered at all."""
        super().__init__(message)
        self.status_code = status_code


class ClaudeResponseError(ClaudeError):
    """The API answered, but the reply is unusable (refusal, cut off or empty)."""


class ClaudeClient:
    """Generates text with the model named by ``CLAUDE_MODEL``.

    The SDK does the retrying: timeouts, 429s, 5xx and connection errors are
    retried with exponential backoff (honouring ``retry-after``) up to
    ``MAX_RETRIES`` times. Other 4xx errors are not retried.
    """

    def __init__(
        self,
        config: Config,
        http_client: anthropic.DefaultAsyncHttpxClient | None = None,
    ) -> None:
        """Build the async client; ``http_client`` lets tests mock the transport."""
        self._model = config.claude_model
        self._ceiling = config.max_tokens_ceiling
        self._limits = {
            Flow.POST: config.post_max_tokens,
            Flow.REPLY: config.reply_max_tokens,
            Flow.GUARD: config.guard_max_tokens,
            Flow.SEED: config.seed_max_tokens,
        }
        for flow, limit in self._limits.items():
            if limit > self._ceiling:
                logger.warning(
                    "Claude %s max_tokens (%d) exceeds CLAUDE_MAX_TOKENS_CEILING "
                    "(%d); the ceiling applies",
                    flow,
                    limit,
                    self._ceiling,
                )
        self._client = anthropic.AsyncAnthropic(
            api_key=config.anthropic_api_key,
            max_retries=MAX_RETRIES,
            timeout=REQUEST_TIMEOUT_SECONDS,
            http_client=http_client,
        )

    async def complete(self, system: str, prompt: str, flow: Flow) -> str:
        """Return the model's text reply to ``prompt``, or raise a ClaudeError.

        The flow's max_tokens (capped by the ceiling) applies. A reply cut off at
        max_tokens is retried once with the limit doubled, still capped by the
        ceiling; a second cut-off raises ClaudeResponseError.
        """
        limit = min(self._limits[flow], self._ceiling)
        response = await self._request(system, prompt, flow, limit)
        if response.stop_reason == "max_tokens":
            retry_limit = min(limit * RETRY_TOKEN_FACTOR, self._ceiling)
            if retry_limit > limit:
                logger.warning(
                    "Claude %s call hit max_tokens (%d); retrying once at %d",
                    flow,
                    limit,
                    retry_limit,
                )
                response = await self._request(system, prompt, flow, retry_limit)
        return self._reply_text(response, flow)

    async def _request(
        self, system: str, prompt: str, flow: Flow, max_tokens: int
    ) -> anthropic.types.Message:
        """Make one API call, log its token usage and map SDK errors to ours."""
        try:
            response = await self._client.messages.create(
                model=self._model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": USER_ROLE, "content": prompt}],
            )
        except anthropic.APITimeoutError as error:
            raise ClaudeTimeoutError(
                f"Claude request timed out after {MAX_RETRIES} retries"
            ) from error
        except anthropic.RateLimitError as error:
            raise ClaudeRateLimitError(
                f"Claude request was rate limited after {MAX_RETRIES} retries "
                f"(request {error.request_id})"
            ) from error
        except anthropic.APIStatusError as error:
            error_type, error_message = _error_detail(error)
            logger.error(
                "Claude API error: flow=%s max_tokens=%d status=%s request=%s "
                "type=%s message=%s",
                flow,
                max_tokens,
                error.status_code,
                error.request_id,
                error_type,
                error_message,
            )
            raise ClaudeAPIError(
                f"Claude API returned HTTP {error.status_code} "
                f"(request {error.request_id}, flow {flow}, "
                f"type {error_type}): {error_message}",
                status_code=error.status_code,
            ) from error
        except anthropic.APIConnectionError as error:
            raise ClaudeAPIError(
                f"Claude API connection failed after {MAX_RETRIES} retries"
            ) from error
        logger.info(
            "Claude call: flow=%s model=%s max_tokens=%d stop_reason=%s "
            "input_tokens=%d output_tokens=%d",
            flow,
            self._model,
            max_tokens,
            response.stop_reason,
            response.usage.input_tokens,
            response.usage.output_tokens,
        )
        return response

    @staticmethod
    def _reply_text(response: anthropic.types.Message, flow: Flow) -> str:
        """Return the reply text, or raise if the reply is unusable."""
        if response.stop_reason == "refusal":
            category = response.stop_details.category if response.stop_details else None
            raise ClaudeResponseError(
                f"Claude declined the request (category: {category})"
            )
        text = "".join(
            block.text for block in response.content if block.type == "text"
        ).strip()
        if response.stop_reason == "max_tokens":
            raise ClaudeResponseError(
                f"Claude {flow} call was cut off at max_tokens "
                f"({response.usage.output_tokens} output tokens); "
                f"text starts: {text[:ERROR_TEXT_PREVIEW_CHARS]!r}"
            )
        if not text:
            raise ClaudeResponseError("Claude returned no text")
        return text
