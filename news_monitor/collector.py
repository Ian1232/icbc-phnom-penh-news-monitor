"""Bounded, read-only news search with publication-date and relevance checks.

Search feeds are indexes, not an exhaustive record of everything published online.
The Google search window is deliberately wider than the reporting date; the final
decision always uses the item's actual publication timestamp in Phnom Penh time.
No article destination is fetched, and no external translation service is used.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
import re
import socket
import threading
import time
import unicodedata
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import xml.etree.ElementTree as ET

PHNOM_PENH_TZ = timezone(timedelta(hours=7), "Asia/Phnom_Penh")
MAX_FEED_BYTES = 2 * 1024 * 1024
MAX_WORKERS = 6
FETCH_TIMEOUT_SECONDS = 25
FETCH_ATTEMPTS = 3
ALLOWED_FEED_HOSTS = frozenset({"news.google.com", "www.bing.com", "www.baidu.com"})
USER_AGENT = "ICBCPhnomPenhNewsMonitor/1.0 (public RSS; daily digest)"
BAIDU_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"


@dataclass(frozen=True)
class Article:
    title: str
    url: str
    source: str
    published_at: str
    summary: str = ""
    query: str = ""
    publication_note: str = ""


@dataclass(frozen=True)
class QueryStatus:
    name: str
    url: str
    ok: bool
    raw_count: int = 0
    relevant_count: int = 0
    date_count: int = 0
    error: str = ""
    invalid_date_count: int = 0
    invalid_link_count: int = 0


@dataclass(frozen=True)
class CollectionResult:
    target_date: str
    articles: tuple[Article, ...]
    statuses: tuple[QueryStatus, ...]

    @property
    def complete(self) -> bool:
        return bool(self.statuses) and all(status.ok for status in self.statuses)

    @property
    def diagnostics(self) -> dict:
        return {
            "target_date": self.target_date,
            "query_count": len(self.statuses),
            "successful_queries": sum(status.ok for status in self.statuses),
            "failed_queries": sum(not status.ok for status in self.statuses),
            "raw_items": sum(status.raw_count for status in self.statuses),
            "relevant_items": sum(status.relevant_count for status in self.statuses),
            "on_date_items_before_dedup": sum(status.date_count for status in self.statuses),
            "invalid_date_items": sum(status.invalid_date_count for status in self.statuses),
            "invalid_link_items": sum(status.invalid_link_count for status in self.statuses),
            "articles": len(self.articles),
        }


@dataclass(frozen=True)
class FeedQuery:
    name: str
    url: str
    provider: str


class _PlainText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.links = 0
        self.hidden = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in {"script", "style"}:
            self.hidden += 1
        if tag == "a":
            self.links += 1
        if tag in {"br", "p", "div", "li"}:
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)
        if tag in {"p", "div", "li", "a"}:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)


def _plain_text(value: str) -> tuple[str, int]:
    parser = _PlainText()
    parser.feed(value)
    return re.sub(r"\s+", " ", unescape("".join(parser.parts))).strip(), parser.links


def _normalize(value: str) -> str:
    value = unicodedata.normalize("NFKC", unescape(value)).casefold()
    return "".join(char for char in value if unicodedata.category(char)[0] in "LN")


_BANK_NAME = re.compile(
    r"industrial\s+(?:and|&)\s+commercial\s+bank\s+(?:of\s+)?china"
    r"|(?:中国|中國)?工商(?:银行|銀行)|工行|អាយ\s*ស៊ី\s*ប៊ី\s*ស៊ី",
    re.IGNORECASE,
)
_ICBC = re.compile(r"(?<![A-Za-z0-9])i\.?\s*c\.?\s*b\.?\s*c\.?(?![A-Za-z0-9])", re.I)
_REGION = re.compile(
    r"(?<![A-Za-z])(?:phnom[\s-]*penh|cambodia(?:n)?|kampuchea)(?![A-Za-z])"
    r"|金边|金邊|柬埔寨|柬国|柬國|ភ្នំពេញ|កម្ពុជា",
    re.IGNORECASE,
)
_CANADIAN_INSURANCE = re.compile(
    r"insurance corporation of british columbia|british columbia|autoplan"
    r"|(?:driver|motorist|car insurance|vehicle insurance|road safety)",
    re.IGNORECASE,
)


def is_relevant(title: str, summary: str = "") -> bool:
    """Require both the named bank and its Cambodia/Phnom Penh connection."""
    text = "".join(char for char in unicodedata.normalize("NFKC", f"{title} {summary}") if unicodedata.category(char) != "Cf")
    official_name = bool(_BANK_NAME.search(text))
    if not (official_name or _ICBC.search(text)) or not _REGION.search(text):
        return False
    if not official_name and _CANADIAN_INSURANCE.search(text):
        return False
    return True


def _validate_feed_url(url: str) -> None:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in ALLOWED_FEED_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in {None, 443}
        or (parsed.hostname == "news.google.com" and parsed.path != "/rss/search")
        or (parsed.hostname == "www.bing.com" and parsed.path != "/news/search")
        or (parsed.hostname == "www.baidu.com" and
            (parsed.path != "/s" or dict(parse_qsl(parsed.query)).get("tn") != "news"))
    ):
        raise ValueError("Feed URL is outside the approved HTTPS news searches")


class _FeedRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlsplit(newurl).hostname in {"wappass.baidu.com", "passport.baidu.com"}:
            raise ValueError("Baidu returned a security verification page")
        _validate_feed_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_feed(url: str) -> bytes:
    """Fetch approved RSS/HTML news searches, retaining normal TLS verification."""
    _validate_feed_url(url)
    opener = build_opener(_FeedRedirects())
    for attempt in range(FETCH_ATTEMPTS):
        try:
            baidu = urlsplit(url).hostname == "www.baidu.com"
            headers = {"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/xml;q=0.9"}
            if baidu:
                headers = {"User-Agent": BAIDU_USER_AGENT,
                           "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                           "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7"}
            request = Request(url, headers=headers)
            with opener.open(request, timeout=FETCH_TIMEOUT_SECONDS) as response:
                _validate_feed_url(response.geturl())
                data = response.read(MAX_FEED_BYTES + 1)
                if len(data) > MAX_FEED_BYTES:
                    raise ValueError("Feed exceeds the maximum response size")
                return data
        except HTTPError as exc:
            retry = exc.code in {408, 429, 500, 502, 503, 504}
            if not retry or attempt + 1 == FETCH_ATTEMPTS:
                raise
        except (URLError, TimeoutError, socket.timeout, ConnectionError):
            if attempt + 1 == FETCH_ATTEMPTS:
                raise
        time.sleep(0.75 * (2 ** attempt))
    raise RuntimeError("Feed fetch exhausted retries")


def build_queries(target_date: date) -> tuple[FeedQuery, ...]:
    """Use exact and broad multilingual queries, then filter the returned items."""
    # RSS search date operators may use a different time zone from the reader.
    bounds = f"after:{target_date - timedelta(days=1):%Y-%m-%d} before:{target_date + timedelta(days=2):%Y-%m-%d}"
    specs = (
        ("google-en-broad", 'ICBC Cambodia', "en-US", "US", "US:en"),
        ("google-en-branch", '"ICBC" "Phnom Penh"', "en-US", "US", "US:en"),
        ("google-en-full-name", '"Industrial and Commercial Bank of China" (Cambodia OR "Phnom Penh")', "en-US", "US", "US:en"),
        ("google-zh-branch", '(工商银行 OR 工行) (金边 OR 柬埔寨)', "zh-CN", "CN", "CN:zh-Hans"),
        # Khmer keywords use a supported English Google News edition.
        ("google-km-branch", 'ICBC (ភ្នំពេញ OR កម្ពុជា)', "en-US", "US", "US:en"),
    )
    queries = [
        FeedQuery(name, "https://news.google.com/rss/search?" + urlencode({"q": f"{term} {bounds}", "hl": hl, "gl": gl, "ceid": ceid}), "google")
        for name, term, hl, gl, ceid in specs
    ]
    # Bing's index provides an independent path. Date filtering still uses pubDate;
    # its undocumented search-date syntax is deliberately not relied upon.
    for name, term in (
        ("bing-en-broad", '"ICBC" Cambodia'),
        ("bing-en-branch", '"ICBC" "Phnom Penh"'),
        ("bing-zh-branch", '工商银行 金边'),
    ):
        queries.append(FeedQuery(name, "https://www.bing.com/news/search?" + urlencode({"q": term, "format": "rss", "setlang": "en-US"}), "bing"))
    # Baidu's public news results expose publication labels in the result cards.
    # Never substitute search/crawl time for the article's publication time.
    for name, term in (
        ("baidu-zh-branch", '工商银行 金边'),
        ("baidu-zh-cambodia", '工行 柬埔寨'),
        ("baidu-zh-full-name", '"工商银行金边分行"'),
        ("baidu-en-branch", '"ICBC" "Phnom Penh"'),
    ):
        queries.append(FeedQuery(name, "https://www.baidu.com/s?" + urlencode({
            "tn": "news", "rtt": "4", "bsst": "1", "cl": "2", "ie": "utf-8", "wd": term,
        }), "baidu"))
    return tuple(queries)


def _parse_publication(value: str) -> datetime | None:
    if not value.strip():
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (ValueError, TypeError, IndexError, OverflowError):
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except (ValueError, TypeError, OverflowError):
            return None
    # A timestamp with no time-zone cannot safely establish a reporting day.
    if parsed is None or parsed.tzinfo is None:
        return None
    return parsed.astimezone(PHNOM_PENH_TZ)


def _article_url(value: str) -> str:
    value = unescape(value).strip()
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            return ""
        # Bing wraps the real article URL in its public click URL. Unwrap only
        # the explicit URL parameter; do not fetch the wrapper or destination.
        if parsed.hostname in {"www.bing.com", "bing.com"} and parsed.path.endswith("/apiclick.aspx"):
            destination = dict(parse_qsl(parsed.query)).get("url", "")
            return _article_url(destination) if destination and destination != value else value
        return value
    except ValueError:
        return ""


def _source_text(item: ET.Element) -> str:
    for child in item:
        if child.tag.rsplit("}", 1)[-1].lower() == "source":
            return _plain_text("".join(child.itertext()))[0]
    return ""


def _strip_publisher(title: str, source: str) -> str:
    # Remove only a source name explicitly identified by this RSS item.
    if source:
        for separator in (" - ", " – ", " — "):
            suffix = separator + source
            if title.casefold().endswith(suffix.casefold()):
                return title[:-len(suffix)].strip()
    return title


def parse_google_news_rss(data: bytes, query: FeedQuery, target_date: date) -> tuple[list[Article], QueryStatus]:
    """Parse a Google or Bing RSS response without visiting article URLs."""
    if len(data) > MAX_FEED_BYTES:
        raise ValueError("Feed exceeds the maximum response size")
    if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        raise ValueError("Feed contains unsupported XML declarations")
    root = ET.fromstring(data)
    if root.tag.rsplit("}", 1)[-1].lower() != "rss" or root.find("channel") is None:
        raise ValueError("Response is not an RSS feed")
    items = root.findall("./channel/item")
    articles: list[Article] = []
    relevant_count = 0
    invalid_date_count = 0
    invalid_link_count = 0
    for item in items:
        source = _source_text(item)
        title = _strip_publisher(_plain_text(item.findtext("title", ""))[0], source)
        if not title:
            continue
        description, description_links = _plain_text(item.findtext("description", ""))
        # Google descriptions are headline/publisher link-rollups, not article
        # prose. Keeping them would both fabricate summaries and pollute matching.
        summary = description if query.provider == "bing" and description_links <= 1 else ""
        if _normalize(summary) in {_normalize(title), _normalize(title + source)}:
            summary = ""
        if not is_relevant(title, summary):
            continue
        relevant_count += 1
        published = _parse_publication(item.findtext("pubDate", "") or item.findtext("{http://purl.org/dc/elements/1.1/}date", ""))
        if published is None:
            invalid_date_count += 1
            continue
        if published.date() != target_date:
            continue
        url = _article_url(item.findtext("link", ""))
        if not url:
            invalid_link_count += 1
            continue
        articles.append(Article(title, url, source or urlsplit(url).hostname or "Unknown source", published.isoformat(), summary[:1600], query.name))
    errors = []
    if invalid_date_count:
        errors.append(f"{invalid_date_count} relevant item(s) have no usable publication timestamp")
    if invalid_link_count:
        errors.append(f"{invalid_link_count} on-date item(s) have no usable article URL")
    status = QueryStatus(query.name, query.url, not errors, len(items), relevant_count, len(articles), "; ".join(errors), invalid_date_count, invalid_link_count)
    return articles, status


def _url_key(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.hostname == "news.google.com" and "/articles/" in parsed.path:
        query = ""
    else:
        query = urlencode([(key, value) for key, value in parse_qsl(parsed.query) if not key.casefold().startswith("utm_") and key.casefold() not in {"fbclid", "gclid"}])
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip("/"), query, ""))


def deduplicate_articles(articles: list[Article]) -> tuple[Article, ...]:
    """Collapse indexed copies, while preserving materially different headlines."""
    kept: list[Article] = []
    urls: set[str] = set()
    titles: set[str] = set()
    for article in sorted(articles, key=lambda a: (a.published_at, a.title, a.url)):
        url_key, title_key = _url_key(article.url), _normalize(article.title)
        if url_key in urls or title_key in titles:
            continue
        near_duplicate = False
        for previous in kept:
            previous_key = _normalize(previous.title)
            if min(len(title_key), len(previous_key)) < 50:
                continue
            if re.findall(r"\d+", title_key) != re.findall(r"\d+", previous_key):
                continue
            if min(len(title_key), len(previous_key)) / max(len(title_key), len(previous_key)) < 0.92:
                continue
            # Permit only harmless English article-word differences. General
            # fuzzy similarity can conflate "Bank A" and "Bank B", or opposing
            # claims with only one changed word, even at very high thresholds.
            tokens = lambda text: [word for word in re.findall(r"\w+", unicodedata.normalize("NFKC", text).casefold()) if word not in {"a", "an", "the"}]
            if tokens(article.title) == tokens(previous.title):
                near_duplicate = True
                break
        urls.add(url_key)
        titles.add(title_key)
        if not near_duplicate:
            kept.append(article)
    return tuple(kept)


def _error_text(exc: Exception) -> str:
    # Do not echo HTTP response content, query strings, or environment values.
    if isinstance(exc, HTTPError):
        return f"HTTP {exc.code}"
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return "Feed request timed out"
    if isinstance(exc, URLError):
        return "Feed connection failed"
    if isinstance(exc, ET.ParseError):
        return "Feed returned invalid XML"
    if isinstance(exc, ValueError):
        return str(exc)[:180]
    return f"Feed collection failed ({type(exc).__name__})"


def collect(target_date: date, *, now: datetime | None = None, fetcher: Callable[[str], bytes] | None = None) -> CollectionResult:
    """Collect the requested completed Phnom Penh calendar day.

    ``fetcher`` exists for deterministic fixture testing and must return RSS bytes.
    Successful zero-result feeds remain distinguishable from failed feeds.
    """
    if isinstance(target_date, datetime) or not isinstance(target_date, date):
        raise TypeError("target_date must be a datetime.date")
    current = now or datetime.now(PHNOM_PENH_TZ)
    if current.tzinfo is None:
        raise ValueError("now must include a time zone")
    if target_date >= current.astimezone(PHNOM_PENH_TZ).date():
        raise ValueError("Only completed Phnom Penh calendar days can be collected")
    queries = build_queries(target_date)
    fetch = fetcher or fetch_feed
    baidu_gate = threading.BoundedSemaphore(1)
    baidu_verification_required = False

    def run(query: FeedQuery) -> tuple[list[Article], QueryStatus]:
        nonlocal baidu_verification_required
        try:
            if query.provider == "baidu":
                from .baidu import parse_baidu_news
                # Keep the four daily Baidu requests sequential and low volume.
                with baidu_gate:
                    if baidu_verification_required:
                        raise ValueError("Baidu security verification required; remaining queries skipped")
                    try:
                        return parse_baidu_news(fetch(query.url), query, target_date, observed_at=current)
                    except ValueError as exc:
                        if "security verification" in str(exc):
                            baidu_verification_required = True
                        raise
            return parse_google_news_rss(fetch(query.url), query, target_date)
        except Exception as exc:
            return [], QueryStatus(query.name, query.url, False, error=_error_text(exc))

    articles: list[Article] = []
    statuses: dict[str, QueryStatus] = {}
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(queries))) as executor:
        jobs = {executor.submit(run, query): query.name for query in queries}
        for future in as_completed(jobs):
            found, status = future.result()
            articles.extend(found)
            statuses[status.name] = status
    return CollectionResult(target_date.isoformat(), deduplicate_articles(articles), tuple(statuses[query.name] for query in queries))
