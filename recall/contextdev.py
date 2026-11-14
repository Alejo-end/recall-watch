"""The calls to Context.dev that Recall Watch makes.

Every request shape comes from the documentation at docs.context.dev, read on 14 November 2026:
  scrape a page    POST /v1/web/scrape, formats.html     (docs.context.dev/scrape/overview)
  read fields      POST /v1/web/scrape, formats.json     (docs.context.dev/scrape/json)
  news search      POST /v1/news/search                  (docs.context.dev/news/overview)
  create monitor   POST /v1/monitors                     (docs.context.dev/monitors/overview)
  list monitors    GET  /v1/monitors?q=&search_by=name   (same page, "List monitors")
  all changes      GET  /v1/monitors/changes?since=      (docs.context.dev/api-reference/monitors/account-changes)
  webhook check    X-Context-Signature: t=<unix>,v1=<hex HMAC-SHA256 of "t.body"> (docs.context.dev/webhooks)

The backend made none of these calls against the live API yet: it has no key. The tests check the
request shapes against the documentation with a fake transport. On 14 November, the two scrape
calls ran once by hand through Context.dev's MCP server, with this schema, and both answered.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import os
import re
import time
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import httpx

from .engine import ALLERGENS, HAZARDS, Notice

BASE_URL = "https://api.context.dev/v1"
NAME_PREFIX = "Recall Watch:"
MONTHS_FI = "tammi helmi maalis huhti touko kesä heinä elo syys loka marras joulu".split()

# The fields that Context.dev reads from one recall notice. Every field can be missing on a page,
# so none is required: a required field receives a guess (docs.context.dev/scrape/json, "Design notes").
NOTICE_SCHEMA = {
    "type": "object",
    "properties": {
        "company": {"type": ["string", "null"], "description": "The company that recalls the product"},
        "brand": {"type": ["string", "null"], "description": "The brand on the pack"},
        "products": {"type": "array", "description": "Each recalled product", "items": {"type": "object", "properties": {
            "name": {"type": "string", "description": "The product name with its pack size, as written on the page"},
            "ean": {"type": ["string", "null"], "description": "The EAN or GTIN code, digits only"}}}},
        "lot": {"type": ["string", "null"], "description": "Lot codes and best before or use by dates that the recall covers, in English"},
        "hazard": {"type": ["string", "null"], "enum": [*HAZARDS, None]},
        "allergens": {"type": "array", "items": {"type": "string", "enum": list(ALLERGENS)},
                      "description": "The allergens that the notice warns about"},
        "summary": {"type": ["string", "null"], "description": "One plain English sentence: what is wrong with the product"},
    },
}
NOTICE_INSTRUCTIONS = ("This page is a food recall notice from Finland, usually in Finnish. Use only facts stated on the page. "
                       "Leave a field empty when the page does not state it. Do not translate product names. "
                       "Write the fields lot and summary in English.")


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(value: str | None, default: float) -> float:
    if not value:
        return default
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return default


def domain(url: str) -> str:
    return (urlparse(url).hostname or "").removeprefix("www.")


class ContextDev:
    def __init__(self, api_key: str | None = None, transport: httpx.BaseTransport | None = None):
        key = api_key or os.environ.get("CONTEXT_DEV_API_KEY")
        if not key:
            raise RuntimeError("Set CONTEXT_DEV_API_KEY in recall-watch/.env, or run with SOURCE=replay")
        self.http = httpx.Client(base_url=BASE_URL, transport=transport, timeout=30,
                                 headers={"Authorization": f"Bearer {key}"})

    # Pages ------------------------------------------------------------------
    def scrape(self, url: str, body: dict) -> dict:
        r = self.http.post("/web/scrape", json={"url": url, **body})
        r.raise_for_status()
        return r.json()

    def recall_list(self, url: str) -> list[dict]:
        """The notices on the authority's list page: date, title and address of each."""
        out = self.scrape(url, {"formats": {"html": True}}).get("html") or {}
        if not out.get("success"):
            raise RuntimeError(f"Context.dev did not return the list page: {out.get('error_code')} {out.get('message')}")
        return parse_recall_list(out["data"], url)

    def read_notice(self, item: dict, now: float, official: bool = True) -> Notice:
        """Read the fields of one notice. `item` has url and title, and date when the list gave one."""
        t0 = time.time()
        out = self.scrape(item["url"], {"formats": {"json": True},
                                         "jsonParams": {"schema": NOTICE_SCHEMA, "instructions": NOTICE_INSTRUCTIONS}}).get("json") or {}
        if not out.get("success"):
            raise RuntimeError(f"Context.dev did not read {item['url']}: {out.get('error_code')} {out.get('message')}")
        d = out.get("data") or {}
        products = [p for p in d.get("products") or [] if isinstance(p, dict)]
        date = item.get("date") or iso(now)[:10]
        # A notice counts from the moment this system reads it, also before 09:00 on its date.
        published_at = min(parse_ts(date + "T09:00:00+02:00", now), now)
        return Notice(
            id=notice_id(item["url"]), url=item["url"], title=item.get("title") or "", date=date,
            published_at=published_at, detected_at=now, source=domain(item["url"]),
            official=official, company=d.get("company") or "", brand=d.get("brand") or "",
            products=[p.get("name") or "" for p in products], eans=[p.get("ean") or "" for p in products],
            lot=d.get("lot") or "", hazard=d.get("hazard") or "other", allergens=d.get("allergens") or [],
            summary=d.get("summary") or item.get("title") or "", extracted_by="context.dev",
            extract_ms=int((time.time() - t0) * 1000))

    # News -------------------------------------------------------------------
    def news(self, query: str, since: float, limit: int = 10) -> list[dict]:
        body = {"searchBy": {"type": "natural_language", "query": query}, "limit": limit,
                "filterBy": {"date": {"from": int(since * 1000)}}}
        r = self.http.post("/news/search", json=body)
        r.raise_for_status()
        return [{"id": f"news:{a.get('story_id') or a['id']}", "title": a.get("title") or "", "url": a.get("url") or "",
                 "source": (a.get("source") or {}).get("domain") or domain(a.get("url") or ""),
                 "published_at": parse_ts(a.get("published_at"), time.time())} for a in r.json().get("data", [])]

    # Monitors ---------------------------------------------------------------
    def monitors(self) -> list[dict]:
        r = self.http.get("/monitors", params={"q": "Recall Watch", "search_by": "name", "limit": 100})
        r.raise_for_status()
        return [m for m in r.json().get("data", []) if m.get("name", "").startswith(NAME_PREFIX)]

    def create_monitor(self, spec: dict) -> dict:
        body = {"name": spec["name"], "target": spec["target"],
                "schedule": {"type": "interval", "frequency": 10, "unit": "minutes"},
                "tags": ["recall-watch", f"kind-{spec['kind']}"]}
        if "change_detection" in spec:
            body["change_detection"] = spec["change_detection"]
        r = self.http.post("/monitors", json=body)
        r.raise_for_status()
        return r.json()

    def changes(self, since: float, kinds: dict[str, str]) -> list[dict]:
        """Changes of our monitors at or after `since`, each with its `kind`. `kinds` maps a monitor id to its kind."""
        out, cursor = [], None
        while True:
            params = {"since": iso(since), "limit": 100}
            if cursor:
                params["cursor"] = cursor
            r = self.http.get("/monitors/changes", params=params)
            r.raise_for_status()
            page = r.json()
            out += [{**c, "kind": kinds[c["monitor_id"]]} for c in page.get("data", []) if c.get("monitor_id") in kinds]
            cursor = page.get("next_cursor")
            if not page.get("has_more") or not cursor:
                return out


def notice_id(url: str) -> str:
    """The domain, the path and the query of the address. Two pages that differ only by the query are two notices."""
    u = urlparse(url)
    return (domain(url) + u.path).rstrip("/") + ("?" + u.query if u.query else "")


def parse_recall_list(html_text: str, base: str) -> list[dict]:
    """The items of the recall list of ruokavirasto.fi: a date, then a link with the title.

    Checked against the page as it was on 14 November 2026 (tests/fixtures/recall-list.html).
    """
    out = []
    pattern = r'(\d{1,2})\.\s*(\w+?)kuuta\s+(\d{4})\s*</p>.*?<a[^>]*href="([^"]+)"[^>]*>\s*(.*?)\s*</a>'
    for d, m, y, href, title in re.findall(pattern, html_text, flags=re.S):
        if m not in MONTHS_FI:
            continue
        out.append({"date": f"{y}-{MONTHS_FI.index(m) + 1:02d}-{int(d):02d}", "url": urljoin(base, href),
                    "title": html.unescape(re.sub(r"<[^>]+>", "", title)).strip()})
    return out


def verify_webhook(raw_body: bytes, header: str, secret: str, now: float | None = None, tolerance_s: int = 300) -> bool:
    """The check from docs.context.dev/webhooks, "Verify signatures"."""
    if not secret or not header:
        return False
    parts: dict[str, str] = {}
    for part in header.split(","):
        pair = part.strip().split("=")
        if len(pair) != 2 or pair[0] in parts:
            return False
        parts[pair[0]] = pair[1]
    t, v1 = parts.get("t", ""), parts.get("v1", "")
    if not (re.fullmatch(r"[0-9]{1,12}", t) and re.fullmatch(r"[0-9a-fA-F]{64}", v1)):
        return False
    if abs((now or time.time()) - int(t)) > tolerance_s:
        return False
    expected = hmac.new(secret.encode(), f"{t}.".encode() + raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, v1.lower())
