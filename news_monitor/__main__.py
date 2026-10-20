"""Daily ICBC Phnom Penh branch news digest. No paid APIs or AI services."""
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys

from .collector import collect
from .delivery import DeliveryError, deliver, load_state

ICT = timezone(timedelta(hours=7), "Asia/Phnom_Penh")


def reporting_date(now=None):
    return ((now or datetime.now(ICT)).astimezone(ICT) - timedelta(days=1)).date()


def split_messages(text, limit=3500):
    # Telegram counts UTF-16 code units. Preserve links and complete entry lines.
    chunks = []
    current = ""
    for line in text.splitlines(keepends=True):
        while len(line.encode("utf-16-le")) // 2 > limit:
            if current:
                chunks.append(current.rstrip())
                current = ""
            end = min(limit - 1, len(line))
            while len(line[:end].encode("utf-16-le")) // 2 > limit:
                end -= 1
            chunks.append(line[:end])
            line = line[end:]
        if len((current + line).encode("utf-16-le")) // 2 > limit:
            chunks.append(current.rstrip())
            current = ""
        current += line
    if current.strip():
        chunks.append(current.rstrip())
    return chunks


def render(result, *, test=False, complete=None):
    healthy = result.complete if complete is None else complete
    count = len(result.articles)
    prefix = "【测试】" if test else ""
    lines = [f"{prefix}工行金边分行｜每日新闻汇总", f"新闻日期：{result.target_date}（金边时间）",
             f"监测范围：分行名称及 ICBC Cambodia 等中英文、柬文别名"]
    if not healthy:
        lines += ["⚠️ 部分新闻源采集失败，以下结果可能不完整。"]
    lines += [f"本次检索收录：{count} 条（已去重）", ""]
    if count == 0:
        lines += ["本次检索未发现符合日期和关联规则的新闻。" if healthy else
                  "已成功采集的来源中未发现符合条件的新闻；不能据此认定当天没有新闻。"]
    for index, article in enumerate(result.articles, 1):
        stamp = datetime.fromisoformat(article.published_at).astimezone(ICT).strftime("%H:%M")
        lines += [f"{index}. {article.title}", f"来源：{article.source}｜发布时间：{stamp}"]
        if article.summary:
            lines += [f"来源摘要：{article.summary[:400]}"]
        lines += [article.url, ""]
    lines += ["说明：按新闻源标注的发布时间筛选；保留原文标题与链接。免费检索有收录及发布时间误差，未收录内容可能遗漏。"]
    return "\n".join(lines)


def run_day(day, args):
    key = ("test:" if args.test_message else "daily:") + day.isoformat()
    state = load_state(args.state)
    # Skip collection too when all existing recipients already received the digest.
    # deliver() checks the current recipient list, so newly added recipients can catch up.
    record = state["deliveries"].get(key)
    if record and record.get("complete") and not args.force and not args.dry_run:
        result = deliver(record["messages"], delivery_key=key, state_path=args.state)
        print(json.dumps({"date": day.isoformat(), "previously_completed": True, **result}))
        return 0
    result = collect(day)
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    report = render(result, test=args.test_message)
    (output / f"{day}.txt").write_text(report + "\n", encoding="utf-8")
    diagnostics = {**result.diagnostics, "queries": [asdict(status) for status in result.statuses],
                   "items": [asdict(article) for article in result.articles]}
    (output / f"{day}.json").write_text(json.dumps(diagnostics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"date": day.isoformat(), "articles": len(result.articles), "complete": result.complete,
                      "queries_ok": sum(s.ok for s in result.statuses), "queries_total": len(result.statuses)}))
    if args.dry_run:
        print(report)
        return 0 if result.complete else 2
    healthy = sum(status.ok for status in result.statuses)
    if healthy == 0:
        warning = (("【测试】" if args.test_message else "") + f"工行金边分行新闻监测失败\n新闻日期：{day}\n所有新闻检索源均采集失败，无法判断是否有新闻。"
                   "\n系统会在后续运行重试，请查看 GitHub Actions。")
        status = deliver([warning], delivery_key=f"outage:{key}", state_path=args.state)
        print(json.dumps({"source_outage": True, **status}))
        return 2
    if not result.complete:
        # Keep a partial-result warning separate from the final daily delivery key.
        # A later healthy retry can deliver the complete digest.
        status = deliver(split_messages(report), delivery_key=f"partial:{key}", state_path=args.state,
                         force=args.force)
    else:
        status = deliver(split_messages(report), delivery_key=key, state_path=args.state, force=args.force)
    print(json.dumps({"date": day.isoformat(), **status}))
    return 0 if result.complete else 2


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", type=date.fromisoformat, help="Reporting date YYYY-MM-DD; default yesterday ICT")
    parser.add_argument("--dry-run", action="store_true", help="Collect and render without Telegram or delivery state writes")
    parser.add_argument("--test-message", action="store_true", help="Send a labelled test using a separate ledger")
    parser.add_argument("--force", action="store_true", help="Explicitly resend a previously delivered report")
    parser.add_argument("--catch-up-days", type=int, choices=range(1, 8), default=1)
    parser.add_argument("--start-date", type=date.fromisoformat, default=os.environ.get("MONITOR_START_DATE"),
                        help="Do not backfill reports earlier than this date")
    parser.add_argument("--state", default="state/deliveries.json")
    parser.add_argument("--output", default="reports")
    args = parser.parse_args(argv)
    day = args.date or reporting_date()
    if day > reporting_date() and not args.test_message:
        parser.error("Daily reports only cover completed Phnom Penh calendar days")
    exit_code = 0
    for offset in reversed(range(args.catch_up_days)):
        if args.start_date and day - timedelta(days=offset) < args.start_date:
            continue
        try:
            exit_code = max(exit_code, run_day(day - timedelta(days=offset), args))
        except (DeliveryError, OSError, ValueError) as exc:
            # Our own exceptions contain no transport URL / Telegram credentials.
            print(f"Monitor failed: {type(exc).__name__}", file=sys.stderr)
            exit_code = 2
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
