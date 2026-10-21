"""Offline regressions for headlines that omit the branch name.

The sample title is previously observed public metadata. Its RSS timestamp,
search responses and article paragraph below are synthetic test fixtures; these
tests make no request to the supplied Baijiahao URL or any other website.
"""

from datetime import date, datetime, timezone
from html import escape
import json
import unittest
from unittest.mock import Mock
from urllib.parse import urlsplit

from news_monitor.__main__ import render
from news_monitor.article_content import ContentEvidence, extract_article_html
from news_monitor.baidu import parse_baidu_news
from news_monitor.collector import (
    Article,
    CollectionResult,
    FeedQuery,
    QueryStatus,
    build_queries,
    collect,
    deduplicate_articles,
    is_relevant,
    parse_google_news_rss,
    verify_body_candidates,
)


DAY = date(2026, 9, 21)
NOW = datetime(2026, 10, 21, tzinfo=timezone.utc)
SAMPLE_TITLE = "柬埔寨川渝总商会700余人共庆中国国庆节和中秋节 共话中柬友谊、共谋合作发展"
SAMPLE_URL = "https://baijiahao.baidu.com/s?id=1877016373831758570&wfr=spider&for=pc"
FIXTURE_TIMESTAMP = "Mon, 21 Sep 2026 03:00:00 GMT"
FIXTURE_PARAGRAPH = "本段为离线回归测试构造的活动报道正文。出席代表包括中国工商银行金边分行。"
EXACT = FeedQuery("fixture-exact-branch", "https://news.google.com/rss/search?q=fixture", "google", exact_branch=True)
BROAD = FeedQuery("fixture-broad", "https://news.google.com/rss/search?q=fixture", "google")
BAIDU_EMPTY = '<html><head><title>百度资讯搜索</title></head><body><div id="noresult">抱歉，没有找到相关结果</div></body></html>'.encode()


def rss_item(title=SAMPLE_TITLE, published=FIXTURE_TIMESTAMP, url=SAMPLE_URL, description=""):
    return (f"<item><title>{escape(title)}</title><link>{escape(url)}</link>"
            f"<pubDate>{escape(published)}</pubDate><source>Fixture Publisher</source>"
            f"<description>{escape(description)}</description></item>")


def rss(*items):
    return ("<rss version='2.0'><channel><title>Offline fixture</title>" + "".join(items) + "</channel></rss>").encode()


def candidate(title=SAMPLE_TITLE, url=SAMPLE_URL, hour="10"):
    return Article(title, url, "Fixture Publisher", f"2026-09-21T{hour}:00:00+07:00",
                   query=EXACT.name, relevance="candidate", evidence="exact_branch_query",
                   review_reason="精确分行名称检索命中，正文尚未核验")


def exact_feed_fetcher(data):
    exact_urls = {query.url for query in build_queries(DAY)
                  if query.provider == "google" and query.exact_branch}

    def fetch(url):
        if urlsplit(url).hostname == "www.baidu.com":
            return BAIDU_EMPTY
        return data if url in exact_urls else rss()

    return fetch


class BodyGapTests(unittest.TestCase):
    def test_sample_headline_without_bank_is_preserved_only_as_exact_query_candidate(self):
        self.assertFalse(is_relevant(SAMPLE_TITLE))
        articles, status = parse_google_news_rss(rss(rss_item()), EXACT, DAY)
        self.assertTrue(status.ok)
        self.assertEqual(len(articles), 1)
        self.assertEqual(articles[0].url, SAMPLE_URL)
        self.assertEqual(articles[0].relevance, "candidate")
        self.assertEqual(articles[0].evidence, "exact_branch_query")
        self.assertEqual(articles[0].published_at, "2026-09-21T10:00:00+07:00")

    def test_broad_query_does_not_turn_generic_cambodia_news_into_candidate(self):
        articles, status = parse_google_news_rss(rss(rss_item()), BROAD, DAY)
        self.assertTrue(status.ok)
        self.assertEqual(articles, [])

    def test_baidu_exact_query_also_preserves_title_only_gap_candidate(self):
        query = FeedQuery("baidu-fixture-exact", "https://www.baidu.com/s?tn=news&wd=fixture", "baidu", exact_branch=True)
        fields = {"title": SAMPLE_TITLE, "titleUrl": SAMPLE_URL, "summary": "活动报道的测试摘要。",
                  "sourceName": "Fixture Publisher", "dispTime": "2026-09-21 11:00"}
        data = ('<html><title>百度资讯搜索</title><div class="result" tpl="news-normal" id="1">'
                '<!--s-data:' + json.dumps(fields, ensure_ascii=False) + '--></div></html>').encode()
        articles, status = parse_baidu_news(data, query, DAY, observed_at=NOW)
        self.assertTrue(status.ok)
        self.assertEqual(len(articles), 1)
        self.assertEqual(articles[0].relevance, "candidate")
        self.assertEqual(articles[0].evidence, "exact_branch_query")
        self.assertEqual(articles[0].url, SAMPLE_URL)

    def test_baidu_broad_query_drops_same_unconfirmed_event(self):
        query = FeedQuery("baidu-fixture-broad", "https://www.baidu.com/s?tn=news&wd=fixture", "baidu")
        fields = {"title": SAMPLE_TITLE, "titleUrl": SAMPLE_URL, "summary": "活动报道的测试摘要。",
                  "sourceName": "Fixture Publisher", "dispTime": "2026-09-21 11:00"}
        data = ('<html><title>百度资讯搜索</title><div class="result" tpl="news-normal" id="1">'
                '<!--s-data:' + json.dumps(fields, ensure_ascii=False) + '--></div></html>').encode()
        articles, status = parse_baidu_news(data, query, DAY, observed_at=NOW)
        self.assertTrue(status.ok)
        self.assertEqual(articles, [])

    def test_google_headline_rollup_is_not_article_body_evidence(self):
        rollup = '<a href="https://example.com/other">中国工商银行金边分行另一篇报道</a>'
        articles, _ = parse_google_news_rss(rss(rss_item(description=rollup)), EXACT, DAY)
        self.assertEqual(articles[0].relevance, "candidate")
        self.assertEqual(articles[0].summary, "")

    def test_fixture_body_confirms_sample_headline_and_keeps_excerpt(self):
        evidence = extract_article_html(f"<article><h1>{SAMPLE_TITLE}</h1><p>{FIXTURE_PARAGRAPH}</p></article>")
        self.assertTrue(evidence.verified)
        self.assertTrue(evidence.matched)
        inspector = Mock(return_value=evidence)
        articles, checks = verify_body_candidates([candidate()], inspector=inspector)
        self.assertEqual(len(articles), 1)
        self.assertEqual(articles[0].relevance, "confirmed")
        self.assertEqual(articles[0].evidence, "article_body")
        self.assertIn("中国工商银行金边分行", articles[0].summary)
        self.assertEqual(articles[0].review_reason, "")
        self.assertEqual(checks[0]["outcome"], "confirmed")
        inspector.assert_called_once_with(SAMPLE_URL)

    def test_exact_query_without_body_match_does_not_confirm_article(self):
        evidence = extract_article_html("<article><p>本段为离线测试构造的无关活动正文，介绍文化活动安排、参加人数和当天的节庆节目。</p></article>")
        self.assertTrue(evidence.verified)
        self.assertFalse(evidence.matched)
        articles, checks = verify_body_candidates([candidate()], inspector=lambda url: evidence)
        self.assertEqual(articles, ())
        self.assertEqual(checks[0]["outcome"], "unrelated")

    def test_navigation_and_related_stories_do_not_establish_branch_relevance(self):
        html = ("<article><p>本段为离线测试构造的无关活动正文，介绍文化活动安排、参加人数和当天的节庆节目。</p>"
                "<nav>中国工商银行金边分行</nav><aside>中国工商银行金边分行</aside>"
                "<div class='related'>中国工商银行金边分行</div></article>")
        evidence = extract_article_html(html)
        self.assertTrue(evidence.verified)
        self.assertFalse(evidence.matched)

    def test_blocked_body_stays_visible_without_becoming_confirmed(self):
        evidence = ContentEvidence(False, False, error="Article host is unavailable or requires authentication")
        articles, checks = verify_body_candidates([candidate()], inspector=lambda url: evidence)
        self.assertEqual(articles[0].relevance, "candidate")
        self.assertEqual(checks[0]["outcome"], "unverified")
        self.assertIn("unavailable", checks[0]["error"])
        self.assertIn("关联性待确认", articles[0].review_reason)

    def test_body_timeout_stays_visible_without_leaking_exception(self):
        inspector = Mock(side_effect=TimeoutError("private diagnostic detail"))
        articles, checks = verify_body_candidates([candidate()], inspector=inspector)
        self.assertEqual(articles[0].relevance, "candidate")
        self.assertEqual(checks[0]["outcome"], "unverified")
        self.assertNotIn("private diagnostic detail", str(checks))

    def test_body_budget_preserves_unchecked_candidate_with_explicit_reason(self):
        inspector = Mock(return_value=ContentEvidence(False, False, error="Timed out"))
        articles, checks = verify_body_candidates([
            candidate(), candidate(title="另一个活动报道", url="https://example.com/second"),
        ], inspector=inspector, limit=1)
        self.assertEqual(len(articles), 2)
        inspector.assert_called_once()
        self.assertEqual([check["outcome"] for check in checks], ["unverified", "unverified"])
        self.assertIn("额度", next(article.review_reason for article in articles if article.url.endswith("second")))

    def test_metadata_confirmed_item_does_not_require_body_fetch(self):
        confirmed = Article("ICBC Phnom Penh joins event", "https://example.com/confirmed", "Fixture Publisher", "2026-09-21T10:00:00+07:00")
        inspector = Mock(side_effect=AssertionError("Should not fetch already-confirmed metadata"))
        articles, checks = verify_body_candidates([confirmed], inspector=inspector)
        self.assertEqual(articles, (confirmed,))
        self.assertEqual(checks, ())
        inspector.assert_not_called()

    def test_confirmed_duplicate_wins_over_earlier_unverified_copy(self):
        earlier = candidate(url="https://example.com/story?utm_source=feed", hour="09")
        confirmed = Article("中国工商银行金边分行出席活动", "https://example.com/story?utm_source=other", "Fixture Publisher", "2026-09-21T11:00:00+07:00")
        articles = deduplicate_articles([earlier, confirmed])
        self.assertEqual(articles, (confirmed,))

    def test_collection_deduplicates_exact_query_copies_before_body_inspection(self):
        inspector = Mock(return_value=ContentEvidence(True, True, FIXTURE_PARAGRAPH))
        result = collect(DAY, now=NOW, fetcher=exact_feed_fetcher(rss(rss_item())), body_inspector=inspector)
        self.assertTrue(result.complete)
        self.assertEqual(len(result.articles), 1)
        self.assertEqual(result.diagnostics["confirmed_articles"], 1)
        self.assertEqual(result.diagnostics["candidate_articles"], 0)
        self.assertEqual(result.diagnostics["body_confirmed_count"], 1)
        self.assertGreater(result.diagnostics["on_date_items_before_dedup"], 1)
        inspector.assert_called_once_with(SAMPLE_URL)

    def test_historical_sample_never_enters_a_different_reporting_day(self):
        wrong_day = date(2026, 10, 20)
        exact_urls = {query.url for query in build_queries(wrong_day)
                      if query.provider == "google" and query.exact_branch}

        def fetch(url):
            if urlsplit(url).hostname == "www.baidu.com":
                return BAIDU_EMPTY
            return rss(rss_item()) if url in exact_urls else rss()

        inspector = Mock(side_effect=AssertionError("Wrong-day articles must not be fetched"))
        result = collect(wrong_day, now=NOW, fetcher=fetch, body_inspector=inspector)
        self.assertTrue(result.complete)
        self.assertEqual(result.articles, ())
        inspector.assert_not_called()

    def test_exact_candidate_with_missing_date_is_failure_and_never_fetched(self):
        inspector = Mock(side_effect=AssertionError("Undated articles must not be fetched"))
        result = collect(DAY, now=NOW, fetcher=exact_feed_fetcher(rss(rss_item(published=""))), body_inspector=inspector)
        self.assertFalse(result.complete)
        self.assertEqual(result.articles, ())
        self.assertGreater(result.diagnostics["invalid_date_items"], 0)
        inspector.assert_not_called()

    def test_unverified_body_is_distinct_from_search_source_failure(self):
        inspector = Mock(return_value=ContentEvidence(False, False, error="No readable main article body was found"))
        result = collect(DAY, now=NOW, fetcher=exact_feed_fetcher(rss(rss_item())), body_inspector=inspector)
        self.assertTrue(result.complete)
        self.assertEqual(result.diagnostics["failed_queries"], 0)
        self.assertEqual(result.diagnostics["confirmed_articles"], 0)
        self.assertEqual(result.diagnostics["candidate_articles"], 1)
        self.assertEqual(result.diagnostics["body_unverified_count"], 1)

    def test_render_separates_confirmed_and_unverified_counts_and_evidence(self):
        confirmed = Article("正文核实的活动报道", "https://example.com/confirmed", "Fixture Publisher", "2026-09-21T11:00:00+07:00",
                            summary=FIXTURE_PARAGRAPH, evidence="article_body")
        result = CollectionResult(DAY.isoformat(), (confirmed, candidate()), (QueryStatus("google-fixture", EXACT.url, True),))
        text = render(result, test=True)
        self.assertIn("已确认关联：1 条；关联待核实：1 条", text)
        self.assertIn("【已确认关联】", text)
        self.assertIn("正文关联摘录：", text)
        self.assertIn("【关联待核实】", text)
        self.assertIn("不计入已确认新闻", text)
        self.assertIn(SAMPLE_URL, text)

    def test_candidate_only_report_does_not_claim_no_news(self):
        result = CollectionResult(DAY.isoformat(), (candidate(),), (QueryStatus("google-fixture", EXACT.url, True),))
        text = render(result)
        self.assertIn("已确认关联：0 条；关联待核实：1 条", text)
        self.assertNotIn("本次检索未发现", text)
        self.assertNotIn("【已确认关联】", text)


if __name__ == "__main__":
    unittest.main()
