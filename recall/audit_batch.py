"""Run a bounded source-grounding review over current official recall notices.

    uv run python -m recall.audit_batch --limit 10

The report measures only whether a second model finds the Context.dev extraction supported by
the same source HTML. It is not an extraction-accuracy study and must not be presented as one.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from .config import load_env, load_watch
from .contextdev import ContextDev
from .verda import FIELDS, VerdaExtractionAuditor


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--out", type=Path, help="write the JSON report to this path as well as the console")
    args = parser.parse_args()
    if not 1 <= args.limit <= 20:
        raise SystemExit("--limit must be between 1 and 20")
    load_env()
    context, auditor = ContextDev(), VerdaExtractionAuditor()
    items = context.recall_list(load_watch()["list"]["url"])[:args.limit]
    rows = []
    for index, item in enumerate(items, start=1):
        raw = (context.scrape(item["url"], {"formats": {"html": True}}).get("html") or {})
        if not raw.get("success"):
            rows.append({"url": item["url"], "title": item["title"], "error": "source_html_unavailable"})
            continue
        notice = context.read_notice(item, time.time())
        audited = auditor.audit(notice, raw["data"])["audit"]
        rows.append({
            "url": item["url"], "title": item["title"], "overall": audited["overall"],
            "field_status": {field: audited["field_status"][field]["status"] for field in FIELDS},
            "concerns": audited.get("concerns") or [],
        })
        print(f"audited {index}/{len(items)}: {item['title']}", flush=True)
    counts = {field: {status: 0 for status in ("supported", "contradicted", "not_stated", "unclear")} for field in FIELDS}
    for row in rows:
        for field, status in (row.get("field_status") or {}).items():
            counts[field][status] += 1
    report = {
        "kind": "second_model_source_grounding_review",
        "warning": "This is not a human extraction-accuracy study.",
        "model": auditor.model,
        "sample_size": len(rows),
        "field_status_counts": counts,
        "rows": rows,
    }
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(rendered + "\n")
    print(rendered)


if __name__ == "__main__":
    main()
