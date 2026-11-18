"""The only module that talks to python-substack (an unofficial API).

SA-31 covers the session (cookie auth, ``check_session()``); SA-32 adds
``publish()``, which only ever publishes to the web: ``send`` is always False,
so no email goes to subscribers.
"""

import asyncio
import logging
import re
import threading
from collections.abc import Callable
from typing import Any

import requests
from substack import Api
from substack.exceptions import SubstackAPIException, SubstackRequestException

from humblebrag.alerts import alert, describe_error
from humblebrag.config import Config

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT_SECONDS = 30.0
SESSION_COOKIE_NAME = "substack.sid"
AUTH_STATUS_CODES = frozenset({401, 403})
SESSION_EXPIRED_MESSAGE = "Substack session expired, refresh cookie"
RUNBOOK_URL = (
    "https://github.com/stuartmunn/substack-agent/blob/main/docs/runbook.md"
    "#refresh-the-substack-cookie"
)
PUBLISH_ALERT_EVENT = "substack publish"
# Hard rule: never email subscribers. Not a parameter, not configurable.
SEND_EMAIL = False
SESSION_ALERT_EVENT = "substack session"
CHECK_ALERT_EVENT = "substack"
# Raised by python-substack when the cookie logs in as nobody or the wrong user.
_NO_PUBLICATION_MARKERS = (
    "Requested publication is unavailable",
    "Could not find primary publication",
)
_KNOWN_COOKIE = re.compile(r"^(substack\.sid|connect\.sid|substack\.lli)=")

ApiFactory = Callable[..., Any]


class SubstackError(Exception):
    """A Substack call failed for a reason other than an expired session."""


class PublishError(SubstackError):
    """Publishing failed. Never retried automatically: the post may be live.

    ``draft_id`` is the Substack draft that was created (if any) and may be left
    orphaned; ``maybe_published`` is True when the publish call was made, so the
    post could already be public and must be checked by hand before retrying.
    """

    def __init__(
        self, message: str, draft_id: str | None, *, maybe_published: bool
    ) -> None:
        """Keep the draft id and whether the post may be live."""
        super().__init__(message)
        self.draft_id = draft_id
        self.maybe_published = maybe_published


class SubstackAuthError(SubstackError):
    """The session cookie is missing, expired or rejected."""


def cookie_string(secret: str) -> str:
    """Return the cookie as ``name=value`` pairs.

    The secret may be the full ``name=value; ...`` string copied from the
    browser, or just the bare ``substack.sid`` value (which may end in ``=``).
    """
    value = secret.strip()
    if ";" in value or _KNOWN_COOKIE.match(value):
        return value
    return f"{SESSION_COOKIE_NAME}={value}"


class SubstackPublisher:
    """Holds the authenticated Substack API for the configured publication."""

    def __init__(self, config: Config, api_factory: ApiFactory = Api) -> None:
        """Keep the cookie and publication URL; nothing is contacted yet."""
        self._cookie = config.substack_cookie
        self._publication_url = config.substack_publication_url
        self._api_factory = api_factory
        self._api: Any | None = None
        self._lock = threading.Lock()
        self._publish_lock = threading.Lock()

    @property
    def api(self) -> Any:
        """The API from the last successful ``check_session()``."""
        if self._api is None:
            raise SubstackError("Substack session has not been checked yet")
        return self._api

    def check_session(self) -> None:
        """Log in with the cookie and confirm the publication is reachable.

        Raises SubstackAuthError if the session has expired or is rejected, and
        SubstackError for any other failure (outage, timeout, changed API).
        Blocking: async callers use ``ensure_session``.
        """
        with self._lock:  # overlapping checks run one at a time
            try:
                api = self._login()
            except SubstackError:
                self._api = None  # a failed check leaves no usable session
                raise
            self._api = api  # one assignment: readers never see a gap

    def _login(self) -> Any:
        """Build the API from the cookie, translating library errors."""
        try:
            return self._api_factory(
                cookies_string=cookie_string(self._cookie),
                publication_url=self._publication_url,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
        except SubstackAPIException as error:
            self._log_failure(error)
            if error.status_code in AUTH_STATUS_CODES:
                raise SubstackAuthError(SESSION_EXPIRED_MESSAGE) from None
            raise SubstackError(
                f"Substack returned HTTP {error.status_code} during the session check"
            ) from None
        except SubstackRequestException as error:
            self._log_failure(error)
            if str(error.message or "").startswith(_NO_PUBLICATION_MARKERS):
                raise SubstackAuthError(SESSION_EXPIRED_MESSAGE) from None
            raise SubstackError("Substack gave an unusable reply") from None
        except requests.RequestException as error:
            self._log_failure(error)
            raise SubstackError(
                f"Substack could not be reached ({type(error).__name__})"
            ) from None
        except Exception as error:  # library changed: still a typed, alerted error
            logger.exception("Unexpected error from python-substack")
            raise SubstackError(
                f"Substack session check failed unexpectedly ({type(error).__name__})"
            ) from None

    @staticmethod
    def _log_failure(error: Exception) -> None:
        """Log the library's own description (its cookie-like values are redacted
        by the library and by the log redactor). The chain is cut with
        ``from None`` so no traceback can carry request headers."""
        logger.warning(
            "Substack session check failed: %s: %s", type(error).__name__, error
        )

    async def ensure_session(self) -> None:
        """Check the session off the event loop; on failure alert Stuart, re-raise."""
        try:
            await asyncio.to_thread(self.check_session)
        except SubstackAuthError:
            await alert(
                SESSION_ALERT_EVENT,
                f"{SESSION_EXPIRED_MESSAGE}. Runbook: {RUNBOOK_URL}",
            )
            raise
        except SubstackError as error:
            await alert(CHECK_ALERT_EVENT, f"Session check failed: {error}")
            raise

    async def check_session_or_alert(self) -> bool:
        """Startup check: alert on any failure but never raise. True if healthy."""
        try:
            await self.ensure_session()
        except SubstackError:
            return False
        except Exception as error:  # a startup check must never crash the agent
            logger.exception("Unexpected error during the Substack session check")
            await alert(
                CHECK_ALERT_EVENT, f"Session check crashed: {describe_error(error)}"
            )
            return False
        return True

    async def publish(self, title: str, subtitle: str, body_markdown: str) -> str:
        """Publish to the web only and return the public URL.

        Re-checks the session first. Any failure alerts Stuart, raises
        ``PublishError`` and is never retried here, to avoid a double post.
        """
        await self.ensure_session()
        try:
            return await asyncio.to_thread(
                self._publish_blocking, title, subtitle, body_markdown
            )
        except PublishError as error:
            logger.error("Publishing failed: %s", error)
            await alert(PUBLISH_ALERT_EVENT, str(error))
            raise

    def _publish_blocking(self, title: str, subtitle: str, body_markdown: str) -> str:
        """Markdown -> draft -> prepublish -> publish(send=False). Blocking."""
        with self._publish_lock:  # two publishes never overlap
            api = self.api
            draft_id: str | None = None
            published_called = False
            try:
                draft = api.create_draft_from_markdown(
                    title, body_markdown, subtitle=subtitle
                )["draft"]
                draft_id = str(draft["id"])
                api.prepublish_draft(draft_id)
                published_called = True
                result = api.publish_draft(draft_id, send=SEND_EMAIL)
                return self._public_url(result)
            except Exception as error:
                logger.exception("Substack publish failed")
                raise self._publish_failure(
                    draft_id, published_called, type(error).__name__
                ) from None

    def _public_url(self, result: Any) -> str:
        """The post's public https URL from the publish reply."""
        if isinstance(result, dict):
            url = result.get("canonical_url")
            if isinstance(url, str) and url.startswith("https://"):
                return url
            slug = result.get("slug")
            if isinstance(slug, str) and slug:
                return f"{self._publication_url.rstrip('/')}/p/{slug}"
        raise ValueError("publish reply had no post URL")

    @staticmethod
    def _publish_failure(
        draft_id: str | None, published_called: bool, error_name: str
    ) -> PublishError:
        """Describe a failure, logging any orphaned draft with its ID."""
        if published_called:
            note = (
                f"Substack publish failed ({error_name}) after the publish call; "
                f"the post may be live. Check Substack before retrying. "
                f"Draft ID: {draft_id}"
            )
        elif draft_id is not None:
            logger.error(
                "Orphaned Substack draft left by a failed publish: %s", draft_id
            )
            note = (
                f"Substack publish failed ({error_name}). Nothing was published; "
                f"orphaned draft ID: {draft_id}"
            )
        else:
            note = f"Substack publish failed ({error_name}). No draft was created."
        if published_called:
            logger.error("Draft %s may be published or orphaned", draft_id)
        return PublishError(note, draft_id, maybe_published=published_called)
