"""The calls to Context.dev that Recall Watch makes.

Every request shape comes from the documentation at docs.context.dev, read on 14 November 2026:
  scrape a page    POST /v1/web/scrape, formats.html     (docs.context.dev/scrape/overview)
  read fields      POST /v1/web/scrape, formats.json     (docs.context.dev/scrape/json)
  news search      POST /v1/news/search                  (docs.context.dev/news/overview)
  create monitor   POST /v1/monitors                     (docs.context.dev/monitors/overview)
  list monitors    GET  /v1/monitors?q=&search_by=name   (same page, "List monitors")
  all changes      GET  /v1/monitors/changes?since=      (docs.context.dev/api-reference/monitors/account-changes)
  run a monitor    POST /v1/monitors/{id}/run            (docs.context.dev/monitors/schedules, read on 15 November 2026)
  webhook check    X-Context-Signature: t=<unix>,v1=<hex HMAC-SHA256 of "t.body"> (docs.context.dev/webhooks)

The backend path has tests against a fake transport. Two Recall Watch monitors are configured in
Context.dev. On 14 November, two scrape calls ran once by hand through Context.dev's MCP server,
with this schema, and both answered. A complete backend live synchronization still needs a
hand-check of the extracted fields.
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
        "title_fi": {"type": ["string", "null"], "description": "The notice headline exactly as written on the page, in its original language"},
        "published_date": {"type": ["string", "null"], "description": "The publication date stated on the page, as YYYY-MM-DD"},
        "title_en": {"type": ["string", "null"], "description": "The notice title translated to plain English. A working translation; keep product names as written"},
    },
}
NOTICE_INSTRUCTIONS = ("This page is a food recall notice from Finland, usually in Finnish. Use only facts stated on the page. "
                       "Leave a field empty when the page does not state it. Do not translate product names. "
                       "Write the fields lot, summary and title_en in English.")
# Recorded with every case so a reader knows who produced the English title.
MACHINE_TRANSLATION = "Machine working translation from the Context.dev extraction of the official page. Not reviewed by a person; the Finnish wording is authoritative."

# A public page is evidence of what was visible at a time, not evidence of stock or sales.  Keeping
# this schema deliberately narrow stops an extraction model from making that leap for us.
PUBLIC_PAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "state": {"type": ["string", "null"], "enum": ["active_listing", "not_purchasable", "recall_message", "removed", "unverified", None],
                  "description": "Visible page state only: active_listing has normal price/order/add-to-cart language; not_purchasable shows the product but says it is not available, sold out or out of the assortment; recall_message has a recall/withdrawal notice; removed is a product-not-found or removed page; unverified when none can be supported."},
        "evidence": {"type": ["string", "null"], "description": "One short English sentence describing only the visible words or page state that support the classification."},
        "page_title": {"type": ["string", "null"], "description": "The visible page title, in the page's original language."},
        "excerpt": {"type": ["string", "null"], "description": "Up to 200 characters copied word for word from the page, in its original language, that show the state. Copy one continuous passage; never join text from different places. Do not translate or paraphrase."},
    },
}


PAGE_STATES = {"active_listing", "not_purchasable", "recall_message", "removed", "unverified"}


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
        full = self.scrape(item["url"], {"formats": {"json": True, "images": True},
                                          "jsonParams": {"schema": NOTICE_SCHEMA, "instructions": NOTICE_INSTRUCTIONS}})
        out = full.get("json") or {}
        if not out.get("success"):
            raise RuntimeError(f"Context.dev did not read {item['url']}: {out.get('error_code')} {out.get('message')}")
        images = notice_images((full.get("images") or {}).get("data") or [])
        d = out.get("data") or {}
        products = [p for p in d.get("products") or [] if isinstance(p, dict)]
        stated = d.get("published_date") or ""
        date = item.get("date") or (stated if re.fullmatch(r"\d{4}-\d{2}-\d{2}", stated) else iso(now)[:10])
        # A notice counts from the moment this system reads it, also before 09:00 on its date.
        published_at = min(parse_ts(date + "T09:00:00+02:00", now), now)
        return Notice(
            id=notice_id(item["url"]), url=item["url"], title=item.get("title") or d.get("title_fi") or "", date=date,
            published_at=published_at, detected_at=now, source=domain(item["url"]),
            official=official, company=d.get("company") or "", brand=d.get("brand") or "",
            products=[p.get("name") or "" for p in products], eans=[p.get("ean") or "" for p in products],
            lot=d.get("lot") or "", hazard=d.get("hazard") or "other", allergens=d.get("allergens") or [],
            summary=d.get("summary") or item.get("title") or "", title_en=d.get("title_en") or "", images=images,
            extracted_by="context.dev",
            extract_ms=int((time.time() - t0) * 1000))

    def observe_public_page(self, url: str, product: str, gtin: str = "") -> dict:
        """Classify one supplied public page against an official product identity.

        This is intentionally invoked only for an explicit URL in this prototype.  A production
        rollout would populate those URLs from partner-approved monitor targets instead of crawling
        arbitrary pages.  The result never says that a recalled pack was in stock or sold.
        """
        identity = f"Product: {product}." + (f" GTIN: {gtin}." if gtin else "")
        instructions = (
            "Inspect this public product, retailer, or manufacturer page for this official recall identity. "
            f"{identity} Use only text visibly present on this URL. Classify active_listing only when normal "
            "price, order, add-to-cart, or availability language is visible for the identified product. "
            "Classify not_purchasable when the product is shown but marked unavailable, sold out or out of the assortment. "
            "Classify recall_message only when a recall/withdrawal statement is visible. Classify removed only "
            "for a clear product-not-found/removed result. Otherwise use unverified. Do not infer stock, sales, "
            "or an affected lot. Evidence must describe what the page visibly says, in English."
        )
        t0 = time.time()
        # maxAgeMs 0: an observation must be a fresh read, never Context.dev's cached copy of the page.
        full = self.scrape(url, {"formats": {"json": True, "markdown": True, "product": True, "screenshot": True},
                                 "screenshotParams": {"format": "jpeg"}, "maxAgeMs": 0,
                                 # A cookie dialog over the product would make the screenshot useless as evidence.
                                 "sharedParams": {"dismissPopups": True},
                                 "jsonParams": {"schema": PUBLIC_PAGE_SCHEMA, "instructions": instructions}})
        out = full.get("json") or {}
        if not out.get("success"):
            raise RuntimeError(f"Context.dev did not read {url}: {out.get('error_code')} {out.get('message')}")
        data = out.get("data") or {}
        state = data.get("state") if data.get("state") in PAGE_STATES else "unverified"
        prod = ((full.get("product") or {}).get("data") or {})
        p = prod.get("product") or {} if prod.get("isProductPage") else {}
        shot = (full.get("screenshot") or {}).get("data") or ""
        return {"state": state, "evidence": data.get("evidence") or "The page could not be classified from visible text.",
                "page_title": data.get("page_title") or (full.get("metadata") or {}).get("title") or domain(url),
                "excerpt": data.get("excerpt") or "",
                "markdown": (full.get("markdown") or {}).get("data") or "",
                "screenshot_jpeg_b64": shot.split(",", 1)[1] if shot.startswith("data:image/jpeg;base64,") else "",
                "product": {"name": p.get("name") or "", "sku": str(p.get("sku") or ""), "price": p.get("price"),
                            "image": p.get("imageUrl") or "", "brand": p.get("brand") or ""},
                "request_id": full.get("request_id") or "",
                "cache": (full.get("cache_metadata") or {}).get("status") or "",
                "credits": (full.get("key_metadata") or {}).get("credits_consumed"),
                "extract_ms": int((time.time() - t0) * 1000)}

    def monitor_runs(self, monitor_id: str, limit: int = 6) -> list[dict]:
        """The latest runs of one monitor: when each ran and whether it saw a change."""
        r = self.http.get(f"/monitors/{monitor_id}/runs", params={"limit": limit})
        r.raise_for_status()
        return [{k: x.get(k) for k in ("id", "status", "run_type", "started_at", "completed_at", "change_detected", "change_id", "skip_reason", "credits_charged")}
                for x in r.json().get("data", [])]

    def run_monitor(self, monitor_id: str) -> str:
        """Queue one monitor run now, outside its schedule. Returns the run id (docs.context.dev/monitors/schedules)."""
        r = self.http.post(f"/monitors/{monitor_id}/run")
        r.raise_for_status()
        return r.json().get("run_id") or ""

    def monitor(self, monitor_id: str) -> dict:
        """One monitor: status, last_run_at, next_run_at, last_change_at, last_error (docs.context.dev/monitors/overview)."""
        r = self.http.get(f"/monitors/{monitor_id}")
        r.raise_for_status()
        m = r.json()
        return {k: m.get(k) for k in ("id", "status", "last_run_at", "next_run_at", "last_change_at", "last_error", "created_at")}

    def search(self, query: str, include_domains: list[str], limit: int = 10) -> list[dict]:
        """Ranked pages for a query, only from the given domains (docs.context.dev/search/overview)."""
        r = self.http.post("/web/search", json={"query": query, "numResults": limit, "includeDomains": include_domains})
        r.raise_for_status()
        return [{"url": x.get("url") or "", "title": x.get("title") or "", "relevance": x.get("relevance") or ""}
                for x in r.json().get("results", []) if x.get("url")]

    def create_page_monitor(self, name: str, url: str, product: str) -> dict:
        """A page monitor that reports a change of the product's purchase or recall state."""
        instructions = (f"Report when the page changes how it offers {product}: price or add-to-cart appears or disappears, "
                        "it is marked unavailable or out of the assortment, the product page is removed, or a recall or "
                        "withdrawal notice appears. Ignore other products, prices of other items, ads, menus and cookie text.")
        return self.create_monitor({"name": name, "kind": "page",
                                    "target": {"type": "page", "url": url, "instructions": instructions}})

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


def notice_images(images: list[dict]) -> list[dict]:
    """The product photos that the Food Authority attaches to a recall notice, not its logos and icons."""
    out = []
    for i in images:
        u = i.get("url") or ""
        if u.startswith("https://") and "/globalassets/" in u and "takaisinved" in u and not u.endswith(".svg"):
            out.append({"url": u, "alt": i.get("alt") or ""})
    return out[:6]


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
