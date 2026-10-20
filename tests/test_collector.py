"""RSS fixtures verify the reporting-day boundary and false-positive defenses."""

from datetime import date, datetime, timezone
from html import escape
import unittest
from urllib.parse import parse_qs, urlsplit

from news_monitor.collector import (
    Article,
    FeedQuery,
    MAX_FEED_BYTES,
    _validate_feed_url,
    build_queries,
    collect,
    deduplicate_articles,
    is_relevant,
    parse_google_news_rss,
)


DAY = date(2026, 10, 19)
GOOGLE = FeedQuery("fixture-google", "https://news.google.com/rss/search?q=ICBC", "google")
BING = FeedQuery("fixture-bing", "https://www.bing.com/news/search?q=ICBC&format=rss", "bing")


def item(title, published="Mon, 19 Oct 2026 03:00:00 GMT", url="https://example.com/a", source="Khmer Times", description=""):
    return f"<item><title>{escape(title)}</title><link>{escape(url)}</link><pubDate>{escape(published)}</pubDate><source>{escape(source)}</source><description>{escape(description)}</description></item>"


def rss(*items):
    return ("<?xml version='1.0' encoding='utf-8'?><rss version='2.0'><channel><title>Fixture</title>" + "".join(items) + "</channel></rss>").encode()


class CollectorTests(unittest.TestCase):
    def test_previous_day_is_based_on_phnom_penh_publication_timestamp(self):
        data = rss(
            item("ICBC Cambodia midnight", "Sun, 18 Oct 2026 17:00:00 GMT", "https://example.com/first"),
            item("ICBC Phnom Penh final minute", "Mon, 19 Oct 2026 16:59:59 GMT", "https://example.com/last"),
            item("ICBC Cambodia too early", "Sun, 18 Oct 2026 16:59:59 GMT", "https://example.com/early"),
            item("ICBC Cambodia next day", "Mon, 19 Oct 2026 17:00:00 GMT", "https://example.com/next"),
            item("ICBC Cambodia old news newly indexed", "Fri, 01 May 2026 00:00:00 GMT", "https://example.com/old"),
        )
        articles, status = parse_google_news_rss(data, GOOGLE, DAY)
        self.assertEqual(len(articles), 2)
        self.assertEqual(articles[0].published_at, "2026-10-19T00:00:00+07:00")
        self.assertEqual(articles[1].published_at, "2026-10-19T23:59:59+07:00")
        self.assertEqual((status.raw_count, status.relevant_count, status.date_count), (5, 5, 2))
        self.assertTrue(status.ok)

    def test_exact_publisher_suffix_only(self):
        articles, _ = parse_google_news_rss(rss(
            item("ICBC Cambodia signs agreement - Khmer Times"),
            item("ICBC Phnom Penh - development cooperation - new programme", url="https://example.com/b"),
        ), GOOGLE, DAY)
        self.assertEqual(articles[0].title, "ICBC Cambodia signs agreement")
        self.assertEqual(articles[1].title, "ICBC Phnom Penh - development cooperation - new programme")

    def test_google_rollup_does_not_create_false_relevance_or_summary(self):
        rollup = '<a href="https://example.com/a">ICBC Cambodia cooperation</a><a href="https://example.com/b">Other news</a>'
        articles, status = parse_google_news_rss(rss(
            item("ICBC London reports results", description=rollup),
            item("ICBC Phnom Penh launches service", url="https://example.com/branch", description=rollup),
        ), GOOGLE, DAY)
        self.assertEqual(status.relevant_count, 1)
        self.assertEqual(len(articles), 1)
        self.assertEqual(articles[0].summary, "")

    def test_bing_actual_snippet_can_establish_branch_connection(self):
        articles, status = parse_google_news_rss(rss(item(
            "Industrial and Commercial Bank of China joins event",
            description="Its Phnom Penh Branch took part in the Cambodia forum.",
            url="https://www.bing.com/news/apiclick.aspx?ref=FexRss&url=https%3A%2F%2Fexample.com%2Fstory",
        )), BING, DAY)
        self.assertTrue(status.ok)
        self.assertEqual(articles[0].url, "https://example.com/story")
        self.assertIn("Phnom Penh Branch", articles[0].summary)

    def test_multilingual_relevance_and_other_markets(self):
        for title in (
            "ICBC Phnom Penh supports local businesses",
            "I.C.B.C. Cambodia supports local businesses",
            "中国工商银行金边分行签署合作协议",
            "工行柬埔寨业务取得进展",
            "Industrial and Commercial Bank of China Limited, Phnom Penh Branch",
            "ធនាគារ អាយស៊ីប៊ីស៊ី លីមីធីត សាខាភ្នំពេញ",
            "ធនាគារ អាយ\u200bស៊ី\u200bប៊ី\u200bស៊ី សាខាភ្នំពេញ",
        ):
            with self.subTest(title=title):
                self.assertTrue(is_relevant(title))
        for title in (
            "ICBC Singapore reports results",
            "Cambodia announces new rail project",
            "ICBC Canada drivers travelling to Cambodia seek insurance",
            "Cambodia trade rises - PICBCOM unrelated brand",
            "Bank of China Phnom Penh opens a new branch",
            "ICBC British Columbia Cambodia car insurance claims",
        ):
            with self.subTest(title=title):
                self.assertFalse(is_relevant(title))

    def test_missing_or_naive_publication_date_is_visible_failure(self):
        articles, status = parse_google_news_rss(rss(
            item("ICBC Cambodia event", published=""),
            item("ICBC Phnom Penh event", published="2026-10-19T08:00:00", url="https://example.com/b"),
        ), GOOGLE, DAY)
        self.assertEqual(articles, [])
        self.assertFalse(status.ok)
        self.assertEqual(status.invalid_date_count, 2)
        self.assertIn("publication timestamp", status.error)

    def test_duplicate_headlines_and_tracking_urls_collapse(self):
        def article(title, url, hour="03"):
            return Article(title, url, "News", f"2026-10-19T{hour}:00:00+07:00")
        result = deduplicate_articles([
            article("ICBC Cambodia signs cooperation agreement", "https://example.com/a?utm_source=x"),
            article("ICBC Cambodia signs cooperation agreement!", "https://other.example/b"),
            article("Rewritten title about ICBC Phnom Penh", "https://example.com/a?utm_source=y", "04"),
            article("ICBC Cambodia opens a new business centre", "https://example.com/c"),
        ])
        self.assertEqual(len(result), 2)

    def test_similar_but_distinct_numeric_stories_remain_separate(self):
        result = deduplicate_articles([
            Article("ICBC Phnom Penh supports Cambodia trade with 100 new corporate loans", "https://example.com/a", "News", "2026-10-19T10:00:00+07:00"),
            Article("ICBC Phnom Penh supports Cambodia trade with 200 new corporate loans", "https://example.com/b", "News", "2026-10-19T10:01:00+07:00"),
        ])
        self.assertEqual(len(result), 2)

    def test_near_identical_headlines_with_changed_counterparty_remain_separate(self):
        result = deduplicate_articles([
            Article("ICBC Phnom Penh signs a cooperation agreement with Cambodia Bank A", "https://example.com/a", "News", "2026-10-19T10:00:00+07:00"),
            Article("ICBC Phnom Penh signs a cooperation agreement with Cambodia Bank B", "https://example.com/b", "News", "2026-10-19T10:01:00+07:00"),
        ])
        self.assertEqual(len(result), 2)

    def test_missing_link_for_on_date_article_is_visible_failure(self):
        articles, status = parse_google_news_rss(rss(item("ICBC Cambodia signs agreement", url="javascript:alert(1)")), GOOGLE, DAY)
        self.assertFalse(status.ok)
        self.assertEqual(status.invalid_link_count, 1)
        self.assertEqual(articles, [])

    def test_search_window_is_padded_and_multilingual(self):
        queries = build_queries(DAY)
        self.assertGreaterEqual(len(queries), 6)
        for query in queries:
            _validate_feed_url(query.url)
            if query.provider == "google":
                term = parse_qs(urlsplit(query.url).query)["q"][0]
                self.assertIn("after:2026-10-18", term)
                self.assertIn("before:2026-10-21", term)
        self.assertTrue(any("zh-CN" in query.url for query in queries))

    def test_valid_empty_feed_is_different_from_failed_source(self):
        fixtures = {
            "google": rss(),
            "bing": b"<html>temporary block</html>",
        }
        result = collect(DAY, now=datetime(2026, 10, 20, tzinfo=timezone.utc), fetcher=lambda url: fixtures["google" if "google.com" in url else "bing"])
        self.assertFalse(result.complete)
        self.assertEqual(result.articles, ())
        self.assertEqual(result.diagnostics["successful_queries"], 5)
        self.assertEqual(result.diagnostics["failed_queries"], 3)
        self.assertTrue(all(status.ok for status in result.statuses[:5]))
        self.assertIn("not an RSS", result.statuses[-1].error)

    def test_all_empty_success_is_complete(self):
        result = collect(DAY, now=datetime(2026, 10, 20, tzinfo=timezone.utc), fetcher=lambda url: rss())
        self.assertTrue(result.complete)
        self.assertEqual(result.articles, ())
        self.assertEqual(result.diagnostics["raw_items"], 0)

    def test_collection_deduplicates_overlapping_queries(self):
        result = collect(DAY, now=datetime(2026, 10, 20, tzinfo=timezone.utc), fetcher=lambda url: rss(item("ICBC Cambodia signs agreement")))
        self.assertTrue(result.complete)
        self.assertEqual(len(result.articles), 1)
        self.assertEqual(result.diagnostics["on_date_items_before_dedup"], 8)

    def test_today_and_naive_clock_cannot_generate_a_complete_digest(self):
        with self.assertRaises(ValueError):
            collect(DAY, now=datetime(2026, 10, 19, 12, tzinfo=timezone.utc), fetcher=lambda url: rss())
        with self.assertRaises(ValueError):
            collect(DAY, now=datetime(2026, 10, 20), fetcher=lambda url: rss())

    def test_only_known_https_feed_sources_can_be_fetched(self):
        for url in (
            "http://news.google.com/rss/search?q=ICBC",
            "https://evil.example/rss/search?q=ICBC",
            "https://news.google.com:444/rss/search?q=ICBC",
            "https://news.google.com/other",
            "https://user:secret@news.google.com/rss/search?q=ICBC",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                _validate_feed_url(url)

    def test_reject_oversized_and_entity_feeds(self):
        with self.assertRaises(ValueError):
            parse_google_news_rss(b"x" * (MAX_FEED_BYTES + 1), GOOGLE, DAY)
        with self.assertRaises(ValueError):
            parse_google_news_rss(b'<!DOCTYPE rss [<!ENTITY x "hello">]><rss><channel/></rss>', GOOGLE, DAY)


if __name__ == "__main__":
    unittest.main()
