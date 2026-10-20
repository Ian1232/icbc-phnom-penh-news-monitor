"""Conservative publication-time parsing for Chinese Baidu news result labels.

Baidu's visible labels do not always contain an actual publication timestamp.
In particular, a China calendar date spans 23:00 on the preceding Phnom Penh
date through 23:00 on that date. Date-only labels cannot establish the requested
Phnom Penh reporting day and intentionally return ``None``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html import unescape
import re
import unicodedata


CHINA_TZ = timezone(timedelta(hours=8), "Asia/Shanghai")

_CLOCK = r"(?P<hour>\d{1,2}):(?P<minute>\d{2})(?::(?P<second>\d{2}))?(?![\d:.])"
_EXPLICIT_DATE = re.compile(
    r"(?<!\d)(?P<year>\d{4})\s*(?:年|[-/])\s*"
    r"(?P<month>\d{1,2})\s*(?:月|[-/])\s*(?P<day>\d{1,2})(?!\d)\s*日?"
)
_INFERRED_DATE = re.compile(
    r"(?<![\d/-])(?P<month>\d{1,2})\s*(?:月|-)\s*(?P<day>\d{1,2})(?!\d)\s*日?"
)
_DATE_CLOCK = re.compile(r"\s*(?:T|[,，])?\s*" + _CLOCK)
_RELATIVE_DAY = re.compile(r"(?P<day>今天|昨天|前天)\s*" + _CLOCK)
_RELATIVE_AGE = re.compile(r"(?<![\d.\-])(?P<amount>\d+)\s*(?P<unit>分钟|小时|天)前(?!\d)")
_NON_PUBLICATION = re.compile(
    r"百度快照|网页快照|缓存|快照时间|收录时间|索引时间|更新时间|更新于|最后更新|爬取时间|抓取时间"
)


def _clock_values(match: re.Match) -> tuple[int, int, int]:
    return int(match["hour"]), int(match["minute"]), int(match["second"] or 0)


def _has_conflicting_timezone(text: str) -> bool:
    # A visible Baidu China-time label can be parsed, but an ISO/foreign-time
    # suffix must not silently be reinterpreted as UTC+8.
    tail = text.lstrip()
    if re.match(r"(?:Z\b|UTC\b|GMT\b|CST\b)", tail, re.I):
        return True
    zone = re.match(r"([+-])(\d{2}):?(\d{2})(?!\d)", tail)
    return bool(zone and (zone[1], zone[2], zone[3]) != ("+", "08", "00"))


def _timestamp(year: int, month: int, day: int, clock: re.Match, text: str) -> datetime | None:
    if _has_conflicting_timezone(text[clock.end():]):
        return None
    try:
        return datetime(year, month, day, *_clock_values(clock), tzinfo=CHINA_TZ)
    except (ValueError, OverflowError):
        return None


def parse_baidu_datetime(text: str, *, observed_at: datetime) -> datetime | None:
    """Return a China-time publication timestamp, or ``None`` if ambiguous.

    ``observed_at`` must be timezone-aware and is converted to China time before
    interpreting labels such as ``昨天23:30`` or an omitted calendar year.
    Yearless month/day labels resolve to the most recent matching calendar date;
    a same-day future clock is retained as that day, rather than inferred to be
    from last year. Coarse relative ages use the observation timestamp minus the
    stated duration; callers should not present them as publisher-exact times.

    Only date labels accompanied by HH:MM (optionally seconds) establish a time.
    Date-only, cache/index/update labels, invalid dates and unknown text return
    ``None``. ``ValueError`` rejects an observation without a UTC offset.
    """
    if not isinstance(observed_at, datetime):
        raise TypeError("observed_at must be a datetime")
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("observed_at must include a time zone")
    if not isinstance(text, str):
        return None
    observed = observed_at.astimezone(CHINA_TZ)
    value = unicodedata.normalize("NFKC", unescape(text)).strip()
    value = "".join(char for char in value if unicodedata.category(char) != "Cf")
    if not value or _NON_PUBLICATION.search(value):
        return None

    explicit = _EXPLICIT_DATE.search(value)
    if explicit:
        clock = _DATE_CLOCK.match(value, explicit.end())
        if clock is None:
            return None
        return _timestamp(int(explicit["year"]), int(explicit["month"]), int(explicit["day"]), clock, value)

    inferred = _INFERRED_DATE.search(value)
    if inferred:
        clock = _DATE_CLOCK.match(value, inferred.end())
        if clock is None:
            return None
        month, day = int(inferred["month"]), int(inferred["day"])
        # Include leap-day labels across a non-leap current year. Eight years
        # covers the longest Gregorian leap-year gap around century boundaries.
        for year in range(observed.year, max(0, observed.year - 9), -1):
            candidate = _timestamp(year, month, day, clock, value)
            if candidate is not None and candidate.date() <= observed.date():
                return candidate
        return None

    relative_day = _RELATIVE_DAY.search(value)
    if relative_day:
        day = observed.date() - timedelta(days={"今天": 0, "昨天": 1, "前天": 2}[relative_day["day"]])
        return _timestamp(day.year, day.month, day.day, relative_day, value)
    if any(word in value for word in ("今天", "昨天", "前天")):
        return None

    relative_age = _RELATIVE_AGE.search(value)
    if relative_age:
        unit = relative_age["unit"]
        try:
            amount = int(relative_age["amount"])
            elapsed = timedelta(**{{"分钟": "minutes", "小时": "hours", "天": "days"}[unit]: amount})
            return observed - elapsed
        except (OverflowError, ValueError):
            return None
    if re.search(r"(?<!\w)刚刚(?!\w)", value) or value == "刚刚":
        return observed
    return None


@dataclass(frozen=True)
class BaiduDateBounds:
    """Possible publication interval; ``latest`` is exclusive.

    A coarse valid label has bounds, no exact ``published_at``, and an error
    explaining its precision. Callers can ignore intervals outside the reporting
    day, accept intervals wholly within it, and expose overlapping ones instead
    of asserting that the article was or was not published on that day.
    """

    earliest: datetime | None = None
    latest: datetime | None = None
    published_at: datetime | None = None
    error: str = ""


def _calendar_bounds(year: int, month: int, day: int) -> BaiduDateBounds:
    try:
        earliest = datetime(year, month, day, tzinfo=CHINA_TZ)
        return BaiduDateBounds(earliest, earliest + timedelta(days=1), None,
                               "Baidu publication date has no precise time")
    except (ValueError, OverflowError):
        return BaiduDateBounds(error="Invalid Baidu publication date")


def baidu_datetime_bounds(text: str, *, observed_at: datetime) -> BaiduDateBounds:
    """Return conservative intervals for exact, date-only and relative labels.

    This precision-aware companion should drive reporting-day decisions. A
    China date without a time always straddles two Phnom Penh calendar dates.
    Relative labels such as ``N小时前`` are treated as elapsed-duration buckets;
    ``N天前`` additionally includes its possible China calendar-day meaning.
    """
    parsed = parse_baidu_datetime(text, observed_at=observed_at)
    observed = observed_at.astimezone(CHINA_TZ)
    if not isinstance(text, str):
        return BaiduDateBounds(error="Missing Baidu publication time")
    value = unicodedata.normalize("NFKC", unescape(text)).strip()
    value = "".join(char for char in value if unicodedata.category(char) != "Cf")
    if not value or _NON_PUBLICATION.search(value):
        return BaiduDateBounds(error="Missing publication time or non-publication label")

    # Relative age strings describe a range, even though the simple helper can
    # return a useful approximate timestamp for non-filtering callers.
    age = _RELATIVE_AGE.search(value)
    if age and parsed is not None:
        try:
            unit = {"分钟": "minutes", "小时": "hours", "天": "days"}[age["unit"]]
            earliest = observed - timedelta(**{unit: int(age["amount"]) + 1})
            latest = parsed
            if age["unit"] == "天":
                calendar = _calendar_bounds(parsed.year, parsed.month, parsed.day)
                if calendar.earliest is not None:
                    earliest = min(earliest, calendar.earliest)
                    latest = max(latest, calendar.latest)
            # Include the exact lower elapsed-age boundary in this exclusive
            # interval representation, including N=0 at the observation instant.
            return BaiduDateBounds(earliest, latest + timedelta(microseconds=1), None,
                                   "Baidu relative publication age has coarse precision")
        except (ValueError, OverflowError):
            return BaiduDateBounds(error="Invalid Baidu relative publication age")
    if parsed is not None:
        if value == "刚刚" or re.search(r"(?<!\w)刚刚(?!\w)", value):
            return BaiduDateBounds(parsed - timedelta(minutes=1), parsed + timedelta(microseconds=1), None,
                                   "Baidu just-now publication label has coarse precision")
        return BaiduDateBounds(parsed, parsed + timedelta(microseconds=1), parsed)

    explicit = _EXPLICIT_DATE.search(value)
    if explicit:
        if _DATE_CLOCK.match(value, explicit.end()):
            return BaiduDateBounds(error="Invalid Baidu publication timestamp")
        return _calendar_bounds(int(explicit["year"]), int(explicit["month"]), int(explicit["day"]))
    inferred = _INFERRED_DATE.search(value)
    if inferred:
        if _DATE_CLOCK.match(value, inferred.end()):
            return BaiduDateBounds(error="Invalid Baidu publication timestamp")
        for year in range(observed.year, max(0, observed.year - 9), -1):
            candidate = _calendar_bounds(year, int(inferred["month"]), int(inferred["day"]))
            if candidate.earliest is not None and candidate.earliest.date() <= observed.date():
                return candidate
        return BaiduDateBounds(error="Invalid Baidu publication date")
    for label, days in (("今天", 0), ("昨天", 1), ("前天", 2)):
        if value == label:
            day = observed.date() - timedelta(days=days)
            return _calendar_bounds(day.year, day.month, day.day)
    return BaiduDateBounds(error="Unrecognized or ambiguous Baidu publication time")
