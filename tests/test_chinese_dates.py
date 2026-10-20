from datetime import datetime, timedelta, timezone
import unittest

from news_monitor.chinese_dates import CHINA_TZ, baidu_datetime_bounds, parse_baidu_datetime


ICT = timezone(timedelta(hours=7))


class BaiduDateTests(unittest.TestCase):
    def setUp(self):
        self.observed = datetime(2026, 10, 20, 8, 15, 30, tzinfo=ICT)

    def parse(self, text):
        return parse_baidu_datetime(text, observed_at=self.observed)

    def test_explicit_date_formats_preserve_china_time(self):
        for text in ("2026年10月19日 14:05", "2026-10-19 14:05", "2026/10/19 14:05", "2026年10月19日14:05"):
            with self.subTest(text=text):
                self.assertEqual(self.parse(text), datetime(2026, 10, 19, 14, 5, tzinfo=CHINA_TZ))

    def test_seconds_fullwidth_and_publish_prefix(self):
        self.assertEqual(self.parse("发布时间：２０２６年１０月１９日 １４：０５：０９"),
                         datetime(2026, 10, 19, 14, 5, 9, tzinfo=CHINA_TZ))

    def test_china_midnight_maps_to_previous_phnom_penh_date(self):
        parsed = self.parse("2026-10-20 00:30")
        self.assertEqual(parsed.astimezone(ICT), datetime(2026, 10, 19, 23, 30, tzinfo=ICT))
        self.assertEqual(self.parse("2026-10-20 01:00").astimezone(ICT).day, 20)

    def test_date_only_cannot_determine_phnom_penh_reporting_day(self):
        for text in ("2026年10月19日", "2026-10-19", "2026/10/19", "10月19日", "10-19", "今天", "昨天", "前天"):
            with self.subTest(text=text):
                self.assertIsNone(self.parse(text))

    def test_month_day_infers_current_year(self):
        self.assertEqual(self.parse("10月19日 22:45"), datetime(2026, 10, 19, 22, 45, tzinfo=CHINA_TZ))
        self.assertEqual(self.parse("10-19 22:45:12"), datetime(2026, 10, 19, 22, 45, 12, tzinfo=CHINA_TZ))

    def test_january_infers_last_year_for_december(self):
        observed = datetime(2027, 1, 1, 2, tzinfo=CHINA_TZ)
        parsed = parse_baidu_datetime("12月31日 23:30", observed_at=observed)
        self.assertEqual(parsed, datetime(2026, 12, 31, 23, 30, tzinfo=CHINA_TZ))

    def test_same_day_future_clock_is_not_misread_as_last_year(self):
        self.assertEqual(self.parse("10-20 23:59"), datetime(2026, 10, 20, 23, 59, tzinfo=CHINA_TZ))

    def test_year_inference_uses_china_calendar_not_observer_zone(self):
        observed = datetime(2026, 12, 31, 23, 30, tzinfo=ICT)
        self.assertEqual(parse_baidu_datetime("01-01 00:15", observed_at=observed),
                         datetime(2027, 1, 1, 0, 15, tzinfo=CHINA_TZ))

    def test_leap_day_inference_and_invalid_dates(self):
        self.assertEqual(self.parse("2月29日 08:00"), datetime(2024, 2, 29, 8, tzinfo=CHINA_TZ))
        for text in ("2026年2月29日 12:00", "2026-13-19 14:00", "2026-10-32 14:00", "2026-10-19 24:00", "2026-10-19 14:60"):
            with self.subTest(text=text):
                self.assertIsNone(self.parse(text))

    def test_relative_minutes_hours_and_days(self):
        china_observed = self.observed.astimezone(CHINA_TZ)
        self.assertEqual(self.parse("刚刚"), china_observed)
        self.assertEqual(self.parse("5分钟前"), china_observed - timedelta(minutes=5))
        self.assertEqual(self.parse("3小时前"), china_observed - timedelta(hours=3))
        self.assertEqual(self.parse("2天前"), china_observed - timedelta(days=2))

    def test_relative_calendar_days_use_china_time(self):
        self.assertEqual(self.parse("今天 08:00"), datetime(2026, 10, 20, 8, tzinfo=CHINA_TZ))
        self.assertEqual(self.parse("昨天23:30:05"), datetime(2026, 10, 19, 23, 30, 5, tzinfo=CHINA_TZ))
        self.assertEqual(self.parse("前天 00:30").astimezone(ICT), datetime(2026, 10, 17, 23, 30, tzinfo=ICT))

    def test_relative_date_with_utc_observation_rolls_over_in_china(self):
        observed = datetime(2026, 10, 19, 16, 30, tzinfo=timezone.utc)
        parsed = parse_baidu_datetime("昨天 23:59", observed_at=observed)
        self.assertEqual(parsed, datetime(2026, 10, 19, 23, 59, tzinfo=CHINA_TZ))

    def test_cache_index_and_update_labels_are_not_publication_dates(self):
        for text in ("百度快照", "百度快照 2026-10-19 13:00", "更新时间：2026-10-19 13:00", "2026-10-19 13:00 抓取时间", "收录时间 3小时前", "网页快照 昨天08:00"):
            with self.subTest(text=text):
                self.assertIsNone(self.parse(text))

    def test_unknown_bad_relative_and_unlabelled_clock_return_none(self):
        for text in ("", "来源：百度新闻", "相关资讯", "08:30", "-1小时前", "1.5小时前", "9999999999999999999999999天前"):
            with self.subTest(text=text):
                self.assertIsNone(self.parse(text))

    def test_explicit_foreign_timezone_is_not_relabelled_as_china_time(self):
        for text in ("2026-10-19T14:00:00Z", "2026-10-19 14:00 UTC", "2026-10-19 14:00+07:00"):
            with self.subTest(text=text):
                self.assertIsNone(self.parse(text))
        self.assertEqual(self.parse("2026-10-19 14:00+08:00"), datetime(2026, 10, 19, 14, tzinfo=CHINA_TZ))

    def test_observation_must_be_aware(self):
        with self.assertRaises(ValueError):
            parse_baidu_datetime("2026-10-19 14:00", observed_at=datetime(2026, 10, 20))
        with self.assertRaises(TypeError):
            parse_baidu_datetime("2026-10-19 14:00", observed_at=None)

    def test_date_only_bounds_explicitly_straddle_two_phnom_penh_dates(self):
        bounds = baidu_datetime_bounds("2026年10月19日", observed_at=self.observed)
        self.assertIsNone(bounds.published_at)
        self.assertEqual(bounds.earliest.astimezone(ICT), datetime(2026, 10, 18, 23, tzinfo=ICT))
        self.assertEqual(bounds.latest.astimezone(ICT), datetime(2026, 10, 19, 23, tzinfo=ICT))
        self.assertTrue(bounds.error)

    def test_exact_bounds_include_resolved_timestamp(self):
        bounds = baidu_datetime_bounds("2026-10-19 14:00", observed_at=self.observed)
        self.assertEqual(bounds.published_at, datetime(2026, 10, 19, 14, tzinfo=CHINA_TZ))
        self.assertLess(bounds.earliest, bounds.latest)
        self.assertEqual(bounds.error, "")

    def test_coarse_age_bounds_can_expose_midnight_ambiguity(self):
        observed = datetime(2026, 10, 20, 0, 0, 30, tzinfo=ICT)
        bounds = baidu_datetime_bounds("刚刚", observed_at=observed)
        self.assertIsNone(bounds.published_at)
        self.assertEqual(bounds.earliest.astimezone(ICT).day, 19)
        self.assertEqual(bounds.latest.astimezone(ICT).day, 20)
        bounds = baidu_datetime_bounds("1小时前", observed_at=observed)
        self.assertEqual(bounds.earliest, observed.astimezone(CHINA_TZ) - timedelta(hours=2))
        self.assertTrue(bounds.error)

    def test_relative_days_bounds_cover_elapsed_and_calendar_interpretations(self):
        bounds = baidu_datetime_bounds("1天前", observed_at=self.observed)
        self.assertIsNone(bounds.published_at)
        self.assertLessEqual(bounds.earliest, self.observed.astimezone(CHINA_TZ) - timedelta(days=2))
        self.assertGreaterEqual(bounds.latest, datetime(2026, 10, 20, tzinfo=CHINA_TZ))

    def test_unknown_or_cache_bounds_are_not_unbounded_successes(self):
        for text in ("百度快照 昨天08:00", "unknown", "2026-10-19 24:00"):
            with self.subTest(text=text):
                bounds = baidu_datetime_bounds(text, observed_at=self.observed)
                self.assertIsNone(bounds.earliest)
                self.assertIsNone(bounds.latest)
                self.assertTrue(bounds.error)


if __name__ == "__main__":
    unittest.main()
