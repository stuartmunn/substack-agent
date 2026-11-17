"""Thin wrapper around the Anthropic API: model from config, bounded retries,
typed errors for the caller to alert on."""

import logging

import anthropic

from humblebrag.config import Config

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
REQUEST_TIMEOUT_SECONDS = 120.0
DEFAULT_MAX_TOKENS = 4096
USER_ROLE = "user"


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
        self._client = anthropic.AsyncAnthropic(
            api_key=config.anthropic_api_key,
            max_retries=MAX_RETRIES,
            timeout=REQUEST_TIMEOUT_SECONDS,
            http_client=http_client,
        )

    async def complete(
        self, system: str, prompt: str, max_tokens: int = DEFAULT_MAX_TOKENS
    ) -> str:
        """Return the model's text reply to ``prompt``, or raise a ClaudeError."""
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
            raise ClaudeAPIError(
                f"Claude API returned HTTP {error.status_code} "
                f"(request {error.request_id})",
                status_code=error.status_code,
            ) from error
        except anthropic.APIConnectionError as error:
            raise ClaudeAPIError(
                f"Claude API connection failed after {MAX_RETRIES} retries"
            ) from error
        return self._reply_text(response)

    @staticmethod
    def _reply_text(response: anthropic.types.Message) -> str:
        """Return the reply text, or raise if the reply is unusable."""
        if response.stop_reason == "refusal":
            category = response.stop_details.category if response.stop_details else None
            raise ClaudeResponseError(
                f"Claude declined the request (category: {category})"
            )
        if response.stop_reason == "max_tokens":
            raise ClaudeResponseError("Claude's reply was cut off at max_tokens")
        text = "".join(
            block.text for block in response.content if block.type == "text"
        ).strip()
        if not text:
            raise ClaudeResponseError("Claude returned no text")
        return text
