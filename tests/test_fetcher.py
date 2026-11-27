"""Tests for the Substack link fetcher (SA-52 acceptance criteria).

All pages are saved fixtures served by a fake ``get``; there is no live network.
"""

import ast
import socket
import threading
import tomllib
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import requests
from requests.adapters import HTTPAdapter

from humblebrag import fetcher as fetcher_module
from humblebrag.alerts import configure_alerts
from humblebrag.config import DEFAULT_FETCH_TIMEOUT_SECONDS, load_config
from humblebrag.fetcher import (
    DEFAULT_TIMEOUT_SECONDS,
    MAX_BODY_BYTES,
    MAX_REDIRECTS,
    TEXT_LIMITS,
    FetchStatus,
    TargetType,
    fetch_target,
    get_pinned,
    read_substack_link,
)

FIXTURES = Path(__file__).parent / "fixtures" / "substack"
ROOT = Path(__file__).resolve().parent.parent
POST_URL = "https://example.substack.com/p/a-humble-post"
COMMENT_URL = POST_URL + "/comment/123456"
NOTE_URL = "https://substack.com/@example/note/c-987654"
CUSTOM_URL = "https://blog.example.org/p/a-humble-post"
HTML = "text/html; charset=utf-8"


def _page(name: str) -> bytes:
    """Load a saved fixture page."""
    return (FIXTURES / name).read_bytes()


class FakeResponse:
    """The parts of ``requests.Response`` the fetcher uses."""

    def __init__(
        self,
        status: int = 200,
        body: bytes = b"",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status
        self.headers = {"Content-Type": HTML, **(headers or {})}
        self.encoding = "utf-8"
        self._body = body
        self.closed = False

    def iter_content(self, chunk_size: int) -> Iterator[bytes]:
        """Yield the body in chunks."""
        for start in range(0, len(self._body), chunk_size):
            yield self._body[start : start + chunk_size]

    def close(self) -> None:
        """Record that the response was released."""
        self.closed = True


class FakeGet:
    """A scripted replacement for ``requests.get`` that records its calls."""

    def __init__(self, *responses: FakeResponse | Exception) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, url: str, address: str, **kwargs: Any) -> FakeResponse:
        """Return or raise the next scripted outcome."""
        self.calls.append((url, {**kwargs, "address": address}))
        outcome = self.responses.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _serve(name: str) -> FakeGet:
    """A fake network that serves one fixture page."""
    return FakeGet(FakeResponse(body=_page(name)))


WFH_NOTE_URL = "https://substack.com/@hithisisbry/note/c-398035566"
WFH_NOTE_TEXT = (
    "Why can't people understand that working from home can help people do "
    "their best work, having the right environment can make an enormous "
    "difference."
)


@pytest.mark.parametrize("query", ["", "?r=9gykro"])
def test_the_sa58_note_is_read_with_and_without_the_r_parameter(query: str) -> None:
    """SA-58 AC3/AC7: the saved page gives the same readable text either way."""
    result = fetch_target(
        WFH_NOTE_URL + query, get=_serve("note_working_from_home.html")
    )
    assert result.ok
    assert result.target_type is TargetType.NOTE
    assert result.text == WFH_NOTE_TEXT


PUBLIC_ADDRESS = "93.184.216.34"


def _answer(address: str) -> list[tuple[Any, ...]]:
    """A getaddrinfo-style answer holding one address."""
    return [(0, 0, 0, "", (address, 443))]


@pytest.fixture(autouse=True)
def public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """No live DNS: every host resolves to a public address unless a test says not."""
    monkeypatch.setattr(
        fetcher_module.socket, "getaddrinfo", lambda *a, **k: _answer(PUBLIC_ADDRESS)
    )


@pytest.fixture
def alerts() -> Iterator[list[str]]:
    """Capture alert texts."""
    sent: list[str] = []

    async def sender(text: str) -> None:
        sent.append(text)

    configure_alerts(sender)
    yield sent
    configure_alerts(None)


# AC1: result carries type, text and (for comments) the parent title


def test_a_post_returns_its_type_and_body_text() -> None:
    """AC1/AC10: HTML is stripped, whitespace is tidied, scripts are dropped."""
    result = fetch_target(POST_URL, get=_serve("post.html"))
    assert result.ok
    assert result.target_type is TargetType.POST
    assert result.parent_title is None
    assert result.text == (
        "First paragraph with a link.\nA heading\n"
        "Second paragraph with odd spacing & an entity.\nOne\nTwo"
    )


def test_a_note_returns_its_type_and_text() -> None:
    """AC1: a Note's text comes from the page description."""
    result = fetch_target(NOTE_URL, get=_serve("note.html"))
    assert result.ok
    assert result.target_type is TargetType.NOTE
    assert result.text == "Just a note about tea.\nSecond line."


def test_a_comment_returns_its_text_and_the_parent_post_title() -> None:
    """AC1: a comment carries the parent title, without the author."""
    result = fetch_target(COMMENT_URL, get=_serve("comment.html"))
    assert result.ok
    assert result.target_type is TargetType.COMMENT
    assert result.text == "I think the tea is overrated."
    assert result.parent_title == 'The "Humble" Post'


def test_a_post_title_containing_the_author_separator_is_kept_whole() -> None:
    """The last " - by " is the author's, so an earlier one stays in the title."""
    page = _page("comment.html").replace(
        b"The &quot;Humble&quot; Post", b"Learn SQL - by Example"
    )
    result = fetch_target(COMMENT_URL, get=FakeGet(FakeResponse(body=page)))
    assert result.parent_title == "Learn SQL - by Example"


def test_a_comment_without_a_recognisable_title_has_no_parent_title() -> None:
    """AC1: the title is only given when it is on the page in the known form."""
    page = _page("comment.html").replace(b"Comments - ", b"")
    result = fetch_target(COMMENT_URL, get=FakeGet(FakeResponse(body=page)))
    assert result.ok
    assert result.parent_title is None


@pytest.mark.parametrize(
    "url",
    [
        "https://example.substack.com/about",
        "https://example.substack.com/",
        "https://substack.com/@example",
    ],
)
def test_an_unsupported_path_is_not_fetched(url: str) -> None:
    """AC8: a link that is not a post, Note or comment is refused, not guessed."""
    get = FakeGet()
    result = fetch_target(url, get=get)
    assert result.status is FetchStatus.COULDNT_READ
    assert get.calls == []


# AC2: non-HTTPS is rejected without a request


@pytest.mark.parametrize(
    "url",
    [
        "http://example.substack.com/p/a-humble-post",
        "ftp://example.substack.com/p/a-humble-post",
        "example.substack.com/p/a-humble-post",
        "https://127.0.0.1/p/a-humble-post",
        "https://user:pw@example.substack.com/p/a-humble-post",
        "https://[::1]/p/a-humble-post",
    ],
)
def test_a_non_https_or_unsafe_url_is_rejected_without_a_request(url: str) -> None:
    """AC2: nothing is requested for a non-HTTPS (or IP-literal) URL."""
    get = FakeGet()
    result = fetch_target(url, get=get)
    assert result.status is FetchStatus.REJECTED
    assert get.calls == []


# AC3 / AC4: Substack hosts and custom domains


@pytest.mark.parametrize(
    "url",
    [
        "https://substack.com/@example/note/c-1",
        "https://example.substack.com/p/a-humble-post",
        "https://deep.sub.substack.com/p/a-humble-post",
    ],
)
def test_substack_hosts_are_accepted_without_a_marker(url: str) -> None:
    """AC3: substack.com and its subdomains need no page marker."""
    page = _page("note.html" if "note" in url else "post.html")
    page = page.replace(b"substackcdn.com", b"cdn.invalid")
    assert fetch_target(url, get=FakeGet(FakeResponse(body=page))).ok


def test_a_lookalike_host_is_not_treated_as_substack() -> None:
    """AC3/AC4: notsubstack.com gets the marker check, so it needs the marker."""
    page = _page("post.html").replace(b"substackcdn.com", b"cdn.invalid")
    result = fetch_target(
        "https://notsubstack.com/p/a-humble-post",
        get=FakeGet(FakeResponse(body=page)),
    )
    assert result.status is FetchStatus.NOT_SUBSTACK


def test_a_custom_domain_substack_page_is_accepted() -> None:
    """AC4: a custom domain with Substack's markers is read."""
    result = fetch_target(CUSTOM_URL, get=_serve("custom_domain_post.html"))
    assert result.ok
    assert result.target_type is TargetType.POST


def test_a_custom_domain_non_substack_page_is_rejected() -> None:
    """AC4: no markers means "not a Substack page", and no text is returned."""
    result = fetch_target(CUSTOM_URL, get=_serve("not_substack.html"))
    assert result.status is FetchStatus.NOT_SUBSTACK
    assert result.text == ""
    assert "not a Substack page" in result.reason


def test_a_page_with_only_a_cdn_image_is_not_a_substack_page() -> None:
    """AC4: one marker alone is not enough."""
    page = _page("not_substack.html").replace(
        b"</head>", b'<link href="https://substackcdn.com/x.css"></head>'
    )
    result = fetch_target(CUSTOM_URL, get=FakeGet(FakeResponse(body=page)))
    assert result.status is FetchStatus.NOT_SUBSTACK


# AC5: a plain unauthenticated GET


def test_the_request_is_a_plain_get_with_no_credentials() -> None:
    """AC5: no cookies or auth are sent, and the Substack publisher is not used."""
    get = _serve("post.html")
    fetch_target(POST_URL, get=get)
    ((_, kwargs),) = get.calls
    assert set(kwargs["headers"]) == {"User-Agent", "Accept"}
    assert "cookies" not in kwargs
    assert "auth" not in kwargs
    tree = ast.parse((ROOT / "src" / "humblebrag" / "fetcher.py").read_text())
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    }
    assert not {name for name in imported if "substack" in name.split(".")[0]}
    assert "humblebrag.publisher" not in imported


def test_the_default_getter_is_the_pinned_requests_getter() -> None:
    """AC5: the real fetch is a plain ``requests`` GET with no session state."""
    assert fetcher_module.fetch_target.__defaults__ is not None
    assert fetcher_module.fetch_target.__defaults__[1] is fetcher_module.get_pinned


# DNS rebinding: the request connects to the address that was vetted


def test_the_vetted_address_is_passed_to_the_request() -> None:
    """The address checked is the address handed to the getter, on every hop."""
    get = FakeGet(_redirect(CUSTOM_URL), FakeResponse(body=_page("post.html")))
    fetch_target(POST_URL, get=get)
    assert [call[1]["address"] for call in get.calls] == [PUBLIC_ADDRESS] * 2


def _capture_pinned_request(
    monkeypatch: pytest.MonkeyPatch, url: str, address: str
) -> tuple[requests.PreparedRequest, HTTPAdapter]:
    """Run ``get_pinned`` against a stub transport; return what it would send."""
    seen: list[tuple[requests.PreparedRequest, HTTPAdapter]] = []

    def send(
        self: HTTPAdapter, request: requests.PreparedRequest, **kwargs: Any
    ) -> requests.Response:
        seen.append((request, self))
        response = requests.Response()
        response.status_code = 200
        return response

    monkeypatch.setattr(HTTPAdapter, "send", send)
    get_pinned(url, address, headers={"Accept": "x"}, timeout=3, stream=True)
    return seen[0]


def test_the_pinned_getter_connects_to_the_ip_and_keeps_the_hostname(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The URL carries the IP; Host, SNI and certificate checks use the name."""
    request, adapter = _capture_pinned_request(
        monkeypatch, "https://blog.example.org/p/a-humble-post?x=1", PUBLIC_ADDRESS
    )
    assert request.url == f"https://{PUBLIC_ADDRESS}/p/a-humble-post?x=1"
    assert request.headers["Host"] == "blog.example.org"
    assert request.headers["Accept"] == "x"
    kwargs = adapter.poolmanager.connection_pool_kw
    assert kwargs["server_hostname"] == "blog.example.org"
    assert kwargs["assert_hostname"] == "blog.example.org"


def test_the_pinned_getter_brackets_an_ipv6_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An IPv6 answer is a valid URL host."""
    request, _ = _capture_pinned_request(
        monkeypatch, "https://blog.example.org:8443/p/x", "2606:2800:220:1::1"
    )
    assert request.url == "https://[2606:2800:220:1::1]:8443/p/x"
    assert request.headers["Host"] == "blog.example.org:8443"


def test_closing_a_pinned_response_closes_its_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No session or connection outlives the response."""
    closed: list[bool] = []
    monkeypatch.setattr(
        fetcher_module.requests.Session, "close", lambda self: closed.append(True)
    )

    def send(
        self: HTTPAdapter, request: requests.PreparedRequest, **kwargs: Any
    ) -> requests.Response:
        response = requests.Response()
        response.status_code = 200
        return response

    monkeypatch.setattr(HTTPAdapter, "send", send)
    response = get_pinned(POST_URL, PUBLIC_ADDRESS, headers={}, timeout=3)
    response.close()
    assert closed == [True]


def test_a_content_type_must_match_exactly() -> None:
    """text/html-something is not HTML; parameters after a semicolon are fine."""
    odd = FakeResponse(body=_page("post.html"), headers={"Content-Type": "text/html-x"})
    assert fetch_target(POST_URL, get=FakeGet(odd)).status is FetchStatus.COULDNT_READ
    upper = FakeResponse(
        body=_page("post.html"), headers={"Content-Type": "Text/HTML ; charset=UTF-8"}
    )
    assert fetch_target(POST_URL, get=FakeGet(upper)).ok


# AC6: timeout, redirects, size


def test_the_timeout_defaults_to_ten_seconds_and_is_configurable() -> None:
    """AC6: 10 s by default, from FETCH_TIMEOUT_SECONDS in config."""
    assert DEFAULT_TIMEOUT_SECONDS == 10.0
    assert DEFAULT_TIMEOUT_SECONDS == float(DEFAULT_FETCH_TIMEOUT_SECONDS)
    assert DEFAULT_FETCH_TIMEOUT_SECONDS == 10
    env = {
        "TELEGRAM_BOT_TOKEN": "t",
        "TELEGRAM_CHAT_ID": "1",
        "ANTHROPIC_API_KEY": "k",
        "CLAUDE_MODEL": "m",
        "SUBSTACK_PUBLICATION_URL": "https://example.substack.com",
        "SUBSTACK_COOKIE": "c",
    }
    assert load_config(env).fetch_timeout_seconds == 10
    assert load_config({**env, "FETCH_TIMEOUT_SECONDS": "3"}).fetch_timeout_seconds == 3


def test_the_timeout_is_passed_to_every_request() -> None:
    """AC6: each request has a timeout no longer than the configured one."""
    get = _serve("post.html")
    fetch_target(POST_URL, timeout=4, get=get)
    assert 0 < get.calls[0][1]["timeout"] <= 4


def _redirect(location: str) -> FakeResponse:
    """A 302 to ``location``."""
    return FakeResponse(302, headers={"Location": location})


def test_up_to_five_redirects_are_followed() -> None:
    """AC6: the fifth redirect is followed and the page read."""
    hops = [_redirect(f"https://example.substack.com/p/hop{n}") for n in range(5)]
    get = FakeGet(*hops, FakeResponse(body=_page("post.html")))
    assert fetch_target(POST_URL, get=get).ok
    assert len(get.calls) == MAX_REDIRECTS + 1
    assert all(call[1]["allow_redirects"] is False for call in get.calls)


def test_a_sixth_redirect_is_refused() -> None:
    """AC6: more than five redirects is a "couldn't read"."""
    hops = [_redirect(f"https://example.substack.com/p/hop{n}") for n in range(6)]
    get = FakeGet(*hops)
    result = fetch_target(POST_URL, get=get)
    assert result.status is FetchStatus.COULDNT_READ
    assert "redirect" in result.reason
    assert len(get.calls) == MAX_REDIRECTS + 1


def test_a_relative_redirect_is_followed() -> None:
    """AC6: a relative Location is resolved against the current URL."""
    get = FakeGet(_redirect("/p/moved"), FakeResponse(body=_page("post.html")))
    assert fetch_target(POST_URL, get=get).ok
    assert get.calls[1][0] == "https://example.substack.com/p/moved"


def test_a_page_of_exactly_two_megabytes_is_read_and_one_byte_more_is_not() -> None:
    """AC6: reads at most 2 MB of body."""
    page = _page("post.html")
    exact = page + b" " * (MAX_BODY_BYTES - len(page))
    assert fetch_target(POST_URL, get=FakeGet(FakeResponse(body=exact))).ok
    result = fetch_target(POST_URL, get=FakeGet(FakeResponse(body=exact + b" ")))
    assert result.status is FetchStatus.COULDNT_READ
    assert "too large" in result.reason


def test_a_slow_body_runs_into_the_overall_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC6: the timeout covers the whole fetch, not just each socket read."""
    clock = iter([0.0, 0.0, 99.0, 99.0, 99.0])
    monkeypatch.setattr(fetcher_module.time, "monotonic", lambda: next(clock))
    result = fetch_target(POST_URL, timeout=5, get=_serve("post.html"))
    assert result.status is FetchStatus.COULDNT_READ
    assert "timed out" in result.reason


# AC7: redirects are re-checked


@pytest.mark.parametrize(
    "location",
    ["http://example.substack.com/p/a-humble-post", "ftp://example.org/x"],
)
def test_a_redirect_to_a_non_https_url_is_refused(location: str) -> None:
    """AC7: no request is made to the non-HTTPS target."""
    get = FakeGet(_redirect(location))
    result = fetch_target(POST_URL, get=get)
    assert result.status is FetchStatus.REJECTED
    assert len(get.calls) == 1


def test_a_redirect_to_a_non_substack_host_needs_the_marker() -> None:
    """AC4/AC7: the final host is checked, not just the first one."""
    get = FakeGet(
        _redirect("https://elsewhere.example.org/p/a-humble-post"),
        FakeResponse(body=_page("not_substack.html")),
    )
    result = fetch_target(POST_URL, get=get)
    assert result.status is FetchStatus.NOT_SUBSTACK


def test_a_redirect_to_a_custom_domain_with_markers_is_read() -> None:
    """AC4/AC7: a publication.substack.com to custom-domain redirect works."""
    get = FakeGet(
        _redirect(CUSTOM_URL), FakeResponse(body=_page("custom_domain_post.html"))
    )
    assert fetch_target(POST_URL, get=get).ok


# SSRF: hosts that resolve to private or internal addresses are never requested


@pytest.mark.parametrize(
    "address",
    ["127.0.0.1", "10.0.0.5", "192.168.1.20", "169.254.169.254", "::1", "fe80::1%eth0"],
)
def test_a_host_resolving_to_a_private_address_is_not_requested(
    monkeypatch: pytest.MonkeyPatch, address: str
) -> None:
    """A custom domain pointed at an internal address gets no request."""
    monkeypatch.setattr(
        fetcher_module.socket, "getaddrinfo", lambda *a, **k: _answer(address)
    )
    get = FakeGet()
    result = fetch_target(CUSTOM_URL, get=get)
    assert result.status is FetchStatus.REJECTED
    assert "private" in result.reason
    assert get.calls == []


def test_one_private_answer_among_public_ones_blocks_the_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every resolved address must be public, not just the first."""
    answers = _answer(PUBLIC_ADDRESS) + _answer("10.0.0.5")
    monkeypatch.setattr(fetcher_module.socket, "getaddrinfo", lambda *a, **k: answers)
    assert fetch_target(CUSTOM_URL, get=FakeGet()).status is FetchStatus.REJECTED


def test_a_redirect_to_a_private_host_is_not_followed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A public page cannot bounce the fetch onto an internal host."""

    def resolve(host: str, *args: Any, **kwargs: Any) -> list[tuple[Any, ...]]:
        return _answer("10.0.0.5" if host == "internal.example.org" else PUBLIC_ADDRESS)

    monkeypatch.setattr(fetcher_module.socket, "getaddrinfo", resolve)
    get = FakeGet(_redirect("https://internal.example.org/p/x"))
    result = fetch_target(POST_URL, get=get)
    assert result.status is FetchStatus.REJECTED
    assert len(get.calls) == 1


def test_a_host_that_does_not_resolve_is_a_couldnt_read_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A DNS failure does not raise and nothing is requested."""

    def fail(*args: Any, **kwargs: Any) -> list[tuple[Any, ...]]:
        raise socket.gaierror("no such host")

    monkeypatch.setattr(fetcher_module.socket, "getaddrinfo", fail)
    get = FakeGet()
    result = fetch_target(POST_URL, get=get)
    assert result.status is FetchStatus.COULDNT_READ
    assert get.calls == []


def test_a_hung_dns_lookup_runs_into_the_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ERR-02: a resolver that never answers cannot outlast the fetch timeout."""
    release = threading.Event()

    def hang(*args: Any, **kwargs: Any) -> list[tuple[Any, ...]]:
        release.wait(5)
        return _answer(PUBLIC_ADDRESS)

    monkeypatch.setattr(fetcher_module.socket, "getaddrinfo", hang)
    get = FakeGet()
    try:
        result = fetch_target(POST_URL, timeout=0.05, get=get)
    finally:
        release.set()
    assert result.status is FetchStatus.COULDNT_READ
    assert "timed out" in result.reason
    assert get.calls == []


def test_stuck_lookups_are_capped_and_free_their_slot_when_they_finish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stalling resolver can hold only a few threads, and recovers afterwards."""
    release = threading.Event()
    slots_freed = threading.Semaphore(0)
    calls: list[str] = []

    def hang(host: str, *args: Any, **kwargs: Any) -> list[tuple[Any, ...]]:
        calls.append(host)
        release.wait(5)
        return _answer(PUBLIC_ADDRESS)

    real_release = fetcher_module._lookup_slots.release

    def release_and_signal() -> None:
        real_release()
        slots_freed.release()

    monkeypatch.setattr(fetcher_module._lookup_slots, "release", release_and_signal)

    monkeypatch.setattr(fetcher_module.socket, "getaddrinfo", hang)
    try:
        for _ in range(fetcher_module.MAX_PENDING_LOOKUPS):
            fetch_target(POST_URL, timeout=0.02, get=FakeGet())
        before = len(calls)
        result = fetch_target(POST_URL, timeout=0.02, get=FakeGet())
        assert "stuck" in result.reason
        assert len(calls) == before  # no new thread was started
    finally:
        release.set()
    for _ in range(fetcher_module.MAX_PENDING_LOOKUPS):  # wait for the slots back
        assert slots_freed.acquire(timeout=5)
    monkeypatch.setattr(
        fetcher_module.socket, "getaddrinfo", lambda *a, **k: _answer(PUBLIC_ADDRESS)
    )
    assert fetch_target(POST_URL, get=_serve("post.html")).ok


# AC8: clear "couldn't read" results, never an exception


@pytest.mark.parametrize("status", [403, 404, 429, 500])
def test_an_http_error_is_a_couldnt_read_result(status: int) -> None:
    """AC8: HTTP errors do not raise and carry no text."""
    result = fetch_target(POST_URL, get=FakeGet(FakeResponse(status)))
    assert result.status is FetchStatus.COULDNT_READ
    assert str(status) in result.reason
    assert result.text == ""


@pytest.mark.parametrize(
    "error",
    [
        requests.Timeout("slow"),
        requests.ConnectionError("down"),
        requests.exceptions.TooManyRedirects("loop"),
        requests.exceptions.SSLError("bad cert"),
        OSError("reset"),
    ],
)
def test_a_network_error_is_a_couldnt_read_result(error: Exception) -> None:
    """AC8: a timeout or connection failure does not raise."""
    result = fetch_target(POST_URL, get=FakeGet(error))
    assert result.status is FetchStatus.COULDNT_READ
    assert result.text == ""


def test_a_timeout_says_so() -> None:
    """AC8: the timeout reason is clear."""
    result = fetch_target(POST_URL, get=FakeGet(requests.Timeout("slow")))
    assert "timed out" in result.reason


def test_a_javascript_only_page_has_no_readable_text() -> None:
    """AC8: nothing is guessed from the URL or the page title."""
    for url in (POST_URL, COMMENT_URL, NOTE_URL):
        result = fetch_target(url, get=_serve("js_only.html"))
        assert result.status is FetchStatus.COULDNT_READ
        assert result.text == ""
        assert "JavaScript" in result.reason


def test_a_non_html_response_is_not_read() -> None:
    """AC8: a PDF or JSON body is a "couldn't read"."""
    body = FakeResponse(body=b"%PDF", headers={"Content-Type": "application/pdf"})
    result = fetch_target(POST_URL, get=FakeGet(body))
    assert result.status is FetchStatus.COULDNT_READ


def test_responses_are_always_closed() -> None:
    """AC6/AC8: even a refused redirect or error response is released."""
    redirect = _redirect("http://example.org/x")
    fetch_target(POST_URL, get=FakeGet(redirect))
    error = FakeResponse(500)
    fetch_target(POST_URL, get=FakeGet(error))
    assert redirect.closed and error.closed


# AC9: no names


def test_results_carry_no_author_or_display_names() -> None:
    """AC9: neither the text nor the title includes a name from the page."""
    for url, name in (
        (POST_URL, "post.html"),
        (NOTE_URL, "note.html"),
        (COMMENT_URL, "comment.html"),
    ):
        result = fetch_target(url, get=_serve(name))
        shown = f"{result.text} {result.parent_title} {result.reason}"
        for personal in ("Example Author", "Some Commenter", "@example"):
            assert personal not in shown
    assert set(vars(result)) == {
        "status",
        "reason",
        "target_type",
        "text",
        "parent_title",
    }


# AC10: length cap


@pytest.mark.parametrize(
    ("name", "url", "kind"),
    [
        ("post.html", POST_URL, TargetType.POST),
        ("note.html", NOTE_URL, TargetType.NOTE),
        ("comment.html", COMMENT_URL, TargetType.COMMENT),
    ],
)
def test_text_is_capped_for_the_prompt(name: str, url: str, kind: TargetType) -> None:
    """AC10: long text is cut to the limit for its type."""
    limit = TEXT_LIMITS[kind.value]
    filler = ("word " * limit).encode()
    page = _page(name)
    if kind is TargetType.POST:
        page = page.replace(b"<p><span>First", b"<p>" + filler + b"</p><p><span>First")
    else:
        page = page.replace(
            b'og:description" content="', b'og:description" content="' + filler
        )
    result = fetch_target(url, get=FakeGet(FakeResponse(body=page)))
    assert result.ok
    assert len(result.text) == limit - 1  # cut at the limit, trailing space removed
    assert not result.text.endswith(" ")


# Alerting: a failed read tells Stuart (ERR-01); bad input does not


async def test_a_failed_read_alerts_stuart(alerts: list[str]) -> None:
    """ERR-01/TST-04: an HTTP failure produces an alert and a result."""
    result = await read_substack_link(POST_URL, get=FakeGet(FakeResponse(500)))
    assert result.status is FetchStatus.COULDNT_READ
    assert len(alerts) == 1
    assert "Substack link fetch" in alerts[0]
    assert "500" in alerts[0]


async def test_a_timeout_alerts_stuart(alerts: list[str]) -> None:
    """TST-04: a timeout produces an alert."""
    await read_substack_link(POST_URL, get=FakeGet(requests.Timeout("slow")))
    assert len(alerts) == 1


async def test_a_good_read_sends_no_alert(alerts: list[str]) -> None:
    """A successful read is silent."""
    result = await read_substack_link(POST_URL, get=_serve("post.html"))
    assert result.ok
    assert alerts == []


@pytest.mark.parametrize(
    "make_get",
    [
        lambda: FakeGet(),
        lambda: _serve("not_substack.html"),
    ],
)
async def test_bad_input_is_reported_to_the_caller_without_an_alert(
    alerts: list[str], make_get: Callable[[], FakeGet]
) -> None:
    """Non-HTTPS and non-Substack links are the user's mistake, not a failure."""
    url = "http://example.org/p/x" if not make_get().responses else CUSTOM_URL
    result = await read_substack_link(url, get=make_get())
    assert not result.ok
    assert alerts == []


# Dependencies and safety


def test_requests_is_a_pinned_direct_dependency() -> None:
    """The HTTP client is declared in pyproject.toml, pinned (SEC-08)."""
    data = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert any(dep.startswith("requests==") for dep in data["project"]["dependencies"])
