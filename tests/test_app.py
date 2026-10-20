import argparse
import contextlib
from datetime import date, datetime, timezone
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from news_monitor import __main__ as app
from news_monitor.collector import Article, CollectionResult, QueryStatus


class AppTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.args = argparse.Namespace(test_message=False, force=False, dry_run=False,
                                       state=str(Path(self.temp.name) / "state.json"),
                                       output=str(Path(self.temp.name) / "reports"))
        self.day = date(2026, 10, 19)

    def result(self, health=(True, True), articles=None):
        return CollectionResult(self.day.isoformat(), tuple(articles or []),
                                tuple(QueryStatus(f"source-{i}", "https://news.google.com/rss/search", ok)
                                      for i, ok in enumerate(health)))

    def run_silently(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return app.run_day(self.day, self.args)

    def test_reporting_day_uses_phnom_penh_midnight(self):
        # 17:00 UTC is midnight of the next local day.
        self.assertEqual(app.reporting_date(datetime(2026, 10, 19, 16, 59, tzinfo=timezone.utc)), date(2026, 10, 18))
        self.assertEqual(app.reporting_date(datetime(2026, 10, 19, 17, 0, tzinfo=timezone.utc)), date(2026, 10, 19))

    def test_split_preserves_every_content_character_and_utf16_limit(self):
        text = "😀" * 3000 + "\n" + "链接 https://example.com/news\n" + "柬埔寨" * 1600
        pieces = app.split_messages(text)
        self.assertGreater(len(pieces), 2)
        self.assertTrue(all(len(x.encode("utf-16-le")) // 2 <= 3500 for x in pieces))
        # Trailing whitespace between chunks may be normalized, but no content lost.
        self.assertEqual("".join(pieces).replace("\n", ""), text.replace("\n", ""))

    def test_healthy_no_news_is_framed_as_search_result(self):
        rendered = app.render(self.result())
        self.assertIn("本次检索未发现", rendered)
        self.assertIn("未收录内容可能遗漏", rendered)
        self.assertNotIn("当天没有新闻", rendered)

    def test_partial_no_news_explicitly_does_not_assert_absence(self):
        rendered = app.render(self.result((True, False)))
        self.assertIn("部分新闻源采集失败", rendered)
        self.assertIn("不能据此认定当天没有新闻", rendered)

    def test_baidu_failure_is_named_in_daily_report(self):
        result = CollectionResult(self.day.isoformat(), (), (
            QueryStatus("google-en-broad", "https://news.google.com/rss/search", True),
            QueryStatus("baidu-zh-branch", "https://www.baidu.com/s?tn=news", False,
                        error="Baidu security verification required"),
        ))
        rendered = app.render(result)
        self.assertIn("检索渠道：Google News、Bing News、百度资讯", rendered)
        self.assertIn("未完成采集：百度资讯", rendered)
        self.assertIn("不能据此认定当天没有新闻", rendered)

    def test_baidu_relative_time_is_displayed_as_source_label(self):
        article = Article("工商银行金边分行签署协议", "https://example.com/a", "Example News",
                          "2026-10-19T10:00:00+07:00", publication_note="百度标注：6小时前（相对时间）")
        rendered = app.render(self.result(articles=[article]))
        self.assertIn("发布时间：百度标注：6小时前（相对时间）", rendered)
        self.assertNotIn("发布时间：10:00", rendered)

    def test_article_metadata_and_per_source_diagnostics_are_retained(self):
        article = Article("ICBC Cambodia signs agreement", "https://example.com/news/1", "Example News",
                          "2026-10-19T02:00:00+00:00", "A source-provided extract.")
        result = self.result(articles=[article])
        with patch.object(app, "collect", return_value=result), patch.object(app, "deliver", return_value={}):
            self.assertEqual(self.run_silently(), 0)
        rendered = (Path(self.args.output) / "2026-10-19.txt").read_text(encoding="utf-8")
        self.assertIn("发布时间：09:00", rendered)
        self.assertIn(article.title, rendered)
        import json
        diagnostics = json.loads((Path(self.args.output) / "2026-10-19.json").read_text(encoding="utf-8"))
        self.assertEqual(len(diagnostics["queries"]), 2)
        self.assertEqual(diagnostics["items"][0]["url"], article.url)

    def test_all_sources_fail_sends_outage_and_never_finalizes_daily(self):
        with patch.object(app, "collect", return_value=self.result((False, False))), patch.object(app, "deliver", return_value={}) as deliver:
            self.assertEqual(self.run_silently(), 2)
        kwargs = deliver.call_args.kwargs
        self.assertTrue(kwargs["delivery_key"].startswith("outage:"))
        self.assertNotEqual(kwargs["delivery_key"], "daily:2026-10-19")
        self.assertIn("无法判断是否有新闻", deliver.call_args.args[0][0])

    def test_partial_delivery_keeps_final_daily_key_available(self):
        with patch.object(app, "collect", return_value=self.result((True, False))), patch.object(app, "deliver", return_value={}) as deliver:
            self.assertEqual(self.run_silently(), 2)
        self.assertEqual(deliver.call_args.kwargs["delivery_key"], "partial:daily:2026-10-19")

    def test_healthy_digest_uses_final_daily_key(self):
        with patch.object(app, "collect", return_value=self.result()), patch.object(app, "deliver", return_value={}) as deliver:
            self.assertEqual(self.run_silently(), 0)
        self.assertEqual(deliver.call_args.kwargs["delivery_key"], "daily:2026-10-19")

    def test_dry_run_does_not_send_telegram(self):
        self.args.dry_run = True
        with patch.object(app, "collect", return_value=self.result()), patch.object(app, "deliver") as deliver:
            self.assertEqual(self.run_silently(), 0)
        deliver.assert_not_called()
        self.assertFalse(Path(self.args.state).exists())

    def test_completed_digest_reuses_frozen_content_without_recollecting(self):
        ledger = {"version": 1, "deliveries": {"daily:2026-10-19": {"complete": True, "messages": ["frozen"]}}}
        with patch.object(app, "load_state", return_value=ledger), patch.object(app, "collect") as collect, patch.object(app, "deliver", return_value={}) as deliver:
            self.assertEqual(self.run_silently(), 0)
        collect.assert_not_called()
        self.assertEqual(deliver.call_args.args[0], ["frozen"])

    def test_test_message_daily_key_is_isolated(self):
        self.args.test_message = True
        with patch.object(app, "collect", return_value=self.result()), patch.object(app, "deliver", return_value={}) as deliver:
            self.assertEqual(self.run_silently(), 0)
        self.assertEqual(deliver.call_args.kwargs["delivery_key"], "test:2026-10-19")
        self.assertIn("【测试】", deliver.call_args.args[0][0])

    def test_test_outage_uses_separate_key_and_label(self):
        self.args.test_message = True
        with patch.object(app, "collect", return_value=self.result((False, False))), patch.object(app, "deliver", return_value={}) as deliver:
            self.assertEqual(self.run_silently(), 2)
        self.assertEqual(deliver.call_args.kwargs["delivery_key"], "outage:test:2026-10-19")
        self.assertIn("【测试】", deliver.call_args.args[0][0])

    def test_catchup_respects_environment_start_date(self):
        with patch.dict(os.environ, {"MONITOR_START_DATE": "2026-10-19"}), patch.object(app, "reporting_date", return_value=self.day), patch.object(app, "run_day", return_value=0) as run:
            self.assertEqual(app.main(["--date", "2026-10-19", "--catch-up-days", "3"]), 0)
        self.assertEqual([call.args[0] for call in run.call_args_list], [self.day])

    def test_catchup_runs_oldest_first_and_preserves_failure(self):
        with patch.dict(os.environ, {"MONITOR_START_DATE": "2026-10-17"}), patch.object(app, "reporting_date", return_value=self.day), patch.object(app, "run_day", side_effect=[2, 0, 0]) as run:
            self.assertEqual(app.main(["--date", "2026-10-19", "--catch-up-days", "3"]), 2)
        self.assertEqual([call.args[0] for call in run.call_args_list], [date(2026, 10, 17), date(2026, 10, 18), self.day])


if __name__ == "__main__":
    unittest.main()
