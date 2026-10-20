"""Parse public Baidu News search result cards without executing page scripts.

Baidu may return a verification page with HTTP 200. An unrecognized page is a
source failure, never evidence that no news exists. Dates displayed by Baidu are
evaluated in their source time zone before the Phnom Penh reporting-day check.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from html.parser import HTMLParser
import json
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .collector import Article, FeedQuery, QueryStatus


@dataclass
class _Node:
    tag: str
    attrs: dict[str, str]
    children: list = field(default_factory=list)

    def text(self) -> str:
        return " ".join(child.text() if isinstance(child, _Node) else child for child in self.children)

    def walk(self):
        yield self
        for child in self.children:
            if isinstance(child, _Node):
                yield from child.walk()


@dataclass
class _Card:
    root: _Node
    metadata: list[dict] = field(default_factory=list)


_VOID = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"})


class _BaiduCards(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.cards: list[_Card] = []
        self.active: _Card | None = None
        self.stack: list[_Node] = []
        self.in_title = False
        self.page_title = ""
        self.empty_depth = 0
        self.empty_text: list[str] = []

    def handle_starttag(self, tag, attrs):
        attributes = {key: value or "" for key, value in attrs}
        if tag == "title":
            self.in_title = True
        if attributes.get("id", "").casefold() in {"noresult", "no-result", "no_result"}:
            self.empty_depth = 1
        elif self.empty_depth and tag not in _VOID:
            self.empty_depth += 1
        classes = set(attributes.get("class", "").split())
        template = attributes.get("tpl", "")
        is_card = (
            tag == "div"
            and bool(classes.intersection({"result", "result-op"}))
            and (template in {"news-normal", "news"} or (not template and attributes.get("id", "").isdigit()))
        )
        if is_card and self.active is None:
            node = _Node(tag, attributes)
            self.active = _Card(node)
            self.stack = [node]
        elif self.active is not None:
            node = _Node(tag, attributes)
            self.stack[-1].children.append(node)
            if tag not in _VOID:
                self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False
        if self.empty_depth:
            self.empty_depth -= 1
        if not self.active:
            return
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                if not self.stack:
                    self.cards.append(self.active)
                    self.active = None
                break

    def handle_data(self, text):
        if self.in_title:
            self.page_title += text
        if self.empty_depth:
            self.empty_text.append(text)
        if self.active and self.stack[-1].tag not in {"script", "style"}:
            self.stack[-1].children.append(text)

    def handle_comment(self, text):
        if self.active is None or not text.startswith("s-data:"):
            return
        try:
            data = json.loads(text[7:])
        except (ValueError, TypeError):
            return
        if isinstance(data, dict) and "title" in data and "titleUrl" in data:
            self.active.metadata.append(data)

    def close(self):
        super().close()
        if self.active:
            self.cards.append(self.active)
            self.active = None
            self.stack = []


def _fields(card: _Card, plain_text) -> dict[str, str]:
    nodes = list(card.root.walk())
    if card.metadata:
        data = card.metadata[0]
        return {
            "title": plain_text(str(data.get("title", "")))[0],
            "url": str(data.get("titleUrl", "")),
            "summary": plain_text(str(data.get("summary", "")))[0],
            "source": plain_text(str(data.get("sourceName", "")))[0],
            "date": plain_text(str(data.get("dispTime", "")))[0],
        }
    fields = {"title": "", "url": "", "summary": "", "source": "", "date": ""}
    headings = [node for node in nodes if node.tag in {"h3", "h2"}]
    for heading in headings:
        anchors = [node for node in heading.walk() if node.tag == "a" and node.attrs.get("href")]
        if anchors:
            fields["title"] = re.sub(r"\s+", " ", anchors[0].text()).strip()
            fields["url"] = anchors[0].attrs["href"]
            break
    for node in nodes:
        aria = node.attrs.get("aria-label", "")
        if aria.startswith("发布于："):
            fields["date"] = aria.removeprefix("发布于：").strip()
        elif aria.startswith("新闻来源："):
            fields["source"] = aria.removeprefix("新闻来源：").strip()
        elif aria.startswith("摘要 "):
            fields["summary"] = aria.removeprefix("摘要 ").split(" 摘要结束", 1)[0].strip()
        classes = set(node.attrs.get("class", "").split())
        if not fields["summary"] and classes.intersection({"c-abstract", "c-summary"}):
            fields["summary"] = re.sub(r"\s+", " ", node.text()).strip()
        if not fields["date"] and "c-author" in classes:
            text = re.sub(r"\s+", " ", node.text()).strip()
            match = re.search(r"\d{4}[-年/]\d{1,2}[-月/]\d{1,2}日?(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?|\d+\s*(?:分钟|小时|天)前|(?:今天|昨天|前天)(?:\s*\d{1,2}:\d{2})?", text)
            if match:
                fields["date"] = match.group(0)
                fields["source"] = fields["source"] or text[:match.start()].strip()
    return fields


def parse_baidu_news(data: bytes, query: "FeedQuery", target_date: date, *, observed_at: datetime) -> tuple[list["Article"], "QueryStatus"]:
    """Return relevant, dated public news cards, or an explicit source failure."""
    # Local imports keep collector's provider dispatch free of import cycles.
    from .collector import Article, QueryStatus, MAX_FEED_BYTES, PHNOM_PENH_TZ, _article_url, _plain_text, is_relevant
    from .chinese_dates import baidu_datetime_bounds

    if observed_at.tzinfo is None:
        raise ValueError("Baidu observation time must include a time zone")
    if len(data) > MAX_FEED_BYTES:
        raise ValueError("Baidu response exceeds maximum response size")
    try:
        html = data.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError("Baidu returned an unsupported response encoding") from exc
    parser = _BaiduCards()
    parser.feed(html)
    parser.close()
    if "安全验证" in parser.page_title or "百度验证码" in parser.page_title:
        raise ValueError("Baidu returned a security verification page")
    if not parser.cards:
        empty_text = "".join(parser.empty_text)
        known_search = "百度" in parser.page_title and any(word in parser.page_title for word in ("资讯", "新闻", "搜索"))
        if known_search and any(word in empty_text for word in ("没有找到", "未找到", "暂无相关")):
            return [], QueryStatus(query.name, query.url, True)
        raise ValueError("Baidu returned an unrecognized search layout")

    lower = datetime.combine(target_date, time.min, tzinfo=PHNOM_PENH_TZ)
    upper = lower + timedelta(days=1)
    articles = []
    relevant_count = invalid_date_count = invalid_link_count = malformed_count = 0
    seen = set()
    for card in parser.cards:
        fields = _fields(card, _plain_text)
        if not fields["title"]:
            malformed_count += 1
            continue
        if not is_relevant(fields["title"], fields["summary"]):
            continue
        relevant_count += 1
        bounds = baidu_datetime_bounds(fields["date"], observed_at=observed_at)
        earliest, latest = bounds.earliest, bounds.latest
        if earliest is None or latest is None:
            invalid_date_count += 1
            continue
        if latest <= lower or earliest >= upper:
            continue
        if bounds.published_at is not None:
            published = bounds.published_at.astimezone(PHNOM_PENH_TZ)
            if published.date() != target_date:
                continue
            publication_note = ""
        else:
            if earliest < lower or latest > upper:
                invalid_date_count += 1
                continue
            # Bounds locate a reporting day without claiming an exact minute.
            # The presentation uses the raw Baidu label instead of this sort key.
            published = earliest.astimezone(PHNOM_PENH_TZ)
            publication_note = f"百度标注：{fields['date']}（相对时间；按发布时间区间筛选）"
        url = _article_url(fields["url"])
        if not url:
            invalid_link_count += 1
            continue
        if (fields["title"], url) in seen:
            continue
        seen.add((fields["title"], url))
        articles.append(Article(fields["title"], url, fields["source"] or "百度资讯检索", published.isoformat(), fields["summary"][:1600], query.name, publication_note))
    errors = []
    if malformed_count:
        errors.append(f"{malformed_count} Baidu result card(s) could not be parsed")
    if invalid_date_count:
        errors.append(f"{invalid_date_count} relevant Baidu item(s) have uncertain publication dates")
    if invalid_link_count:
        errors.append(f"{invalid_link_count} on-date Baidu item(s) have no usable article URL")
    status = QueryStatus(query.name, query.url, not errors, len(parser.cards), relevant_count, len(articles), "; ".join(errors), invalid_date_count, invalid_link_count)
    return articles, status
