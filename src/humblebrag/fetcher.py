"""Fetch a public Substack link and extract the text of the post, Note or comment.

This is a plain, unauthenticated GET. It never touches the publishing session,
its cookies or python-substack. It returns names-free plain text only.
"""

import asyncio
import ipaddress
import logging
import re
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import requests

from humblebrag.alerts import alert

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 10.0
MAX_REDIRECTS = 5
MAX_BODY_BYTES = 2 * 1024 * 1024
CHUNK_BYTES = 8192
USER_AGENT = "Mozilla/5.0 (compatible; SirHumblebrag/3.0)"
ACCEPT = "text/html,application/xhtml+xml"
SUBSTACK_DOMAIN = "substack.com"
SUBSTACK_CDN_MARKER = "substackcdn.com"
SUBSTACK_PRELOADS_MARKER = 'id="preloads-json"'
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
HTML_CONTENT_TYPES = ("text/html", "application/xhtml+xml")
HTTPS_PORT = 443
PRIVATE_HOST_REASON = "That link points at a private or internal address."
ALERT_EVENT = "Substack link fetch"
NOT_SUBSTACK_REASON = "That is not a Substack page."
COMMENTS_TITLE_PREFIX = "Comments - "
TITLE_AUTHOR_SEPARATOR = " - by "
POST_BODY_CLASSES = frozenset({"body", "markup"})
BLOCK_TAGS = frozenset(
    {"p", "br", "div", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6"}
    | {"blockquote", "pre", "tr", "hr", "figure", "figcaption", "section"}
)
SKIPPED_TAGS = frozenset({"script", "style", "noscript", "template"})
VOID_TAGS = frozenset({"br", "hr", "img", "input", "meta", "link", "source", "wbr"})
TEXT_LIMITS = {"post": 4000, "note": 2000, "comment": 2000}
COMMENT_PATH = re.compile(r"^/p/[^/]+/comment/\d+/?$")
POST_PATH = re.compile(r"^/p/[^/]+/?$")
NOTE_PATH = re.compile(r"^/(@[^/]+|profile/[^/]+)/note/c-\d+/?$|^/note/c-\d+/?$")

Getter = Callable[..., requests.Response]


class TargetType(StrEnum):
    """What kind of Substack item a link points at."""

    POST = "post"
    NOTE = "note"
    COMMENT = "comment"


class FetchStatus(StrEnum):
    """The outcome of a fetch."""

    OK = "ok"
    REJECTED = "rejected"
    NOT_SUBSTACK = "not a Substack page"
    COULDNT_READ = "couldn't read"


@dataclass(frozen=True)
class FetchResult:
    """What the fetcher found. It carries no author or display names."""

    status: FetchStatus
    reason: str = ""
    target_type: TargetType | None = None
    text: str = ""
    parent_title: str | None = None

    @property
    def ok(self) -> bool:
        """True when readable target text was found."""
        return self.status is FetchStatus.OK


class _FetchError(Exception):
    """A fetch failed in a way that maps to a result, with a safe reason."""

    def __init__(self, status: FetchStatus, reason: str) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason


class _PageParser(HTMLParser):
    """Collect the meta tags, the title and the post body text of one page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.title = ""
        self._in_title = False
        self._skip_depth = 0
        self._body_depth = 0
        self._body_done = False
        self._body_parts: list[str] = []

    @property
    def body_text(self) -> str:
        """The raw text of the first post body block."""
        return "".join(self._body_parts)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Track meta tags, the title and entry into the post body block."""
        values = {name: value or "" for name, value in attrs}
        if tag == "meta" and values.get("property"):
            self.meta.setdefault(values["property"], values.get("content", ""))
        elif tag == "title":
            self._in_title = True
        if tag in VOID_TAGS:
            if self._body_depth and tag in BLOCK_TAGS:
                self._body_parts.append("\n")
            return
        if self._body_depth:
            self._body_depth += 1
            if tag in SKIPPED_TAGS:
                self._skip_depth += 1
            if tag in BLOCK_TAGS:
                self._body_parts.append("\n")
        elif (
            tag == "div"
            and not self._body_done
            and POST_BODY_CLASSES <= set(values.get("class", "").split())
        ):
            self._body_depth = 1

    def handle_endtag(self, tag: str) -> None:
        """Track leaving the title and the post body block."""
        if tag == "title":
            self._in_title = False
        if tag in VOID_TAGS or not self._body_depth:
            return
        if tag in SKIPPED_TAGS and self._skip_depth:
            self._skip_depth -= 1
        if tag in BLOCK_TAGS:
            self._body_parts.append("\n")
        self._body_depth -= 1
        if not self._body_depth:
            self._body_done = True

    def handle_data(self, data: str) -> None:
        """Collect title text and post body text."""
        if self._in_title:
            self.title += data
        if self._body_depth and not self._skip_depth:
            self._body_parts.append(data.replace("\n", " "))


def tidy_text(text: str, limit: int) -> str:
    """Collapse whitespace, keep paragraph breaks and cap at ``limit`` characters."""
    lines = (" ".join(line.split()) for line in text.replace("\xa0", " ").split("\n"))
    paragraphs = "\n".join(line for line in lines if line)
    return paragraphs[:limit].rstrip()


def _is_substack_host(host: str) -> bool:
    """True for substack.com and any subdomain of it."""
    return host == SUBSTACK_DOMAIN or host.endswith("." + SUBSTACK_DOMAIN)


def _is_ip_literal(host: str) -> bool:
    """True when the host is a bare IP address."""
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return True


def _check_url(url: str) -> str:
    """Return the lower-case host of an acceptable HTTPS URL, else raise."""
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").rstrip(".").lower()
        _ = parts.port
    except ValueError:
        raise _FetchError(FetchStatus.REJECTED, "That is not a valid link.") from None
    if parts.scheme != "https":
        raise _FetchError(FetchStatus.REJECTED, "Only https links are accepted.")
    if not host or _is_ip_literal(host) or "@" in parts.netloc:
        raise _FetchError(FetchStatus.REJECTED, "That is not a valid link.")
    return host


def _require_public_host(host: str) -> None:
    """Refuse a host that does not resolve, or resolves to a non-public address."""
    try:
        infos = socket.getaddrinfo(host, HTTPS_PORT, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError):
        raise _FetchError(
            FetchStatus.COULDNT_READ, "That link's host could not be found."
        ) from None
    addresses = {info[4][0] for info in infos}
    if not addresses or not all(
        ipaddress.ip_address(address.split("%")[0]).is_global for address in addresses
    ):
        raise _FetchError(FetchStatus.REJECTED, PRIVATE_HOST_REASON)


def classify_path(path: str) -> TargetType:
    """Work out what a Substack path points at, or raise if it is not supported."""
    if COMMENT_PATH.match(path):
        return TargetType.COMMENT
    if NOTE_PATH.match(path):
        return TargetType.NOTE
    if POST_PATH.match(path):
        return TargetType.POST
    raise _FetchError(
        FetchStatus.COULDNT_READ, "That link is not a post, Note or comment."
    )


def _read_body(response: requests.Response, deadline: float) -> str:
    """Read at most MAX_BODY_BYTES of the body, within the overall deadline."""
    content_type = response.headers.get("Content-Type", "").lower()
    if not content_type.startswith(HTML_CONTENT_TYPES):
        raise _FetchError(FetchStatus.COULDNT_READ, "The page is not HTML.")
    chunks: list[bytes] = []
    size = 0
    for chunk in response.iter_content(chunk_size=CHUNK_BYTES):
        if time.monotonic() > deadline:
            raise _FetchError(FetchStatus.COULDNT_READ, "The fetch timed out.")
        size += len(chunk)
        if size > MAX_BODY_BYTES:
            raise _FetchError(FetchStatus.COULDNT_READ, "The page is too large.")
        chunks.append(chunk)
    return b"".join(chunks).decode(response.encoding or "utf-8", errors="replace")


def _fetch_html(url: str, timeout: float, get: Getter) -> tuple[str, str]:
    """GET ``url``, re-checking every redirect; return (final host, page html)."""
    deadline = time.monotonic() + timeout
    host = _check_url(url)
    for _hop in range(MAX_REDIRECTS + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _FetchError(FetchStatus.COULDNT_READ, "The fetch timed out.")
        _require_public_host(host)
        response = get(
            url,
            headers={"User-Agent": USER_AGENT, "Accept": ACCEPT},
            timeout=remaining,
            allow_redirects=False,
            stream=True,
        )
        try:
            if response.status_code in REDIRECT_STATUSES:
                location = response.headers.get("Location", "")
                url = urljoin(url, location)
                host = _check_url(url)
                continue
            if response.status_code != 200:
                raise _FetchError(
                    FetchStatus.COULDNT_READ,
                    f"Substack returned HTTP {response.status_code}.",
                )
            return host, _read_body(response, deadline)
        finally:
            response.close()
    raise _FetchError(FetchStatus.COULDNT_READ, "Too many redirects.")


def _is_substack_page(html: str) -> bool:
    """True when the page carries Substack's CDN assets and preload data."""
    return SUBSTACK_CDN_MARKER in html and SUBSTACK_PRELOADS_MARKER in html


def _parent_title(page_title: str) -> str | None:
    """Pull the post title out of "Comments - <title> - by <author>"."""
    title = page_title.strip()
    if not title.startswith(COMMENTS_TITLE_PREFIX):
        return None
    title = title.removeprefix(COMMENTS_TITLE_PREFIX)
    if TITLE_AUTHOR_SEPARATOR not in title:
        return None
    title = title.rsplit(TITLE_AUTHOR_SEPARATOR, 1)[0]
    return " ".join(title.split()) or None


def extract_target(html: str, target_type: TargetType) -> FetchResult:
    """Pull the target text (and a comment's parent title) out of a page."""
    parser = _PageParser()
    parser.feed(html)
    parser.close()
    if target_type is TargetType.POST:
        raw = parser.body_text
    else:
        raw = parser.meta.get("og:description", "")
    text = tidy_text(raw, TEXT_LIMITS[target_type.value])
    if not text:
        return FetchResult(
            FetchStatus.COULDNT_READ,
            "The page has no readable text (it may be drawn by JavaScript).",
        )
    parent = _parent_title(parser.title) if target_type is TargetType.COMMENT else None
    return FetchResult(FetchStatus.OK, "", target_type, text, parent)


def fetch_target(
    url: str,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    get: Getter = requests.get,
) -> FetchResult:
    """Fetch a Substack link and return its text; never raises.

    Blocking: async callers use ``read_substack_link`` instead.
    """
    try:
        _check_url(url)
        target_type = classify_path(urlsplit(url).path)
        final_host, html = _fetch_html(url, timeout, get)
        if not _is_substack_host(final_host) and not _is_substack_page(html):
            return FetchResult(FetchStatus.NOT_SUBSTACK, NOT_SUBSTACK_REASON)
        return extract_target(html, target_type)
    except _FetchError as error:
        return FetchResult(error.status, error.reason)
    except requests.Timeout:
        return FetchResult(FetchStatus.COULDNT_READ, "The fetch timed out.")
    except (requests.RequestException, OSError, ValueError) as error:
        logger.warning("Substack link fetch failed: %s", type(error).__name__)
        return FetchResult(FetchStatus.COULDNT_READ, "The page could not be fetched.")


async def read_substack_link(
    url: str,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    get: Getter = requests.get,
) -> FetchResult:
    """Fetch off the event loop; alert Stuart when a real read fails."""
    result = await asyncio.to_thread(fetch_target, url, timeout, get)
    if result.status is FetchStatus.COULDNT_READ:
        await alert(ALERT_EVENT, result.reason)
    return result
