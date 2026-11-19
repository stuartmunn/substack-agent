"""The only module that talks to python-substack (an unofficial API).

SA-31 covers the session (cookie auth, ``check_session()``). SA-41 adds
``create_draft()`` and ``update_draft()``. The agent never calls Substack's
publish endpoint: it creates and updates drafts only, and Stuart publishes in
the Substack editor. No Substack Notes are created either.
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
EDITOR_PATH = "/publish/post/{draft_id}"
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


class DraftError(SubstackError):
    """Creating or updating a Substack draft failed; nothing was published."""


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
        self._draft_lock = threading.Lock()

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

    def editor_url(self, draft_id: str) -> str:
        """The link that opens the draft in the Substack editor."""
        base = self._publication_url.rstrip("/")
        return base + EDITOR_PATH.format(draft_id=draft_id)

    async def create_draft(self, title: str, subtitle: str, body_markdown: str) -> str:
        """Create a Substack draft and return its ID. Never publishes.

        Re-checks the session first (which alerts on failure). Raises
        ``DraftError`` on any other failure; the caller alerts. Never retried.
        """
        await self.ensure_session()
        return await asyncio.to_thread(
            self._create_blocking, title, subtitle, body_markdown
        )

    async def update_draft(
        self, draft_id: str, title: str, subtitle: str, body_markdown: str
    ) -> None:
        """Replace the title, subtitle and body of an existing draft.

        Re-checks the session first. Raises ``DraftError`` on failure.
        """
        await self.ensure_session()
        await asyncio.to_thread(
            self._update_blocking, draft_id, title, subtitle, body_markdown
        )

    def _create_blocking(self, title: str, subtitle: str, body_markdown: str) -> str:
        """Markdown -> draft; returns the draft ID. Blocking."""
        with self._draft_lock:  # two draft calls never overlap
            try:
                reply = self.api.create_draft_from_markdown(
                    title, body_markdown, subtitle=subtitle
                )
                return self._draft_id(reply)
            except Exception as error:  # noqa: BLE001  # ERR-18: any library error
                raise self._draft_failure("creating", error) from None

    def _update_blocking(
        self, draft_id: str, title: str, subtitle: str, body_markdown: str
    ) -> None:
        """Rewrite an existing draft's body, subtitle and title. Blocking."""
        with self._draft_lock:
            try:
                # The draft is the agent's own copy of the stored post, so the
                # library's revision check is skipped on purpose.
                self.api.update_draft_from_markdown(
                    draft_id, body_markdown, subtitle=subtitle, allow_conflict=True
                )
                self.api.put_draft(draft_id, draft_title=title)
            except Exception as error:  # noqa: BLE001  # ERR-18: any library error
                raise self._draft_failure("updating", error) from None

    @staticmethod
    def _draft_id(reply: Any) -> str:
        """The new draft's ID from the create reply."""
        draft = reply.get("draft") if isinstance(reply, dict) else None
        draft_id = draft.get("id") if isinstance(draft, dict) else None
        if draft_id is None or draft_id == "":
            raise ValueError("create reply had no draft id")
        return str(draft_id)

    @staticmethod
    def _draft_failure(action: str, error: Exception) -> DraftError:
        """A typed, secret-free error; the original is logged then cut off so
        no traceback can carry request headers."""
        logger.error(
            "Substack draft %s failed: %s: %s", action, type(error).__name__, error
        )
        return DraftError(
            f"Substack draft {action} failed ({type(error).__name__}). "
            "Nothing was published."
        )
