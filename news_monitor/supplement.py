"""Explicitly archive and deliver user-submitted historical news without refetching it."""
from __future__ import annotations

import argparse
from datetime import date
import json
from pathlib import Path
import re
import sys
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .delivery import DeliveryError, deliver

DEFAULT_REGISTRY = Path(__file__).resolve().parents[1] / "data" / "manual_news.json"


def canonical_record_url(url):
    parsed = urlsplit(url)
    query = dict(parse_qsl(parsed.query))
    if parsed.hostname == "baijiahao.baidu.com" and parsed.path == "/s":
        article_id = query.get("id", "")
        if not article_id.isdigit():
            raise ValueError("Baijiahao supplemental link has no valid article ID")
        return f"https://baijiahao.baidu.com/s?id={article_id}"
    query = [(key, value) for key, value in parse_qsl(parsed.query)
             if not key.lower().startswith("utm_") and key.lower() not in {"wfr", "for", "fbclid", "gclid"}]
    return urlunsplit((parsed.scheme, parsed.netloc.lower(), parsed.path, urlencode(query), ""))


def load_registry(path=DEFAULT_REGISTRY):
    registry = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(registry, dict) or type(registry.get("version")) is not int or registry.get("version") != 1 or not isinstance(registry.get("records"), list):
        raise ValueError("Invalid supplemental news registry")
    records = {}
    seen_urls = set()
    required = ("id", "original_url", "title", "publisher", "original_source", "published_date",
                "published_time_label", "association", "evidence_type", "ingestion_method", "added_on")
    for record in registry["records"]:
        if not isinstance(record, dict) or any(not isinstance(record.get(key), str) or not record[key].strip() for key in required):
            raise ValueError("Supplemental news record is incomplete")
        identity = record["id"]
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,79}", identity) or identity in records:
            raise ValueError("Supplemental news IDs must be unique and valid")
        url = urlsplit(record["original_url"])
        if url.scheme != "https" or not url.hostname or url.username is not None or url.password is not None or url.port not in {None, 443}:
            raise ValueError("Supplemental news link must be HTTPS without credentials")
        canonical = canonical_record_url(record["original_url"])
        if canonical in seen_urls:
            raise ValueError("Supplemental news original links must be unique")
        seen_urls.add(canonical)
        if any(any(ord(c) < 32 or ord(c) == 127 for c in record[key]) for key in required):
            raise ValueError("Supplemental news fields contain unsupported control characters")
        if sum(len(record[key].encode("utf-16-le")) // 2 for key in required) > 2800:
            raise ValueError("Supplemental news record is too long")
        published = date.fromisoformat(record["published_date"])
        archived = date.fromisoformat(record["added_on"])
        if published.isoformat() != record["published_date"] or archived.isoformat() != record["added_on"]:
            raise ValueError("Supplemental news dates must use YYYY-MM-DD")
        if not record["published_time_label"].startswith(record["published_date"]):
            raise ValueError("Displayed publication time must preserve the original publication date")
        if published > archived:
            raise ValueError("Publication date cannot follow the supplemental archive date")
        if record["evidence_type"] != "saved_source" or record["ingestion_method"] != "user_submitted":
            raise ValueError("Supplemental news must explicitly retain manual provenance")
        records[identity] = dict(record)
    return records


def render_supplement(record):
    return "\n".join([
        "【历史新闻补录｜用户提交】",
        "工行金边分行相关新闻",
        "",
        record["title"],
        f"发布账号：{record['publisher']}｜原文来源：{record['original_source']}",
        f"原文发布时间：{record['published_time_label']}",
        f"补录日期：{record['added_on']}",
        "",
        f"分行关联：{record['association']}",
        "核实依据：已保存的原文页面存档；本次未重新抓取原文。",
        "收录方式：用户提交原链接，人工核实后补录。该记录不代表自动检索已发现原链接。",
        "",
        "原文链接：",
        record["original_url"],
        "",
        "此为历史新闻补录，未计入今天的“昨日新闻”日报。",
    ])


def run_supplement(record_id, *, registry_path=DEFAULT_REGISTRY,
                   state_path="state/deliveries.json", output_directory="reports",
                   dry_run=False, force=False):
    records = load_registry(registry_path)
    if record_id not in records:
        raise ValueError("Supplemental news ID was not found")
    record = records[record_id]
    message = render_supplement(record)
    output = Path(output_directory)
    output.mkdir(parents=True, exist_ok=True)
    (output / f"supplement-{record_id}.txt").write_text(message + "\n", encoding="utf-8")
    (output / f"supplement-{record_id}.json").write_text(
        json.dumps({"archive_record": record, "mode": "historical_user_submission"}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    result = {"record_id": record_id, "mode": "historical_user_submission", "dry_run": dry_run}
    if dry_run:
        print(message)
    else:
        result.update(deliver([message], delivery_key=f"supplement:{record_id}", state_path=state_path, force=force))
    return result


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--id", required=True, dest="record_id")
    parser.add_argument("--registry", default=str(DEFAULT_REGISTRY))
    parser.add_argument("--state", default="state/deliveries.json")
    parser.add_argument("--output", default="reports")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = run_supplement(args.record_id, registry_path=args.registry, state_path=args.state,
                                output_directory=args.output, dry_run=args.dry_run, force=args.force)
        print(json.dumps(result))
        return 0
    except (ValueError, OSError, DeliveryError):
        print("Supplemental news could not be archived or delivered; check its registry and delivery status.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
