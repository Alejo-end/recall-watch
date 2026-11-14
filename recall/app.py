"""Recall Watch: a kitchen screen that answers "is anything on my shelf recalled right now?"

    uv run uvicorn recall.app:app --host 0.0.0.0 --port 8030

SOURCE=live reads Context.dev (needs CONTEXT_DEV_API_KEY). SOURCE=replay plays the real notices
of 2026 from a snapshot. With SOURCE unset, live is used when a key is set, else the replay.
"""

from __future__ import annotations

import json
import os
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .config import ROOT, load_env, load_shelf, load_watch
from .contextdev import ContextDev, domain, notice_id, parse_ts, verify_webhook
from .engine import LEVEL_NAMES, QUARANTINE, Board, Product, match, match_evidence
from .replay import Replay

load_env()
CFG = load_watch()
SHELF, SHELF_NAME = load_shelf(CFG)
SOURCE = os.environ.get("SOURCE") or ("live" if os.environ.get("CONTEXT_DEV_API_KEY") else "replay")
POLL_S = float(os.environ.get("POLL_S", "1" if SOURCE == "replay" else "120"))
DB = os.environ.get("RECALL_DB") or (":memory:" if SOURCE == "replay" else str(ROOT / "data" / "recall.sqlite"))
if DB != ":memory:" and os.path.dirname(DB):   # a bare file name has no folder to create
    os.makedirs(os.path.dirname(DB), exist_ok=True)

board = Board(SHELF, active_days=CFG.get("active_days", 30), db=DB)
KINDS: dict[str, str] = {}   # Context.dev monitor id to kind, filled by the poller
WAKE = threading.Event()     # a webhook sets it, and the poller then runs at once
status = {"source": SOURCE, "poll_s": POLL_S, "last_ok": None, "last_error": None, "started": time.time(),
          "shelf": SHELF_NAME, "news": [], "unread": [], "calls": {"list": 0, "notice": 0, "news": 0}}


class ReplaySource:
    """Adds each notice of the snapshot when the replay's own time reaches its date."""

    def __init__(self, real_now: float):
        first = os.environ.get("REPLAY_FROM", "2026-01-01")
        self.replay = Replay(real_now, day_s=float(os.environ.get("REPLAY_DAY_S", "2")), first_day=first)
        self.cycle = 0
        status["about"] = {**self.replay.about, "day_s": self.replay.day_s}

    def now(self, real_now: float) -> float:
        return self.replay.now(real_now)[0]

    def poll(self, real_now: float) -> None:
        t, cycle = self.replay.now(real_now)
        if cycle != self.cycle:
            self.cycle = cycle
            board.clear()
        for n in self.replay.notices:
            if n.published_at <= t:
                board.add(n)


class LiveSource:
    """Reads the authority's list, the changes of our Context.dev monitors and the news."""

    def __init__(self, real_now: float, client: ContextDev | None = None):
        self.client = client or ContextDev()
        self.kinds = KINDS
        self.since = real_now - 6 * 3600     # on start, read the changes of the last six hours
        self.seen: set[str] = set()          # change ids already handled; `since` is inclusive
        self.next_list = 0.0
        self.next_news = 0.0

    def now(self, real_now: float) -> float:
        return real_now

    def poll(self, now: float) -> None:
        if not self.kinds:
            by_name = {m["name"]: m["kind"] for m in CFG["monitors"]}
            self.kinds.update({m["id"]: by_name[m["name"]] for m in self.client.monitors() if m["name"] in by_name})
            if not self.kinds:
                raise RuntimeError("No Recall Watch monitors exist. Run: uv run python -m recall.setup_monitors --create")
        changes = [c for c in self.client.changes(self.since, self.kinds) if c["id"] not in self.seen]
        list_change = next((c for c in changes if c["kind"] == "list"), None)
        if list_change or now >= self.next_list:
            self.sync_list(now, parse_ts(list_change.get("detected_at"), now) if list_change else None)
        # A change is seen only after its handling ended. A change that fails stays unseen, `since`
        # stays behind it, and the next poll reads it again.
        for c in changes:
            if c["kind"] == "web":
                self.read_web_change(c, now)
            self.seen.add(c["id"])
        if changes:
            self.since = max(parse_ts(c.get("detected_at"), now) for c in changes)
        if now >= self.next_news:
            status["news"] = self.client.news(CFG["news"]["query"], since=now - 7 * 86400)
            status["calls"]["news"] += 1
            self.next_news = now + CFG["news"]["news_minutes"] * 60

    def sync_list(self, now: float, monitor_detected_at: float | None) -> None:
        """Read the list, then read each notice that the database does not hold.

        It reads the notices of this year, and the notices that still count: a notice counts for
        `active_days` after its date, also across the turn of the year. A notice that fails to read
        does not stop the others. `status["unread"]` lists it, and the next list check reads it again.
        """
        items = self.client.recall_list(CFG["list"]["url"])
        status["calls"]["list"] += 1
        self.next_list = now + CFG["list"]["recheck_minutes"] * 60
        day = datetime.fromtimestamp(now, timezone.utc)
        first = min(f"{day:%Y}-01-01", f"{day - timedelta(days=board.active_days):%Y-%m-%d}")
        new = [i for i in items if i["date"] >= first and not board.has(notice_id(i["url"]))]
        unread = []
        for item in new[:CFG["list"].get("backfill_max", 40)]:
            status["calls"]["notice"] += 1
            try:
                n = self.client.read_notice(item, now)
            except Exception as e:
                unread.append({"url": item["url"], "error": f"{type(e).__name__}: {e}"[:300]})
                continue
            n.monitor_detected_at = monitor_detected_at
            board.add(n)
        status["unread"] = unread

    def read_web_change(self, c: dict, now: float) -> None:
        """A recall found elsewhere on the web. It is not from the authority, so it gives "check" at most."""
        official = domain(CFG["list"]["url"])
        for url in (c.get("matched_urls") or [c.get("url")])[:3]:
            if not url or domain(url) == official or board.has(notice_id(url)):
                continue
            n = self.client.read_notice({"url": url, "title": c.get("title") or ""}, now, official=False)
            status["calls"]["notice"] += 1
            n.monitor_detected_at = parse_ts(c.get("detected_at"), now)
            board.add(n)


source = None


def clock() -> float:
    return source.now(time.time()) if source else time.time()


def run_poller() -> None:
    global source
    while True:
        now = time.time()
        try:
            source = source or (ReplaySource(now) if SOURCE == "replay" else LiveSource(now))
            source.poll(now)
            board.state(source.now(now))   # records each level change in the history, also with no viewer
            status["last_ok"], status["last_error"] = now, None
        except Exception as e:  # keep the screen up; it shows that the data is stale
            status["last_error"] = f"{type(e).__name__}: {e}"[:300]
        WAKE.wait(POLL_S)
        WAKE.clear()


@asynccontextmanager
async def lifespan(_app):
    if os.environ.get("RECALL_NO_POLL") != "1":
        threading.Thread(target=run_poller, daemon=True).start()
    yield


app = FastAPI(title="Recall Watch", lifespan=lifespan)


@app.get("/")
def page() -> FileResponse:
    return FileResponse(ROOT / "web" / "index.html")


@app.get("/state")
def state() -> dict:
    real = time.time()
    s = board.state(clock())
    stale = status["last_ok"] is None or real - status["last_ok"] > max(3 * POLL_S, 10)
    return {**s, "kitchen": SHELF_NAME, "status": {**status, "stale": stale}}


@app.get("/blocked")
def blocked() -> dict:
    """Every open "quarantine" hit on the shelf, for purchasing systems to hold for verification.

    These are the hits that put the screen at "quarantine": an official notice with the same EAN
    code, or with the same brand and name. `POST /orders/check` holds the same matches.
    """
    now = clock()
    return {"as_of": now, "products": [
        {"id": h["product"], "name": h["product_name"], "ean": h["ean"], "notice": h["url"], "why": h["why"],
         "action": "hold_and_verify_lot"} for h in board.blocked(now)]}


class OrderItem(BaseModel):
    """The fields a purchasing system usually has before creating an order."""
    id: str = ""
    name: str
    brand: str = ""
    ean: str = ""
    supplier: str = ""


class OrderCheck(BaseModel):
    items: list[OrderItem]


@app.post("/orders/check")
def check_order(body: OrderCheck) -> dict:
    """Return an allow, review, or hold decision before an ordering system submits an order.

    A match at the level "quarantine" gives `hold_and_verify_lot`, the same matches that `GET /blocked`
    lists for the shelf. A match at the level "check" gives `review`.

    A recall normally applies to specific lots. `hold` therefore stops an order only until a person
    checks the supplier's lot and expiry details. It is not a claim that every future batch is unsafe.
    """
    now = clock()
    active = board.active(now)
    decisions = []
    for item in body.items:
        candidate = Product(id=item.id or item.ean or item.name, name=item.name, brand=item.brand,
                            ean=item.ean, supplier=item.supplier)
        evidence = []
        for notice in active:
            level, why, basis = match(notice, candidate)
            if not level:
                continue
            confidence, basis_words = match_evidence(level, basis)
            evidence.append({"notice": notice.url, "title": notice.title, "official": notice.official, "lot": notice.lot,
                             "level_name": LEVEL_NAMES[level], "confidence": confidence, "match_basis": basis_words, "why": why})
        held = any(e["level_name"] == LEVEL_NAMES[QUARANTINE] for e in evidence)
        decision = "hold_and_verify_lot" if held else "review" if evidence else "allow"
        decisions.append({"id": item.id, "name": item.name, "decision": decision, "evidence": evidence})
    return {"as_of": now, "decisions": decisions}


class Ack(BaseModel):
    notice: str
    product: str
    action: str   # quarantined, confirmed_affected, not_this_lot or not_on_shelf
    who: str


@app.post("/ack")
def ack(body: Ack) -> dict:
    """A person records a verification decision. The hit then stops counting."""
    try:
        board.ack(body.notice, body.product, body.action, body.who, clock())
    except KeyError:
        raise HTTPException(404, "No such hit")
    except ValueError as e:
        raise HTTPException(422, str(e))
    return {"ok": True}


@app.post("/webhooks/context")
async def webhook(request: Request) -> dict:
    """Optional: Context.dev pushes a change here, and the poller then runs at once instead of
    waiting. It needs an address that the internet can reach, and CONTEXT_WEBHOOK_SECRET."""
    secret = os.environ.get("CONTEXT_WEBHOOK_SECRET", "")
    raw = await request.body()
    if not verify_webhook(raw, request.headers.get("X-Context-Signature", ""), secret):
        raise HTTPException(401, "Invalid webhook signature")
    try:
        event = json.loads(raw)
    except ValueError:
        raise HTTPException(400, "Invalid JSON")
    # docs.context.dev/monitors/webhooks: change.detected carries the change record in data.change
    change = (event.get("data") or {}).get("change") if isinstance(event, dict) else None
    ours = isinstance(event, dict) and event.get("event") == "change.detected" and (change or {}).get("monitor_id") in KINDS
    if ours:
        WAKE.set()
    return {"ok": True, "used": bool(ours)}
