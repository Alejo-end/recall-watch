"""Run an optional Kimi source-grounding audit for one Context.dev recall extraction.

    uv run python -m recall.audit_notice https://www.ruokavirasto.fi/.../

This prints a review aid. It does not alter notices, matches, holds or the audit trail.
"""

from __future__ import annotations

import json
import sys
import time

from .config import load_env
from .contextdev import ContextDev, domain, notice_id
from .verda import VerdaExtractionAuditor


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: uv run python -m recall.audit_notice <notice-url>")
    load_env()
    url, now = sys.argv[1], time.time()
    context = ContextDev()
    raw = context.scrape(url, {"formats": {"html": True}}).get("html") or {}
    if not raw.get("success"):
        raise SystemExit(f"Context.dev did not return source HTML: {raw.get('message') or raw.get('error_code')}")
    notice = context.read_notice({"url": url, "title": "", "date": ""}, now, official=domain(url) == "ruokavirasto.fi")
    # `read_notice` uses the URL for a stable ID; title/date are not the subject of this audit.
    notice.id = notice_id(url)
    print(json.dumps(VerdaExtractionAuditor().audit(notice, raw["data"]), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
