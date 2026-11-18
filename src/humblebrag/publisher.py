"""The only module that talks to python-substack (an unofficial API).

SA-31 covers the session: cookie auth and ``check_session()``. Publishing is
added behind this same class in SA-32, and always with ``send=False``.
"""

import asyncio
import logging
import re
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
        try:
            self._api = self._api_factory(
                cookies_string=cookie_string(self._cookie),
                publication_url=self._publication_url,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
        except SubstackAPIException as error:
            self._api = None
            if error.status_code in AUTH_STATUS_CODES:
                raise SubstackAuthError(SESSION_EXPIRED_MESSAGE) from None
            raise SubstackError(
                f"Substack returned HTTP {error.status_code} during the session check"
            ) from None
        except SubstackRequestException as error:
            self._api = None
            if error.message.startswith(_NO_PUBLICATION_MARKERS):
                raise SubstackAuthError(SESSION_EXPIRED_MESSAGE) from None
            raise SubstackError("Substack gave an unusable reply") from None
        except requests.RequestException as error:
            self._api = None
            raise SubstackError(
                f"Substack could not be reached ({type(error).__name__})"
            ) from None

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
