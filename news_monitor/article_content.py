"""Bounded public-article inspection for body-only ICBC Phnom Penh mentions.

No scripts, browser challenges, sign-in flows, or refresh redirects are executed.
Baijiahao is explicitly unavailable: neither it nor an authentication/challenge
destination may be fetched. Publisher retrieval uses certificate-verified HTTPS
to a validated public IP, with every redirect independently checked.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from html import unescape
from html.parser import HTMLParser
import http.client
import ipaddress
import json
import queue
import re
import socket
import ssl
import threading
import time
import unicodedata
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit


MAX_ARTICLE_BYTES = 2 * 1024 * 1024
ARTICLE_TIMEOUT_SECONDS = 10
MAX_REDIRECTS = 3
MIN_BODY_CHARACTERS = 24
_DENIED_HOSTS = ("baijiahao.baidu.com", "wappass.baidu.com", "passport.baidu.com")
_DENIED_LABELS = frozenset({"login", "signin", "captcha", "wappass", "passport"})
_SEARCH_HOSTS = frozenset({"news.google.com", "www.google.com", "google.com", "www.bing.com", "bing.com", "www.baidu.com", "baidu.com"})
_AUTH_PATH = re.compile(r"(?:^|/)(?:login|signin|sign-in|captcha|wappass|passport|challenge|verification)(?:/|$|[.?])", re.I)


@dataclass(frozen=True)
class ContentEvidence:
    verified: bool
    matched: bool
    body_excerpt: str = ""
    resolved_url: str = ""
    error: str = ""


class ArticleContentError(ValueError):
    """A controlled, credential-free article-fetch failure."""


def _is_public_ip(address: str) -> bool:
    try:
        value = ipaddress.ip_address(address)
        return value.is_global and not any((value.is_private, value.is_loopback,
                                           value.is_link_local, value.is_multicast,
                                           value.is_reserved, value.is_unspecified))
    except ValueError:
        return False


def _validated_url(url: str) -> tuple[str, str]:
    if not isinstance(url, str) or any(ord(char) < 32 or ord(char) == 127 for char in url):
        raise ArticleContentError("Article URL is invalid")
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").rstrip(".").encode("idna").decode("ascii").lower()
        port = parsed.port
    except (ValueError, UnicodeError):
        raise ArticleContentError("Article URL is invalid") from None
    if parsed.scheme.lower() != "https" or not host or parsed.username is not None or parsed.password is not None or port not in {None, 443}:
        raise ArticleContentError("Article URL must be public HTTPS without credentials")
    if host in {"localhost", "localhost.localdomain"} or host.endswith((".localhost", ".local", ".internal", ".test", ".invalid")):
        raise ArticleContentError("Article host is not a public publisher")
    if any(host == denied or host.endswith("." + denied) for denied in _DENIED_HOSTS) or set(host.split(".")).intersection(_DENIED_LABELS):
        raise ArticleContentError("Article host is unavailable or requires authentication")
    if _AUTH_PATH.search(unquote(parsed.path)):
        raise ArticleContentError("Article destination is an authentication or verification page")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if "." not in host or not re.fullmatch(r"[a-z0-9.-]+", host) or ".." in host or re.fullmatch(r"[0-9.]+", host):
            raise ArticleContentError("Article host is not a public domain") from None
    else:
        if not _is_public_ip(host):
            raise ArticleContentError("Article address is not public")
    authority = f"[{host}]" if ":" in host else host
    return urlunsplit(("https", authority, parsed.path or "/", parsed.query, "")), host


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ArticleContentError("Article request exceeded its time limit")
    return remaining


def _resolve_public_addresses(host: str, deadline: float) -> list[str]:
    """Bound DNS wait and reject mixed public/private answers before connecting."""
    try:
        ipaddress.ip_address(host)
    except ValueError:
        results: queue.Queue = queue.Queue(maxsize=1)

        def resolve():
            try:
                results.put(socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM))
            except Exception as exc:
                results.put(exc)

        threading.Thread(target=resolve, daemon=True).start()
        try:
            records = results.get(timeout=_remaining(deadline))
        except queue.Empty:
            raise ArticleContentError("Article DNS lookup timed out") from None
        if isinstance(records, Exception):
            raise ArticleContentError("Article DNS lookup failed") from None
        addresses = list(dict.fromkeys(record[4][0] for record in records))
    else:
        addresses = [host]
    if not addresses or not all(_is_public_ip(address) for address in addresses):
        raise ArticleContentError("Article DNS did not resolve exclusively to public addresses")
    return addresses


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, address: str, timeout: float):
        super().__init__(host, 443, timeout=timeout, context=ssl.create_default_context())
        self._public_address = address
        self._connection_deadline = time.monotonic() + timeout

    def connect(self):
        # Connect directly to the prevalidated numeric address. DNS is not
        # resolved a second time; TLS still verifies the publisher hostname.
        family = socket.AF_INET6 if ":" in self._public_address else socket.AF_INET
        raw = socket.socket(family, socket.SOCK_STREAM)
        raw.settimeout(_remaining(self._connection_deadline))
        try:
            address = (self._public_address, 443, 0, 0) if family == socket.AF_INET6 else (self._public_address, 443)
            raw.connect(address)
            raw.settimeout(_remaining(self._connection_deadline))
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
            self.sock.settimeout(_remaining(self._connection_deadline))
        except Exception:
            raw.close()
            raise


def _fetch_article(url: str) -> tuple[bytes, str, str]:
    deadline = time.monotonic() + ARTICLE_TIMEOUT_SECONDS
    current, _ = _validated_url(url)
    for redirects in range(MAX_REDIRECTS + 1):
        current, host = _validated_url(current)
        addresses = _resolve_public_addresses(host, deadline)
        parsed = urlsplit(current)
        request_path = urlunsplit(("", "", parsed.path, parsed.query, ""))
        connection = _PinnedHTTPSConnection(host, addresses[0], _remaining(deadline))
        try:
            connection.request("GET", request_path, headers={
                "User-Agent": "ICBCPhnomPenhNewsMonitor/1.0 (public article relevance check)",
                "Accept": "text/html,application/xhtml+xml;q=0.9",
                "Accept-Encoding": "identity",
                "Connection": "close",
            })
            if connection.sock is not None:
                connection.sock.settimeout(_remaining(deadline))
            response = connection.getresponse()
            if response.status in {301, 302, 303, 307, 308}:
                if redirects >= MAX_REDIRECTS:
                    raise ArticleContentError("Article redirect limit exceeded")
                location = response.getheader("Location")
                if not location:
                    raise ArticleContentError("Article redirect has no destination")
                current, _ = _validated_url(urljoin(current, location))
                continue
            if not 200 <= response.status < 300:
                raise ArticleContentError(f"Article request failed (HTTP {response.status})")
            content_type = response.getheader("Content-Type", "")
            if content_type.split(";", 1)[0].strip().lower() not in {"text/html", "application/xhtml+xml"}:
                raise ArticleContentError("Article did not return an HTML document")
            if response.getheader("Content-Encoding", "identity").lower() not in {"", "identity"}:
                raise ArticleContentError("Article returned unsupported content encoding")
            length = response.getheader("Content-Length", "")
            if length.isdigit() and int(length) > MAX_ARTICLE_BYTES:
                raise ArticleContentError("Article exceeds the maximum response size")
            chunks, size = [], 0
            while True:
                if connection.sock is not None:
                    connection.sock.settimeout(_remaining(deadline))
                chunk = response.read(min(65_536, MAX_ARTICLE_BYTES + 1 - size))
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_ARTICLE_BYTES:
                    raise ArticleContentError("Article exceeds the maximum response size")
                chunks.append(chunk)
            _remaining(deadline)
            return b"".join(chunks), current, content_type
        finally:
            connection.close()
    raise ArticleContentError("Article redirect limit exceeded")


@dataclass
class _Node:
    tag: str
    attrs: dict[str, str]
    children: list = field(default_factory=list)


_VOID = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"})
_SKIP_TAGS = frozenset({"script", "style", "nav", "footer", "aside", "header", "form", "button", "svg", "canvas", "iframe", "noscript", "template"})
_BLOCK_TAGS = frozenset({"p", "div", "section", "article", "main", "blockquote", "li", "br", "table", "tr"})
_BODY_IDENTIFIERS = frozenset({
    "content", "article-content", "article_content", "articlebody", "article-body",
    "article_body", "news-content", "news_content", "newsbody", "news-body",
    "story-body", "story-content", "rich_media_content", "entry-content", "post-content",
    "articletext", "artibody", "trs_editor", "ue_context",
})
_EXCLUDED_PART = re.compile(r"(?:^|[-_\s])(?:related|recommend(?:ed|ation|ations)?|sidebar|share|advert(?:isement)?|menu|footer|header|nav|comments?|copyright|breadcrumb|byline|author|pubtime)(?:$|[-_\s])", re.I)
_RELATED_HEADING = re.compile(r"^(?:related(?:\s+(?:news|articles?|stories))?|recommended|you may also like|read more|相关新闻|相关阅读|相关推荐|延伸阅读|热门推荐|相关文章)", re.I)


class _ArticleHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Node("document", {})
        self.stack = [self.root]
        self.nodes = 0

    def handle_starttag(self, tag, attrs):
        self.nodes += 1
        if self.nodes > 25_000 or len(self.stack) > 200:
            raise ArticleContentError("Article HTML is too complex")
        node = _Node(tag.lower(), {key.lower(): value or "" for key, value in attrs})
        self.stack[-1].children.append(node)
        if tag.lower() not in _VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag.lower() not in _VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag.lower():
                del self.stack[index:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def _walk(node):
    yield node
    for child in node.children:
        if isinstance(child, _Node):
            yield from _walk(child)


def _body_nodes(node, *, excluded_ancestor=False):
    # Selecting every <article> from an unrestricted tree would reintroduce
    # articles nested in a sidebar, related panel, hidden element or footer.
    blocked = excluded_ancestor or _excluded(node)
    if not blocked:
        yield node
    for child in node.children:
        if isinstance(child, _Node):
            yield from _body_nodes(child, excluded_ancestor=blocked)


def _raw_text(node: _Node) -> str:
    return " ".join(child if isinstance(child, str) else _raw_text(child) for child in node.children)


def _excluded(node: _Node) -> bool:
    identifiers = node.attrs.get("id", "") + " " + node.attrs.get("class", "")
    identifiers = re.sub(r"(?<=[a-z])(?=[A-Z])", "-", identifiers)
    return (node.tag in _SKIP_TAGS or bool(_EXCLUDED_PART.search(identifiers))
            or node.attrs.get("aria-hidden", "").lower() == "true" or "hidden" in node.attrs
            or node.attrs.get("role", "").lower() in {"navigation", "complementary", "banner"})


def _visible_text(node: _Node) -> str:
    if _excluded(node) or re.fullmatch(r"h[1-6]", node.tag):
        return ""
    parts = []
    for child in node.children:
        if isinstance(child, str):
            parts.append(child)
        else:
            if re.fullmatch(r"h[1-6]", child.tag) and _RELATED_HEADING.search(_raw_text(child).strip()):
                break
            text = _visible_text(child)
            parts.append(("\n" + text + "\n") if child.tag in _BLOCK_TAGS else text)
    return "".join(parts)


def _clean_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", unescape(text))
    text = "".join(char for char in text if unicodedata.category(char) != "Cf" and (ord(char) >= 32 or char in "\n\t"))
    return "\n".join(line for line in (re.sub(r"[ \t\r\f\v]+", " ", line).strip() for line in text.split("\n")) if line)


_EXACT_BRANCH = re.compile(
    r"(?:中国|中國)?工商(?:银行|銀行)[ \t（(]*金[边邊][ \t)）]*分行|工行[ \t]*金[边邊]分行"
    r"|(?<![a-z0-9])ICBC(?:[ \t]+(?:Limited|Ltd\.?))?[ \t,，—–-]+Phnom[ \t-]+Penh[ \t,，—–-]+Branch(?![a-z])"
    r"|Industrial[ \t]+and[ \t]+Commercial[ \t]+Bank[ \t]+of[ \t]+China(?:[ \t]+(?:Limited|Ltd\.?))?[, \t—–-]+Phnom[ \t-]+Penh[, \t—–-]+Branch"
    r"|អាយ[ \t]*ស៊ី[ \t]*ប៊ី[ \t]*ស៊ី[^\n។.!?]{0,30}សាខាភ្នំពេញ",
    re.I,
)
_NAMED_BANK = re.compile(r"(?<![a-z0-9])ICBC(?![a-z0-9])|Industrial\s+(?:and|&)\s+Commercial\s+Bank\s+of\s+China|(?:中国|中國)?工商(?:银行|銀行)|工行|អាយ\s*ស៊ី\s*ប៊ី\s*ស៊ី", re.I)
_PHNOM_PENH = re.compile(r"(?<![a-z])Phnom[ \t-]*Penh(?![a-z])|金边|金邊|ភ្នំពេញ", re.I)
_CANADIAN = re.compile(r"insurance corporation of british columbia|british columbia|autoplan|car insurance|vehicle insurance", re.I)


def _matching_excerpt(text: str) -> str:
    for paragraph in text.split("\n"):
        exact = _EXACT_BRANCH.search(paragraph)
        if exact:
            return paragraph[max(0, exact.start() - 140):exact.end() + 400][:650]
        for sentence in re.split(r"[。！？!?;；]|(?<=[.])\s+(?=[A-Z])", paragraph):
            if _CANADIAN.search(sentence):
                continue
            for bank in _NAMED_BANK.finditer(sentence):
                window_start, window_end = max(0, bank.start() - 160), min(len(sentence), bank.end() + 160)
                if _PHNOM_PENH.search(sentence[window_start:window_end]):
                    return sentence[max(0, bank.start() - 140):bank.end() + 400][:650]
    return ""


def _jsonld_articles(value):
    if isinstance(value, list):
        for item in value:
            yield from _jsonld_articles(item)
    elif isinstance(value, dict):
        kinds = value.get("@type", [])
        kinds = [kinds] if isinstance(kinds, str) else kinds
        if isinstance(kinds, list) and any(isinstance(kind, str) and (kind.rsplit("/", 1)[-1].endswith("Article") or kind in {"BlogPosting", "LiveBlogPosting"}) for kind in kinds):
            body = value.get("articleBody")
            if isinstance(body, str):
                yield value
        # hasPart and relatedLink frequently describe recommendations rather
        # than this page's main article and are intentionally not traversed.
        for key in ("@graph", "mainEntity", "mainEntityOfPage"):
            if key in value:
                yield from _jsonld_articles(value[key])


def _jsonld_bodies(value, resolved_url):
    articles = list(_jsonld_articles(value))
    for article in articles:
        identity = article.get("url") or article.get("mainEntityOfPage") or article.get("@id")
        if isinstance(identity, dict):
            identity = identity.get("@id") or identity.get("url")
        if isinstance(identity, str) and resolved_url:
            try:
                identity_url = urlsplit(urljoin(resolved_url, identity))
                current_url = urlsplit(resolved_url)
                if (identity_url.scheme.lower(), identity_url.netloc.lower(), identity_url.path.rstrip("/"), identity_url.query) != (current_url.scheme.lower(), current_url.netloc.lower(), current_url.path.rstrip("/"), current_url.query):
                    continue
            except ValueError:
                continue
        elif len(articles) > 1:
            # Multiple anonymous article bodies cannot safely identify the
            # current article rather than a structured-data recommendation.
            continue
        parser = _ArticleHTML()
        parser.feed(article["articleBody"])
        yield _clean_text(_visible_text(parser.root))


def _decode_html(data: bytes | str, content_type: str = "") -> str:
    if isinstance(data, str):
        if len(data.encode("utf-8")) > MAX_ARTICLE_BYTES:
            raise ArticleContentError("Article exceeds the maximum response size")
        return data
    if not isinstance(data, bytes) or len(data) > MAX_ARTICLE_BYTES:
        raise ArticleContentError("Article is unavailable or exceeds the maximum response size")
    declared = re.search(r"charset\s*=\s*[\"']?([a-zA-Z0-9_-]+)", content_type)
    if declared is None:
        declared = re.search(r"charset\s*=\s*[\"']?([a-zA-Z0-9_-]+)", data[:4096].decode("ascii", "ignore"))
    encoding = declared[1].lower() if declared else "utf-8-sig"
    allowed = {"utf-8", "utf8", "utf-8-sig", "gb18030", "gbk", "gb2312", "big5", "windows-1252", "iso-8859-1"}
    if encoding not in allowed:
        raise ArticleContentError("Article uses an unsupported text encoding")
    try:
        return data.decode(encoding, errors="strict")
    except (UnicodeError, LookupError):
        raise ArticleContentError("Article text could not be decoded") from None


def extract_article_html(html: bytes | str, *, resolved_url: str = "", content_type: str = "") -> ContentEvidence:
    """Pure extraction for local fixtures; it performs no network operations."""
    try:
        if resolved_url and urlsplit(resolved_url).hostname in _SEARCH_HOSTS:
            return ContentEvidence(False, False, resolved_url=resolved_url, error="Search landing page is not a publisher article")
        parser = _ArticleHTML()
        parser.feed(_decode_html(html, content_type))
        nodes = list(_walk(parser.root))
        title = " ".join(_raw_text(node) for node in nodes if node.tag == "title")
        if re.search(r"安全验证|百度验证码|验证码验证|^\s*(?:just a moment|access denied|sign in|log in)\b", title, re.I):
            raise ArticleContentError("Article returned authentication or security verification")
        candidates = []
        eligible_nodes = {id(node) for node in _body_nodes(parser.root)}
        eligible_scripts = {id(child) for parent in nodes if id(parent) in eligible_nodes
                            for child in parent.children if isinstance(child, _Node) and child.tag == "script"}
        for node in nodes:
            # A structured-data script is eligible if its *parent* is visible;
            # scripts themselves remain excluded from ordinary article text.
            if node.tag == "script" and node.attrs.get("type", "").split(";", 1)[0].strip().lower() == "application/ld+json" and id(node) in eligible_scripts:
                try:
                    candidates.extend(_jsonld_bodies(json.loads(_raw_text(node)), resolved_url))
                except (ValueError, TypeError, RecursionError):
                    pass
            identifiers = {part.casefold() for part in (node.attrs.get("id", "") + " " + node.attrs.get("class", "")).split()}
            itemprop = node.attrs.get("itemprop", "").split()
            if id(node) in eligible_nodes and (node.tag in {"article", "main"} or identifiers.intersection(_BODY_IDENTIFIERS) or "articleBody" in itemprop):
                candidates.append(_clean_text(_visible_text(node)))
        readable = [text for text in dict.fromkeys(candidates) if sum(char.isalnum() for char in text) >= MIN_BODY_CHARACTERS]
        if not readable:
            return ContentEvidence(False, False, resolved_url=resolved_url, error="No readable main article body was found")
        for text in readable:
            excerpt = _matching_excerpt(text)
            if excerpt:
                return ContentEvidence(True, True, excerpt, resolved_url)
        longest = max(readable, key=len)
        return ContentEvidence(True, False, longest[:650], resolved_url)
    except (ArticleContentError, RecursionError) as exc:
        error = str(exc) if isinstance(exc, ArticleContentError) else "Article HTML is too complex"
        return ContentEvidence(False, False, resolved_url=resolved_url, error=error)


def inspect_article(url: str, *, fetcher=None) -> ContentEvidence:
    """Inspect a future supported public publisher, with no retries.

    A fixture ``fetcher(url)`` may return HTML bytes/string or ``(html, final_url)``
    (optionally a third content-type field). Injection skips DNS/network work but
    still checks both initial and returned URLs, including denied destinations.
    Production fetching never uses a proxy, credentials, private IP, or a second
    DNS lookup after validation.
    """
    resolved = ""
    try:
        resolved, _ = _validated_url(url)
        result = (fetcher or _fetch_article)(resolved)
        content_type = ""
        if isinstance(result, tuple) and len(result) in {2, 3}:
            html, destination = result[:2]
            resolved, _ = _validated_url(destination)
            if len(result) == 3:
                content_type = result[2]
        elif isinstance(result, (bytes, str)):
            html = result
        else:
            raise ArticleContentError("Article fetch did not return a supported document")
        return extract_article_html(html, resolved_url=resolved, content_type=content_type)
    except ArticleContentError as exc:
        return ContentEvidence(False, False, resolved_url=resolved, error=str(exc))
    except (OSError, http.client.HTTPException, ValueError, TypeError, UnicodeError, OverflowError):
        return ContentEvidence(False, False, resolved_url=resolved, error="Article request failed or timed out")
