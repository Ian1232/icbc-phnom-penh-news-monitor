"""Baidu's real card schema, precise day boundaries and blocked-page fixtures."""

from datetime import date, datetime, timezone, timedelta
import json
import unittest

from news_monitor.baidu import parse_baidu_news
from news_monitor.collector import FeedQuery


DAY = date(2026, 10, 19)
OBSERVED = datetime(2026, 10, 20, 8, tzinfo=timezone(timedelta(hours=7)))
QUERY = FeedQuery("baidu-zh-branch", "https://www.baidu.com/s?tn=news&wd=ICBC", "baidu")
BAIDU_EMPTY = '<html><head><title>百度资讯搜索</title></head><body><div id="noresult">抱歉，没有找到相关结果</div></body></html>'.encode("utf-8")


def card(title="中国工商银行金边分行举行主题论坛", published="2026年10月19日 12:30", url="https://example.com/a", summary="中国工商银行金边分行在柬埔寨首都举行主题论坛。", source="中国新闻网", identifier=1):
    # This s-data schema was observed on the public Baidu News result HTML.
    data = {"title": title, "titleUrl": url, "summary": summary, "dispTime": published, "sourceName": source, "dataClick": "{'t':'1792487872'}"}
    return f'<div class="result-op c-container xpath-log new-pmd" id="{identifier}" tpl="news-normal"><div><!--s-data:{json.dumps(data, ensure_ascii=False)}--><h3 class="news-title_1YtI1"><a href="{url}">{title}</a></h3></div></div>'


def page(*cards):
    return ('<html><head><title>百度资讯搜索_工商银行 金边</title></head><body><div id="content_left">' + "".join(cards) + '</div></body></html>').encode("utf-8")


class BaiduTests(unittest.TestCase):
    def parse(self, data):
        return parse_baidu_news(data, QUERY, DAY, observed_at=OBSERVED)

    def test_real_recorded_metadata_schema_strips_highlight_and_preserves_snippet(self):
        articles, status = self.parse(page(card(title="<em>中国工商银行金边</em>分行举行主题论坛")))
        self.assertTrue(status.ok)
        self.assertEqual(len(articles), 1)
        self.assertEqual(articles[0].title, "中国工商银行金边分行举行主题论坛")
        self.assertEqual(articles[0].source, "中国新闻网")
        self.assertEqual(articles[0].published_at, "2026-10-19T11:30:00+07:00")
        self.assertEqual(articles[0].summary, "中国工商银行金边分行在柬埔寨首都举行主题论坛。")
        self.assertEqual(articles[0].publication_note, "")

    def test_china_publication_timestamps_convert_to_reporting_day(self):
        articles, status = self.parse(page(
            card(published="2026年10月19日 01:00", url="https://example.com/start"),
            card(published="2026年10月19日 00:59", url="https://example.com/early", identifier=2),
            card(published="2026年10月20日 00:59:59", url="https://example.com/end", identifier=3),
            card(published="2026年10月20日 01:00", url="https://example.com/next", identifier=4),
        ))
        self.assertTrue(status.ok)
        self.assertEqual([article.url for article in articles], ["https://example.com/start", "https://example.com/end"])
        self.assertEqual(status.raw_count, 4)

    def test_old_date_only_item_does_not_degrade_current_digest(self):
        articles, status = self.parse(page(card(published="2023年12月16日")))
        self.assertEqual(articles, [])
        self.assertTrue(status.ok)
        self.assertEqual(status.relevant_count, 1)

    def test_date_only_item_overlapping_target_is_explicitly_uncertain(self):
        for label in ("2026年10月19日", "2026年10月20日", "昨天", "今天", "1天前"):
            with self.subTest(label=label):
                articles, status = self.parse(page(card(published=label)))
                self.assertEqual(articles, [])
                self.assertFalse(status.ok)
                self.assertEqual(status.invalid_date_count, 1)

    def test_relative_hour_bucket_fully_within_target_uses_raw_note(self):
        articles, status = self.parse(page(card(published="12小时前")))
        self.assertTrue(status.ok)
        self.assertEqual(len(articles), 1)
        self.assertIn("12小时前", articles[0].publication_note)
        self.assertIn("相对时间", articles[0].publication_note)

    def test_relative_hour_bucket_straddling_midnight_is_uncertain(self):
        articles, status = self.parse(page(card(published="8小时前")))
        self.assertFalse(status.ok)
        self.assertEqual(articles, [])
        self.assertEqual(status.invalid_date_count, 1)

    def test_real_result_date_only_historical_fixture(self):
        data = page(card(
            title="<em>中国工商银行金边</em>分行举办主题论坛 分析亚太宏观经济发展态势",
            published="2023年12月16日",
            url="https://baijiahao.baidu.com/s?id=1785445113971483761&wfr=spider&for=pc",
            summary="中新网金边12月16日电 当地时间12月15日，中国工商银行金边分行在柬埔寨首都金边举办主题论坛。",
        ))
        articles, status = self.parse(data)
        self.assertTrue(status.ok)
        self.assertEqual((status.raw_count, status.relevant_count, status.date_count), (1, 1, 0))
        self.assertEqual(articles, [])

    def test_relevance_uses_each_card_own_title_and_snippet(self):
        articles, status = self.parse(page(
            card(title="中国工商银行发布集团业绩", summary="工商银行发布集团经营业绩。", identifier=1),
            card(title="柬埔寨推出农业计划", summary="其他银行参与计划。", identifier=2, url="https://example.com/b"),
            card(title="区域金融论坛召开", summary="中国工商银行金边分行出席该论坛。", identifier=3, url="https://example.com/c"),
        ))
        self.assertTrue(status.ok)
        self.assertEqual(status.relevant_count, 1)
        self.assertEqual([article.url for article in articles], ["https://example.com/c"])

    def test_dom_fallback_reads_aria_labels_within_card(self):
        data = page('<div class="result-op c-container" tpl="news-normal" id="1"><h3><a href="https://example.com/dom">工商银行金边分行举行活动</a></h3><span aria-label="发布于：2026年10月19日 16:20">日期</span><span aria-label="新闻来源：中国新闻网">来源</span><span aria-label="摘要 中国工商银行金边分行举行活动。 摘要结束，点击查看详情">正文摘要</span></div>')
        articles, status = self.parse(data)
        self.assertTrue(status.ok)
        self.assertEqual(articles[0].source, "中国新闻网")
        self.assertEqual(articles[0].summary, "中国工商银行金边分行举行活动。")
        self.assertEqual(articles[0].published_at, "2026-10-19T15:20:00+07:00")

    def test_explicit_empty_layout_is_healthy(self):
        articles, status = self.parse(BAIDU_EMPTY)
        self.assertEqual(articles, [])
        self.assertTrue(status.ok)

    def test_security_verification_http200_is_failure(self):
        for content in (
            '<html><title>百度安全验证</title><script src="captcha.js"></script></html>',
            '<html><title>百度验证码</title>请完成验证</html>',
        ):
            with self.subTest(content=content), self.assertRaisesRegex(ValueError, "verification"):
                self.parse(content.encode("utf-8"))

    def test_unknown_or_generic_search_page_is_failure(self):
        for content in (
            b'<html><title>Unexpected service page</title></html>',
            '<html><title>百度一下</title><div id="noresult">没有找到相关结果</div></html>'.encode("utf-8"),
            b'<rss><channel/></rss>',
        ):
            with self.subTest(content=content), self.assertRaisesRegex(ValueError, "unrecognized"):
                self.parse(content)

    def test_broken_result_card_is_not_reported_as_empty(self):
        articles, status = self.parse(page('<div class="result-op c-container" tpl="news-normal" id="1">broken card</div>'))
        self.assertEqual(articles, [])
        self.assertFalse(status.ok)
        self.assertIn("could not be parsed", status.error)

    def test_relevant_unknown_date_does_not_silently_disappear(self):
        articles, status = self.parse(page(card(published="更新时间：2026年10月19日 12:30")))
        self.assertEqual(articles, [])
        self.assertFalse(status.ok)
        self.assertEqual(status.invalid_date_count, 1)

    def test_malformed_article_url_is_visible_failure(self):
        articles, status = self.parse(page(card(url="javascript:alert(1)")))
        self.assertFalse(status.ok)
        self.assertEqual(articles, [])
        self.assertEqual(status.invalid_link_count, 1)

    def test_request_timestamp_is_not_a_publication_date(self):
        articles, status = self.parse(page(card(published="")))
        self.assertEqual(articles, [])
        self.assertFalse(status.ok)


if __name__ == "__main__":
    unittest.main()
