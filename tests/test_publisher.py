"""Tests for Substack session auth (SA-31 acceptance criteria).

No network: the first group fakes the Api class; the second runs the real
python-substack code with the HTTP adapter stubbed.
"""

import asyncio
import json
import logging
import re
import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import Any, ClassVar

import pytest
import requests
from requests.adapters import HTTPAdapter
from substack import Api
from substack.exceptions import SubstackAPIException, SubstackRequestException

from humblebrag.alerts import configure_alerts
from humblebrag.config import Config, load_config
from humblebrag.logging_setup import redactor
from humblebrag.publisher import (
    REQUEST_TIMEOUT_SECONDS,
    RUNBOOK_URL,
    SESSION_EXPIRED_MESSAGE,
    SubstackAuthError,
    SubstackError,
    SubstackPublisher,
    cookie_string,
)

SECRET = "s%3ASUPER-SECRET-SID.signature"
PUBLICATION = "https://humblebrag.substack.com"
ENV = {
    "TELEGRAM_BOT_TOKEN": "123456:TESTTOKEN",
    "TELEGRAM_CHAT_ID": "1",
    "ANTHROPIC_API_KEY": "sk-ant-key",
    "CLAUDE_MODEL": "claude-test-model",
    "SUBSTACK_PUBLICATION_URL": PUBLICATION,
    "SUBSTACK_COOKIE": SECRET,
}


@pytest.fixture(autouse=True)
def alerts() -> Iterator[list[str]]:
    """Capture alert texts; reset the redactor and sender afterwards."""
    sent: list[str] = []

    async def sender(text: str) -> None:
        sent.append(text)

    configure_alerts(sender)
    redactor.clear()
    redactor.add([SECRET])
    yield sent
    configure_alerts(None)
    redactor.clear()


def _config(**overrides: str) -> Config:
    """A config with the test cookie and publication."""
    return load_config({**ENV, **overrides})


class FakeApi:
    """Stands in for substack.Api, recording how it was built."""

    calls: ClassVar[list[dict[str, Any]]] = []

    def __init__(self, **kwargs: Any) -> None:
        """Record the constructor arguments."""
        type(self).calls.append(kwargs)


@pytest.fixture(autouse=True)
def _reset_fake_api() -> None:
    """Clear the recorded constructor calls."""
    FakeApi.calls = []


def _failing(error: Exception) -> Any:
    """An api factory that raises ``error``."""

    def factory(**_kwargs: Any) -> Any:
        raise error

    return factory


# AC1: python-substack pinned, cookie auth from the secret and the URL


def test_python_substack_is_pinned_to_an_exact_version() -> None:
    """AC1/SEC-08: pyproject pins python-substack with ==."""
    root = Path(__file__).resolve().parent.parent
    dependencies = tomllib.loads((root / "pyproject.toml").read_text())["project"][
        "dependencies"
    ]
    assert any(re.fullmatch(r"python-substack==\d+\.\d+\.\d+", d) for d in dependencies)


def test_the_real_python_substack_api_is_the_default() -> None:
    """AC1: the wrapper uses python-substack's Api unless a test swaps it."""
    assert SubstackPublisher(_config())._api_factory is Api


def test_api_is_built_with_the_cookie_the_publication_and_a_timeout() -> None:
    """AC1/ERR-02: cookie auth from the secret, the publication URL, a timeout."""
    publisher = SubstackPublisher(_config(), FakeApi)
    publisher.check_session()
    [call] = FakeApi.calls
    assert call["cookies_string"] == f"substack.sid={SECRET}"
    assert call["publication_url"] == PUBLICATION
    assert call["timeout"] == REQUEST_TIMEOUT_SECONDS
    assert "email" not in call
    assert "password" not in call
    assert isinstance(publisher.api, FakeApi)


@pytest.mark.parametrize(
    ("secret", "expected"),
    [
        ("s%3Abare-value", "substack.sid=s%3Abare-value"),
        ("  s:bare  ", "substack.sid=s:bare"),
        ("substack.sid=abc; substack.lli=1", "substack.sid=abc; substack.lli=1"),
        ("connect.sid=abc", "connect.sid=abc"),
        ("abc123=", "substack.sid=abc123="),
        ("abc123==", "substack.sid=abc123=="),
        ("a=b; c=d", "a=b; c=d"),
    ],
)
def test_cookie_secret_may_be_bare_or_a_full_cookie_string(
    secret: str, expected: str
) -> None:
    """AC1: both a bare sid value and a copied cookie string work."""
    assert cookie_string(secret) == expected


def test_api_is_unavailable_before_a_successful_check() -> None:
    """Publishing code cannot use an unchecked session."""
    with pytest.raises(SubstackError, match="not been checked"):
        _ = SubstackPublisher(_config(), FakeApi).api


# AC2: check_session at startup and before every publish


async def test_ensure_session_checks_every_time_it_is_called() -> None:
    """AC2: each call re-verifies the session (so "before every publish" holds)."""
    publisher = SubstackPublisher(_config(), FakeApi)
    await publisher.ensure_session()
    await publisher.ensure_session()
    assert len(FakeApi.calls) == 2


async def test_check_runs_off_the_event_loop() -> None:
    """ERR-13: the blocking network check does not stall the loop."""
    import threading

    main_thread = threading.get_ident()
    seen: list[int] = []

    def factory(**_kwargs: Any) -> Any:
        seen.append(threading.get_ident())
        return object()

    await SubstackPublisher(_config(), factory).ensure_session()
    assert seen
    assert seen[0] != main_thread


async def test_startup_hook_runs_when_the_bot_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC2: run_bot starts the session check after the alert sender exists."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from humblebrag import alerts as alerts_module
    from humblebrag import bot

    updater = SimpleNamespace(start_polling=AsyncMock(), stop=AsyncMock(), running=True)
    app = MagicMock()
    app.updater = updater
    app.running = True
    app.start = AsyncMock()
    app.stop = AsyncMock()
    app.__aenter__ = AsyncMock(return_value=app)
    app.__aexit__ = AsyncMock(return_value=None)
    monkeypatch.setattr(bot, "build_application", lambda _config: app)
    sender_seen: list[object] = []
    started = asyncio.Event()

    async def on_started() -> None:
        sender_seen.append(alerts_module._sender)
        started.set()
        await asyncio.sleep(3600)  # still running at shutdown: must be cancelled

    task = asyncio.create_task(bot.run_bot(_config(), on_started=on_started))
    async with asyncio.timeout(5):
        await started.wait()
    assert sender_seen[0] is not None
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_entrypoint_passes_the_session_check_to_the_bot() -> None:
    """AC2: __main__ wires publisher.check_session_or_alert into run_bot."""
    source = (
        Path(__file__).resolve().parent.parent / "src/humblebrag/__main__.py"
    ).read_text()
    assert "on_started=publisher.check_session_or_alert" in source


async def test_startup_check_reports_health_without_raising(alerts: list[str]) -> None:
    """AC2: the startup variant returns False on failure and never raises."""
    healthy = SubstackPublisher(_config(), FakeApi)
    assert await healthy.check_session_or_alert() is True
    expired = SubstackPublisher(
        _config(), _failing(SubstackAPIException(401, '{"error": "no"}'))
    )
    assert await expired.check_session_or_alert() is False
    assert len(alerts) == 1


async def test_startup_check_survives_an_unexpected_error(
    alerts: list[str], caplog: pytest.LogCaptureFixture
) -> None:
    """ERR-01/07: even a surprise exception is alerted, not raised."""
    publisher = SubstackPublisher(_config(), _failing(RuntimeError("library changed")))
    assert await publisher.check_session_or_alert() is False
    assert len(alerts) == 1
    assert "RuntimeError: library changed" in alerts[0]
    assert "Unexpected error" in caplog.text


# AC3: expired session -> exact alert with the runbook link


@pytest.mark.parametrize("status", [401, 403])
async def test_expired_session_alerts_with_message_and_runbook(
    alerts: list[str], status: int
) -> None:
    """AC3: auth failures say "Substack session expired, refresh cookie" + link."""
    error = SubstackAPIException(status, json.dumps({"error": "Unauthorized"}))
    publisher = SubstackPublisher(_config(), _failing(error))
    with pytest.raises(SubstackAuthError, match=SESSION_EXPIRED_MESSAGE):
        await publisher.ensure_session()
    [text] = alerts
    assert "Substack session expired, refresh cookie" in text
    assert RUNBOOK_URL in text
    assert RUNBOOK_URL.endswith("docs/runbook.md#refresh-the-substack-cookie")


@pytest.mark.parametrize(
    "message",
    [
        "Requested publication is unavailable: https://humblebrag.substack.com",
        "Could not find primary publication in profile",
    ],
)
async def test_a_session_with_no_access_to_the_publication_counts_as_expired(
    alerts: list[str], message: str
) -> None:
    """AC3: a logged-out cookie sees no publications, which is the same alert."""
    publisher = SubstackPublisher(
        _config(), _failing(SubstackRequestException(message))
    )
    with pytest.raises(SubstackAuthError):
        await publisher.ensure_session()
    assert SESSION_EXPIRED_MESSAGE in alerts[0]


@pytest.mark.parametrize(
    "error",
    [
        SubstackAPIException(500, "<html>oops</html>"),
        SubstackRequestException("Invalid Response: <html>"),
        requests.ConnectTimeout("slow"),
        requests.ConnectionError("down"),
    ],
)
async def test_other_failures_are_not_reported_as_expiry(
    alerts: list[str], error: Exception
) -> None:
    """ERR-01: outages and odd replies alert, but do not blame the cookie."""
    publisher = SubstackPublisher(_config(), _failing(error))
    with pytest.raises(SubstackError) as raised:
        await publisher.ensure_session()
    assert not isinstance(raised.value, SubstackAuthError)
    assert len(alerts) == 1
    assert SESSION_EXPIRED_MESSAGE not in alerts[0]
    assert "Session check failed" in alerts[0]


async def test_a_failed_check_discards_the_previous_session(alerts: list[str]) -> None:
    """After a failure the old API object cannot be used to publish."""
    publisher = SubstackPublisher(_config(), FakeApi)
    publisher.check_session()
    publisher._api_factory = _failing(SubstackAPIException(401, "{}"))
    with pytest.raises(SubstackAuthError):
        await publisher.ensure_session()
    with pytest.raises(SubstackError, match="not been checked"):
        _ = publisher.api


async def test_the_cookie_never_reaches_alerts_or_logs(
    alerts: list[str], caplog: pytest.LogCaptureFixture
) -> None:
    """SEC-01/03: failures mention neither the cookie nor its value."""
    leaky = SubstackRequestException(f"bad reply with {SECRET} inside")
    with caplog.at_level(logging.DEBUG):
        publisher = SubstackPublisher(_config(), _failing(leaky))
        with pytest.raises(SubstackError):
            await publisher.ensure_session()
    assert "SUPER-SECRET" not in " ".join(alerts)
    assert "SUPER-SECRET" not in caplog.text
    assert "SUPER-SECRET" not in repr(publisher)


# The real python-substack code, with the HTTP adapter stubbed


class Wire:
    """Replaces HTTPAdapter.send, answering by URL and recording requests."""

    def __init__(self) -> None:
        """Start with a healthy logged-in account."""
        self.requests: list[tuple[str, dict[str, str], Any]] = []
        self.profile_status = 200
        self.publications: list[dict[str, Any]] = [
            {"publication": {"id": 7, "subdomain": "humblebrag", "name": "HB"}}
        ]
        self.error: Exception | None = None

    def send(self, adapter: HTTPAdapter, request: Any, **kwargs: Any) -> Any:
        """Answer one request."""
        self.requests.append(
            (request.url, dict(request.headers), kwargs.get("timeout"))
        )
        if self.error is not None:
            raise self.error
        response = requests.Response()
        response.url = request.url
        response.request = request
        if request.url.endswith("/user/profile/self"):
            response.status_code = self.profile_status
            body: Any = (
                {"id": 1, "publicationUsers": self.publications}
                if self.profile_status == 200
                else {"error": "Unauthorized"}
            )
        else:
            response.status_code = 200
            body = {}
        response._content = json.dumps(body).encode()
        return response


@pytest.fixture
def wire(monkeypatch: pytest.MonkeyPatch) -> Wire:
    """A stubbed Substack for the real Api."""
    stub = Wire()
    monkeypatch.setattr(
        HTTPAdapter, "send", lambda self, req, **kw: stub.send(self, req, **kw)
    )
    return stub


def test_real_api_sends_the_session_cookie_and_a_timeout(wire: Wire) -> None:
    """AC1/ERR-02: the real library sends substack.sid and honours the timeout."""
    publisher = SubstackPublisher(_config())
    publisher.check_session()
    assert publisher.api is not None
    assert wire.requests
    for _url, headers, timeout in wire.requests:
        assert f"substack.sid={SECRET.replace('%3A', ':')}" in headers.get("Cookie", "")
        assert timeout == REQUEST_TIMEOUT_SECONDS
    assert wire.requests[0][0].endswith("/user/profile/self")


def test_real_api_matches_the_configured_publication(wire: Wire) -> None:
    """AC1: it signs in to the publication named by SUBSTACK_PUBLICATION_URL."""
    SubstackPublisher(_config()).check_session()
    assert any("for_pub=humblebrag" in url for url, _h, _t in wire.requests)


def test_real_api_rejects_a_publication_the_account_does_not_own(wire: Wire) -> None:
    """AC3: a cookie for another account reads as an expired session."""
    publisher = SubstackPublisher(
        _config(SUBSTACK_PUBLICATION_URL="https://someoneelse.substack.com")
    )
    with pytest.raises(SubstackAuthError):
        publisher.check_session()


def test_real_api_401_is_an_expired_session(wire: Wire) -> None:
    """AC3: Substack answering 401 maps to SubstackAuthError."""
    wire.profile_status = 401
    with pytest.raises(SubstackAuthError):
        SubstackPublisher(_config()).check_session()


def test_real_api_logged_out_cookie_with_no_publications_is_expired(wire: Wire) -> None:
    """AC3: a 200 with no publications (logged out) is also an expired session."""
    wire.publications = []
    with pytest.raises(SubstackAuthError):
        SubstackPublisher(_config()).check_session()


def test_real_api_network_error_is_not_an_expiry(wire: Wire) -> None:
    """A transport failure is a SubstackError, not an auth error."""
    wire.error = requests.ConnectionError("down")
    with pytest.raises(SubstackError) as raised:
        SubstackPublisher(_config()).check_session()
    assert not isinstance(raised.value, SubstackAuthError)


async def test_a_failing_startup_hook_is_alerted_not_lost(
    monkeypatch: pytest.MonkeyPatch, alerts: list[str]
) -> None:
    """PR-Agent: a startup hook that raises (even before shutdown) alerts Stuart."""
    from humblebrag import bot

    async def broken() -> None:
        raise RuntimeError("hook exploded")

    await bot._run_startup(broken)
    assert len(alerts) == 1
    assert "Startup task failed: RuntimeError: hook exploded" in alerts[0]


async def test_overlapping_checks_never_run_at_the_same_time() -> None:
    """PR-Agent: two concurrent ensure_session calls are serialised."""
    import threading
    import time

    active = 0
    peak = 0
    guard = threading.Lock()

    def factory(**_kwargs: Any) -> Any:
        nonlocal active, peak
        with guard:
            active += 1
            peak = max(peak, active)
        time.sleep(0.05)
        with guard:
            active -= 1
        return object()

    publisher = SubstackPublisher(_config(), factory)
    await asyncio.gather(publisher.ensure_session(), publisher.ensure_session())
    assert peak == 1


async def test_failure_details_are_logged_without_the_cookie(
    alerts: list[str], caplog: pytest.LogCaptureFixture
) -> None:
    """PR-Agent: operators can see the real cause in the log; no secret in it."""
    error = SubstackAPIException(503, f"upstream down while using {SECRET}")
    publisher = SubstackPublisher(_config(), _failing(error))
    with caplog.at_level(logging.WARNING), pytest.raises(SubstackError):
        await publisher.ensure_session()
    assert "SubstackAPIException" in caplog.text
    assert "503" in caplog.text
    assert "upstream down" in caplog.text
    assert "SUPER-SECRET" not in caplog.text
