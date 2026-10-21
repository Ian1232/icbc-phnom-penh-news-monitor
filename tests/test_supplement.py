import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from news_monitor import delivery, supplement


RECORD_ID = "baijiahao-1877016373831758570"
ORIGINAL_URL = "https://baijiahao.baidu.com/s?id=1877016373831758570&wfr=spider&for=pc"


def record():
    return {
        "id": RECORD_ID,
        "original_url": ORIGINAL_URL,
        "title": "柬埔寨川渝总商会700余人共庆中国国庆节和中秋节 共话中柬友谊、共谋合作发展",
        "publisher": "中国新闻网（百家号）",
        "original_source": "柬华日报",
        "published_date": "2026-09-21",
        "published_time_label": "2026-09-21 13:38（原页面未注明时区）",
        "association": "报道将中国工商银行金边分行列为参会机构之一。",
        "evidence_type": "saved_source",
        "ingestion_method": "user_submitted",
        "added_on": "2026-10-21",
    }


class SupplementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.registry = self.root / "manual_news.json"
        self.state = self.root / "state" / "deliveries.json"
        self.output = self.root / "reports"
        self.save_registry([record()])

    def save_registry(self, records, *, version=1):
        self.registry.write_text(json.dumps({"version": version, "records": records}, ensure_ascii=False), encoding="utf-8")

    def execute(self, identity=RECORD_ID, **kwargs):
        return supplement.run_supplement(identity, registry_path=self.registry, state_path=self.state,
                                         output_directory=self.output, **kwargs)

    def test_known_original_record_preserves_publication_calendar_and_url(self):
        saved = supplement.load_registry(self.registry)[RECORD_ID]
        self.assertEqual(saved["published_date"], "2026-09-21")
        self.assertEqual(saved["added_on"], "2026-10-21")
        self.assertEqual(saved["original_url"], ORIGINAL_URL)
        text = supplement.render_supplement(saved)
        self.assertIn("原文发布时间：2026-09-21 13:38", text)
        self.assertIn("原页面未注明时区", text)
        self.assertIn("补录日期：2026-10-21", text)
        self.assertNotIn("原文发布时间：2026-10-21", text)

    def test_render_keeps_manual_historical_provenance_explicit(self):
        text = supplement.render_supplement(record())
        self.assertTrue(text.startswith("【历史新闻补录｜用户提交】"))
        self.assertIn("已保存的原文页面存档", text)
        self.assertIn("本次未重新抓取原文", text)
        self.assertIn("该记录不代表自动检索已发现原链接", text)
        self.assertIn("未计入今天的“昨日新闻”日报", text)
        self.assertIn(ORIGINAL_URL, text)

    def test_only_explicitly_selected_record_is_sent(self):
        other = record()
        other.update(id="another-historical-record", title="Another article", original_url="https://publisher.example.org/story2")
        self.save_registry([record(), other])
        with patch.object(supplement, "deliver", return_value={"sent_messages": 1}) as send:
            result = self.execute()
        self.assertEqual(send.call_count, 1)
        self.assertEqual(send.call_args.kwargs["delivery_key"], f"supplement:{RECORD_ID}")
        self.assertEqual(len(send.call_args.args[0]), 1)
        self.assertNotIn("Another article", send.call_args.args[0][0])
        self.assertEqual(result["mode"], "historical_user_submission")
        self.assertEqual(result["record_id"], RECORD_ID)

    def test_unknown_or_missing_record_id_fails_without_delivery_or_artifacts(self):
        with patch.object(supplement, "deliver") as send:
            for identity in ("missing-record", "", None, "../../unexpected"):
                with self.subTest(identity=identity):
                    with self.assertRaises(ValueError):
                        self.execute(identity)
        send.assert_not_called()
        self.assertFalse(self.output.exists())
        self.assertFalse(self.state.exists())

    def test_cli_requires_selected_id(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            supplement.main(["--registry", str(self.registry), "--dry-run"])
        self.assertEqual(caught.exception.code, 2)
        self.assertFalse(self.state.exists())

    def test_dry_run_does_not_send_or_write_delivery_state(self):
        with patch.object(supplement, "deliver") as send, contextlib.redirect_stdout(io.StringIO()) as output:
            result = self.execute(dry_run=True)
        send.assert_not_called()
        self.assertFalse(self.state.exists())
        self.assertTrue(result["dry_run"])
        self.assertIn("历史新闻补录", output.getvalue())
        self.assertTrue((self.output / f"supplement-{RECORD_ID}.txt").exists())
        audit = json.loads((self.output / f"supplement-{RECORD_ID}.json").read_text(encoding="utf-8"))
        self.assertEqual(audit["archive_record"]["published_date"], "2026-09-21")
        self.assertEqual(audit["mode"], "historical_user_submission")

    def test_supplement_does_not_collect_or_fetch_article_again(self):
        with patch("news_monitor.collector.collect", side_effect=AssertionError("unexpected index fetch")), patch("news_monitor.article_content.inspect_article", side_effect=AssertionError("unexpected article fetch")), patch.object(supplement, "deliver", return_value={}):
            self.execute()

    def test_repeat_execution_uses_durable_supplement_key_not_daily_key(self):
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "unit-test-private-token", "TELEGRAM_CHAT_IDS": "123456789012345", "TELEGRAM_CHAT_ID": ""}), patch.object(delivery, "send_message", return_value=42) as send, patch.object(delivery.time, "sleep"):
            first = self.execute()
            second = self.execute()
        self.assertEqual(send.call_count, 1)
        self.assertEqual(first["sent_messages"], 1)
        self.assertEqual(second["sent_messages"], 0)
        self.assertEqual(second["skipped_messages"], 1)
        state = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(list(state["deliveries"]), [f"supplement:{RECORD_ID}"])
        serialized = self.state.read_text(encoding="utf-8")
        self.assertNotIn("unit-test-private-token", serialized)
        self.assertNotIn("123456789012345", serialized)

    def test_force_is_explicitly_forwarded(self):
        with patch.object(supplement, "deliver", return_value={}) as send:
            self.execute(force=True)
        self.assertTrue(send.call_args.kwargs["force"])

    def test_registry_version_must_be_integer_one(self):
        for value in (True, 1.0, "1", 0, 2, None):
            with self.subTest(version=value):
                self.save_registry([record()], version=value)
                with self.assertRaises(ValueError):
                    supplement.load_registry(self.registry)

    def test_malformed_registry_and_missing_required_fields_are_rejected(self):
        for registry in ([], {}, {"version": 1, "records": {}}, {"version": 1, "records": [None]}):
            with self.subTest(registry=registry):
                self.registry.write_text(json.dumps(registry), encoding="utf-8")
                with self.assertRaises(ValueError):
                    supplement.load_registry(self.registry)
        for key in record():
            with self.subTest(missing=key):
                incomplete = record()
                del incomplete[key]
                self.save_registry([incomplete])
                with self.assertRaises(ValueError):
                    supplement.load_registry(self.registry)

    def test_duplicate_or_unsafe_record_ids_are_rejected(self):
        self.save_registry([record(), record()])
        with self.assertRaises(ValueError):
            supplement.load_registry(self.registry)
        for identity in ("../../news", "Uppercase", "with space", "-leading", "x" * 81):
            with self.subTest(identity=identity):
                item = record()
                item["id"] = identity
                self.save_registry([item])
                with self.assertRaises(ValueError):
                    supplement.load_registry(self.registry)

    def test_registry_retains_manual_evidence_type(self):
        for key, value in (("ingestion_method", "automatic_search"), ("evidence_type", "live_fetch")):
            with self.subTest(key=key):
                item = record()
                item[key] = value
                self.save_registry([item])
                with self.assertRaises(ValueError):
                    supplement.load_registry(self.registry)

    def test_invalid_dates_and_publication_after_archive_are_rejected(self):
        for published, added in (("2026-02-30", "2026-10-21"), ("2026-10-22", "2026-10-21"), ("2026-09-21", "not-a-date")):
            with self.subTest(published=published, added=added):
                item = record()
                item["published_date"], item["added_on"] = published, added
                self.save_registry([item])
                with self.assertRaises(ValueError):
                    supplement.load_registry(self.registry)

    def test_compact_dates_and_wrong_display_publication_date_are_rejected(self):
        for key, value in (("published_date", "20260921"), ("added_on", "20261021"), ("published_time_label", "2026-10-21 13:38")):
            with self.subTest(key=key, value=value):
                item = record()
                item[key] = value
                self.save_registry([item])
                with self.assertRaises(ValueError):
                    supplement.load_registry(self.registry)

    def test_same_original_url_with_different_tracking_cannot_be_archived_twice(self):
        duplicate = record()
        duplicate["id"] = "same-source-other-id"
        duplicate["original_url"] = "https://baijiahao.baidu.com/s?for=mobile&id=1877016373831758570&utm_source=example"
        self.save_registry([record(), duplicate])
        with self.assertRaises(ValueError):
            supplement.load_registry(self.registry)

    def test_baijiahao_original_link_requires_article_id(self):
        item = record()
        item["original_url"] = "https://baijiahao.baidu.com/s?wfr=spider"
        self.save_registry([item])
        with self.assertRaises(ValueError):
            supplement.load_registry(self.registry)

    def test_supplement_payload_bound_counts_utf16_units(self):
        item = record()
        item["association"] = "😀" * 1500
        self.save_registry([item])
        with self.assertRaises(ValueError):
            supplement.load_registry(self.registry)

    def test_credential_urls_non_https_and_control_characters_are_rejected(self):
        for url in ("http://publisher.example.org/news", "https://user:private@publisher.example.org/news", "https://publisher.example.org:8443/news", "javascript:alert(1)", ORIGINAL_URL + "\nmessage"):
            with self.subTest(url=url):
                item = record()
                item["original_url"] = url
                self.save_registry([item])
                with self.assertRaises(ValueError):
                    supplement.load_registry(self.registry)

    def test_bad_registry_cli_error_is_sanitized_and_does_not_send(self):
        self.registry.write_text('{"private":"transport-secret"', encoding="utf-8")
        with contextlib.redirect_stderr(io.StringIO()) as stderr, contextlib.redirect_stdout(io.StringIO()), patch.object(supplement, "deliver") as send:
            code = supplement.main(["--id", RECORD_ID, "--registry", str(self.registry), "--state", str(self.state), "--output", str(self.output)])
        self.assertEqual(code, 2)
        self.assertNotIn("transport-secret", stderr.getvalue())
        send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
