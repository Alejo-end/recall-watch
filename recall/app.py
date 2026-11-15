"""Recall Watch: which public product pages have not caught up with an official food recall.

    uv run uvicorn recall.app:app --host 0.0.0.0 --port 8030

With CONTEXT_DEV_API_KEY in .env, the live loop reads the Food Authority's list, re-reads approved
public pages when their Context.dev monitors report a change, and keeps an evidence receipt of each
read. Without a key, the screen shows the stored evidence, read-only.

The routes under /afterimage/ serve the screen. "Afterimage" is the name of the timeline: what
each public page still showed after the notice. The routes under /proof/ serve the controlled live
proof, a team-owned test page (recall/proof.py), stored apart from the real evidence.

The first prototype was a kitchen shelf screen. Its routes (/state, /blocked, /orders/check, /ack,
/audit, /imports/stock) stay for its regression tests, hidden from the API schema. Its poller,
which spends Context.dev credits, runs only with KITCHEN=1.
"""

from __future__ import annotations

import csv
import hmac
import io
import json
import logging
import os
import statistics
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .afterimage import AfterimageReplay
from .afterimage_live import LiveCases, Refused, fmt_hel
from .cases import STATE_LABEL
from .identity import BASIS_LABEL as IDENTITY_LABEL
from .workflow import OUTCOMES, dur, owner_message
from .cases import CaseStore
from .evidence import restore_if_missing
from .config import ROOT, load_env, load_shelf, load_watch
from .contextdev import ContextDev, domain, notice_id, parse_ts, verify_webhook
from .engine import LEVEL_NAMES, QUARANTINE, Board, Product, match, match_evidence
from .importer import products_from_csv
from .proof import Proof, proof_config
from .replay import Replay

load_env()
CFG = load_watch()
SHELF, SHELF_NAME = load_shelf(CFG)
KITCHEN = os.environ.get("KITCHEN") == "1"   # the first prototype's poller; off unless asked for
SOURCE = os.environ.get("SOURCE") or ("live" if os.environ.get("CONTEXT_DEV_API_KEY") else "replay")
POLL_S = float(os.environ.get("POLL_S", "1" if SOURCE == "replay" else "120"))
DB = os.environ.get("RECALL_DB") or (":memory:" if SOURCE == "replay" else str(ROOT / "data" / "recall.sqlite"))
if DB != ":memory:" and os.path.dirname(DB):   # a bare file name has no folder to create
    os.makedirs(os.path.dirname(DB), exist_ok=True)

board = Board(SHELF, active_days=CFG.get("active_days", 30), db=DB)
# The kitchen board of the first prototype. Recall Watch uses it only to look up a notice's
# headline when the kitchen poller runs (KITCHEN=1). Its routes stay for the regression tests.
afterimage = AfterimageReplay()
AFTERIMAGE_CFG = CFG.get("afterimage", {})
AFTERIMAGE_DB = os.environ.get("AFTERIMAGE_DB") or str(ROOT / "data" / "afterimage.sqlite")
if AFTERIMAGE_DB != ":memory:":
    os.makedirs(os.path.dirname(AFTERIMAGE_DB), exist_ok=True)
    restore_if_missing(AFTERIMAGE_DB)   # a fresh checkout shows the committed evidence, not an empty screen
live = LiveCases(CaseStore(AFTERIMAGE_DB), AFTERIMAGE_CFG,
                 known_notice=lambda url: board.notices.get(notice_id(url)), list_url=CFG["list"]["url"],
                 list_monitor_name=next((m["name"] for m in CFG["monitors"] if m["kind"] == "list"), ""))
# The controlled live proof: its own database, its own guard rails, never mixed with the real cases.
PROOF_DB = os.environ.get("PROOF_DB") or str(ROOT / "data" / "proof.sqlite")
proof = Proof(LiveCases(CaseStore(PROOF_DB, artifact_dir=None if PROOF_DB == ":memory:" else str(ROOT / "data" / "proof-artifacts")),
                        proof_config(AFTERIMAGE_CFG)),
              os.environ.get("PROOF_STATE") or ROOT / "data" / "proof.json")
WRITE_KEY = os.environ.get("AFTERIMAGE_WRITE_KEY", "")
LIVE_POLL_S = float(os.environ.get("AFTERIMAGE_POLL_S", "120"))
WORKFLOW_VOCAB = {"outcomes": OUTCOMES, "state_labels": STATE_LABEL,
                  "owner_reply_hours": float(AFTERIMAGE_CFG.get("owner_reply_hours", 24))}
LIVE_WAKE = threading.Event()   # a webhook for one of the page monitors sets it
KINDS: dict[str, str] = {}   # Context.dev monitor id to kind, filled by the poller
WAKE = threading.Event()     # a webhook sets it, and the poller then runs at once
status = {"source": SOURCE, "poll_s": POLL_S, "last_ok": None, "last_error": None, "started": time.time(),
          "shelf": SHELF_NAME, "stock_source": SHELF_NAME, "news": [], "unread": [],
          "calls": {"list": 0, "notice": 0, "news": 0}}


class ReplaySource:
    """Adds each notice of the snapshot when the replay's own time reaches its date."""

    def __init__(self, real_now: float):
        # Start just before a checked official EAN match. The visible replay becomes useful in
        # seconds, rather than making a judge wait through weeks of no-op history.
        first = os.environ.get("REPLAY_FROM", "2026-02-09")
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

        Recall pages can add lot numbers or correct a product after first publication. When the list
        monitor reports a change, the newest few notices are read again as well. `Board.add()` keeps
        a changed reading as a revision and ignores an unchanged one.
        """
        items = self.client.recall_list(CFG["list"]["url"])
        status["calls"]["list"] += 1
        self.next_list = now + CFG["list"]["recheck_minutes"] * 60
        day = datetime.fromtimestamp(now, timezone.utc)
        first = min(f"{day:%Y}-01-01", f"{day - timedelta(days=board.active_days):%Y-%m-%d}")
        current = [i for i in items if i["date"] >= first]
        if monitor_detected_at is not None:
            probe = current[:CFG["list"].get("revision_probe_max", 10)]
            # Re-read the newest few for revisions, and every notice not on the board yet, so a burst
            # of new notices after downtime does not wait for the next scheduled sync.
            candidates = probe + [i for i in current[len(probe):] if not board.has(notice_id(i["url"]))]
        else:
            candidates = [i for i in current if not board.has(notice_id(i["url"]))]
        unread = []
        for item in candidates[:CFG["list"].get("backfill_max", 40)]:
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
            # A notice already on the board is read again: the change can be a revision of it.
            if not url or domain(url) == official:
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


def seed_live_case(now: float) -> None:
    """Open the configured pilot case once, with its approved pages. Later starts reuse the stored case."""
    seed = AFTERIMAGE_CFG.get("seed_case")
    if not seed:
        return
    case = live.open_case(seed["notice_url"], "Pilot configuration", now)   # reuses a stored case
    for t in seed.get("targets", []):
        detail = t.get("found", "Listed in watch.yaml by the team")
        try:   # a stored page is reused without a scrape
            target = live.add_target(case["id"], t["url"], t.get("publisher", ""), t.get("page_type", ""), "Pilot configuration",
                                     time.time(), "pilot_configuration", detail)
            live.store.set_origin(target["id"], "pilot_configuration", detail)
        except Refused as e:
            live.status["last_error"] = str(e)


def run_live_poller() -> None:
    """The Recall Watch live loop: monitor changes trigger re-scrapes; quiet pages are re-read on a schedule."""
    seeded = False
    while True:
        now = time.time()
        try:
            if not seeded:
                seed_live_case(now)
                seeded = True
            live.poll(now)
            live.status["last_error"] = None
            if proof.target():
                proof.live.poll(now)    # the test page's own monitor, in its own store
        except Exception as e:
            logging.getLogger("recall.afterimage").exception("live poll failed")
            live.status["last_error"] = type(e).__name__
        LIVE_WAKE.wait(LIVE_POLL_S)
        LIVE_WAKE.clear()


@asynccontextmanager
async def lifespan(_app):
    if os.environ.get("RECALL_NO_POLL") != "1":
        if KITCHEN:
            threading.Thread(target=run_poller, daemon=True).start()
        if live.configured and os.environ.get("AFTERIMAGE_LIVE", "1") != "0":
            threading.Thread(target=run_live_poller, daemon=True).start()
    yield


app = FastAPI(title="Recall Watch", lifespan=lifespan)
LEGACY = {"include_in_schema": False}   # routes of the first prototype, kept for its regression tests


@app.get("/")
def page() -> FileResponse:
    return FileResponse(ROOT / "web" / "index.html")


# Fonts and the QR library are served by the app itself: the screen makes no third-party request.
app.mount("/fonts", StaticFiles(directory=ROOT / "web" / "fonts"), name="fonts")
app.mount("/vendor", StaticFiles(directory=ROOT / "web" / "vendor"), name="vendor")
app.mount("/brand", StaticFiles(directory=ROOT / "web" / "brand"), name="brand")   # the Context.dev logo, from its brand record


@app.get("/fixtures/stock-demo.csv", **LEGACY)
def demo_stock() -> FileResponse:
    """A labelled import fixture for the visible demo; production stock enters through the same import route."""
    return FileResponse(ROOT / "fixtures" / "stock-demo.csv", media_type="text/csv")


@app.get("/state", **LEGACY)
def state() -> dict:
    real = time.time()
    s = board.state(clock())
    stale = status["last_ok"] is None or real - status["last_ok"] > max(3 * POLL_S, 10)
    return {**s, "kitchen": SHELF_NAME, "status": {**status, "stale": stale}}


def lan_address() -> str:
    """This machine's address on the local network: the one other devices use to open the screen."""
    import socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("192.0.2.1", 80))   # a documentation address: nothing is sent, it only picks the interface
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def wifi_address() -> str:
    """The address on the Wi-Fi interface. A VPN can own the default route, so ask the Wi-Fi port first (macOS)."""
    import subprocess
    for port in ("en0", "en1"):
        try:
            ip = subprocess.run(["ipconfig", "getifaddr", port], capture_output=True, text=True, timeout=2).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            ip = ""
        if ip:
            return ip
    return lan_address()


@app.get("/share")
def share(request: Request) -> dict:
    """What a visitor needs to open this screen on a phone: the Wi-Fi network, its password and the address.

    The network name and password come from SHARE_WIFI_SSID and SHARE_WIFI_PASSWORD in .env. They are
    shown on the screen on purpose, for people in the room. Leave them empty to show only the address.
    """
    port = os.environ.get("SHARE_PORT", "8030")
    return {"ssid": os.environ.get("SHARE_WIFI_SSID", ""), "password": os.environ.get("SHARE_WIFI_PASSWORD", ""),
            "security": os.environ.get("SHARE_WIFI_SECURITY", "WPA"),
            "url": os.environ.get("SHARE_URL") or share_url(request, port)}


def share_url(request: Request, port: str) -> str:
    """The Mac's current Wi-Fi address, read on every request.

    When the Mac moves to another network, the card shows the new address at once, even in a page
    that was opened under the old address. The server listens on every interface, so both work.
    """
    return f"http://{wifi_address()}:{port}/"


@app.get("/afterimage")
def afterimage_index() -> dict:
    """The two modes of the board. They never share a case, a row or a chart."""
    return {"modes": {"live": "/afterimage/live", "demo": "/afterimage/demo"}, "live_configured": live.configured}


@app.get("/afterimage/demo")
def afterimage_demo() -> dict:
    """Simulated pages on a simulated clock. Labelled on every response."""
    return {**afterimage.state(), **WORKFLOW_VOCAB}


@app.get("/afterimage/live")
def afterimage_live(case: str = "") -> dict:
    """The persisted live case: observations that Context.dev made of approved public pages."""
    now = time.time()
    cases = live.store.cases()
    # Without a choice, show the newest case that already watches pages.
    # Without a choice: the featured case from watch.yaml, else the newest case that watches pages.
    featured = AFTERIMAGE_CFG.get("featured_notice_url", "")
    cases.sort(key=lambda c: c["notice_url"] != featured)
    watched = [c for c in cases if live.store.targets(c["id"])]
    chosen = case or ((watched or cases)[0]["id"] if cases else "")
    view = live.store.view(chosen, now) if chosen and live.store.case(chosen) else None
    return {"mode": "live", "cases": [{"id": c["id"], "title_fi": c["title_fi"], "title_en": c["title_en"],
                                       "notice_date": c["notice_date"], "pages": len(live.store.targets(c["id"])),
                                       "opened_by": c["opened_by"]} for c in cases],
            "view": view, "suggestions": live.ranked_suggestions(chosen) if view else [],
            "pilot": live.summary(now), "write_requires_key": bool(WRITE_KEY),
            "writes_local_only": not WRITE_KEY, "now": now,
            "monitors": live.monitor_status, "impact": impact(now), "featured_notice_url": featured, **WORKFLOW_VOCAB}


def impact(now: float) -> dict:
    """The pilot measures, over every live case: what a pilot would report, and where this one stands today."""
    views = [live.store.view(c["id"], now) for c in live.store.cases()]
    lags = [r["milestones"]["purchase_closed"]["lag_s"] for v in views for r in v["targets"]
            if (r["milestones"] or {}).get("purchase_closed") and not r["milestones"]["purchase_closed"]["seen_before_watch"]]
    return {"domains": len(live.domains), "cases": len(views), "pages": sum(v["summary"]["targets"] for v in views),
            "auto_opened": sum(1 for c in live.store.cases() if c["opened_by"] == "Recall Watch list check"),
            "list_monitor_minutes": float(AFTERIMAGE_CFG.get("monitor_interval_minutes", 10)),
            "sla_hours": float(AFTERIMAGE_CFG.get("sla_hours", 4)),
            "median_lag_s": statistics.median(lags) if lags else None, "measured_lags": len(lags),
            "corrections": sum(v["summary"]["reviewer_corrections"] for v in views),
            "resolutions": sum(v["summary"]["resolved"] for v in views)}


LOOPBACK = {"127.0.0.1", "::1"}


def require_writer(request: Request) -> None:
    """Writes spend Context.dev credits or change the audit trail, so they are never open to the network.

    With AFTERIMAGE_WRITE_KEY set, every write needs that key. Without it, only this machine can write:
    a request from any other address is refused, whatever host the server listens on.
    """
    if WRITE_KEY:
        if not hmac.compare_digest(request.headers.get("X-Afterimage-Key", ""), WRITE_KEY):
            raise HTTPException(401, "This action needs the operator key.")
        return
    if (request.client.host if request.client else "") not in LOOPBACK:
        raise HTTPException(403, "Changes are allowed only on this computer. Set AFTERIMAGE_WRITE_KEY to allow them from the network.")


def refused(e: Refused) -> HTTPException:
    return HTTPException(e.status, str(e))


class CaseOpen(BaseModel):
    notice_url: str
    who: str = ""


class TargetAdd(BaseModel):
    url: str
    publisher: str = ""
    page_type: str = ""
    who: str = ""


class TargetAction(BaseModel):
    who: str
    action: str           # owner_contacted, resolved, reopened or note
    note: str = ""
    outcome: str = ""     # for resolved: see recall/workflow.py OUTCOMES
    recipient: str = ""   # for owner_contacted: who was contacted
    due_hours: float | None = None   # for owner_contacted: when a reply is due
    corrected_state: str = ""        # for the outcome classification_wrong


@app.post("/afterimage/live/cases", dependencies=[Depends(require_writer)])
def open_live_case(body: CaseOpen) -> dict:
    try:
        return {"ok": True, "case": live.open_case(body.notice_url.strip(), body.who, time.time())}
    except Refused as e:
        raise refused(e)


@app.post("/afterimage/live/cases/{case_id}/targets", dependencies=[Depends(require_writer)])
def add_live_target(case_id: str, body: TargetAdd) -> dict:
    """Approve one public page: one structured scrape now, and a Context.dev monitor from then on."""
    try:
        return {"ok": True, "target": live.add_target(case_id, body.url, body.publisher, body.page_type, body.who, time.time())}
    except Refused as e:
        raise refused(e)


@app.post("/afterimage/live/targets/{target_id}/rescrape", dependencies=[Depends(require_writer)])
def rescrape_live_target(target_id: str) -> dict:
    """Read the page now. The answer says whether the visible state changed since the last read."""
    if not live.store.target(target_id):
        raise HTTPException(404, "No such watched page")
    before = live.store.observations(target_id)
    try:
        obs = live.scrape(target_id, time.time(), "manual")
    except Refused as e:
        raise refused(e)
    prev = before[-1] if before else None
    return {"ok": True, "observation": obs, "changed": bool(prev and prev["state"] != obs["state"]),
            "previous_state": prev["state"] if prev else None, "previous_at": prev["at"] if prev else None}


def _message(case: dict, row: dict | None, when=None) -> dict:
    if not row:
        raise HTTPException(404, "No such watched page")
    return {**owner_message(case, row, float(AFTERIMAGE_CFG.get("owner_reply_hours", 24)), when),
            "to": live.store.owner_for(row["url"]) if row.get("url") else ""}


@app.get("/afterimage/live/targets/{target_id}/owner-message")
def live_owner_message(target_id: str) -> dict:
    """A draft for the person to send from their own mail. The server sends nothing."""
    t = live.store.target(target_id)
    if not t:
        raise HTTPException(404, "No such watched page")
    view = live.store.view(t["case_id"], time.time())
    return _message(view["case"], next((r for r in view["targets"] if r["id"] == target_id), None))


@app.get("/afterimage/demo/targets/{target_id}/owner-message")
def demo_owner_message(target_id: str) -> dict:
    view = afterimage.state()
    return _message(view["case"], next((r for r in view["targets"] if r["id"] == target_id), None),
                    lambda t: "simulated time T+" + dur(t - view["case"]["notice_at"]))


def build_receipt(lc: LiveCases, oid: int, base: str) -> dict:
    """Everything needed to defend one observation: what was read, when, by which call, and against which notice."""
    o = lc.store.observation(oid)
    if not o:
        raise HTTPException(404, "No such observation")
    t = lc.store.target(o["target_id"])
    c = lc.store.case(t["case_id"])
    o["product"] = json.loads(o.get("product") or "{}")
    row = next((r for r in lc.store.view(c["id"], time.time())["targets"] if r["id"] == t["id"]), {})
    return {
        "notice": {k: c[k] for k in ("notice_url", "publisher", "title_fi", "title_en", "translation_provenance", "notice_date",
                                     "notice_at", "notice_at_basis", "products", "gtins", "lot", "images", "opened_at", "opened_by")},
        "page": {k: t[k] for k in ("url", "publisher", "page_type", "added_at", "added_by", "origin", "origin_detail", "monitor_id")},
        "monitor": lc.monitor_status.get(t["monitor_id"]) if t["monitor_id"] else None,
        "observation": o,
        "identity_label": IDENTITY_LABEL.get(o.get("identity") or "", "Not computed (observation from before identity checks)"),
        # Why the page is in the queue now. A receipt of an older read says that a newer read exists.
        "current": {"latest": row.get("latest_observation_id") == oid, "state_label": row.get("state_label", ""),
                    "review": row.get("review", ""), "needs_action": row.get("needs_action", False),
                    "next_step": row.get("next_step", ""), "sla": row.get("sla", ""), "reopened": row.get("reopened", False)},
        "artifacts": {"page_text": f"{base}/observations/{oid}/page.md" if lc.store.artifact(f"{oid}.md") else None,
                      "screenshot": f"{base}/observations/{oid}/screenshot.jpg"
                      if o.get("has_screenshot") and lc.store.artifact(f"{oid}.jpg") else None,
                      # The published evidence copy keeps the hashes but not the retailers' pages themselves.
                      "withheld": lc.store.artifacts_withheld() and bool(o.get("content_sha256"))},
        "has_receipt": bool(o.get("request_id")),
        "test_page": lc is proof.live,
    }


def artifact_response(lc: LiveCases, name: str, media_type: str, missing: str) -> Response:
    data = lc.store.artifact(name)
    if data is None:
        raise HTTPException(404, missing)
    return Response(data, media_type=media_type)


@app.get("/afterimage/live/observations/{oid}/receipt")
def observation_receipt(oid: int) -> dict:
    return build_receipt(live, oid, "/afterimage/live")


@app.get("/afterimage/live/images/{name}")
def stored_image(name: str) -> Response:
    """A stored copy of a notice photo or a page's product photo, as it was when it was kept."""
    data = live.store.artifact(name)
    ext = name.rsplit(".", 1)[-1]
    if data is None or ext == "md":
        raise HTTPException(404, "No such stored image")
    return Response(data, media_type={"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp", "gif": "image/gif",
                                      "avif": "image/avif"}[ext], headers={"Cache-Control": "max-age=86400",
                                                                          "X-Content-Type-Options": "nosniff"})


@app.get("/afterimage/live/observations/{oid}/page.md")
def observation_page(oid: int) -> Response:
    return artifact_response(live, f"{oid}.md", "text/markdown; charset=utf-8", "No page text stored for this observation")


@app.get("/afterimage/live/observations/{oid}/screenshot.jpg")
def observation_screenshot(oid: int) -> Response:
    return artifact_response(live, f"{oid}.jpg", "image/jpeg", "No screenshot stored for this observation")


# The controlled live proof ----------------------------------------------------

class ProofFlip(BaseModel):
    to: str          # on_sale or recall
    who: str


@app.get("/proof")
def proof_state() -> dict:
    """The team-owned test page, its test case and the steps of the latest proof run."""
    return proof.status(time.time())


@app.post("/proof/flip", dependencies=[Depends(require_writer)])
def proof_flip(body: ProofFlip) -> dict:
    """Change the test page, then follow Context.dev from the monitor run to the new receipt."""
    try:
        return {"ok": True, "run": proof.start(body.to, body.who, time.time())}
    except Refused as e:
        raise refused(e)


@app.post("/proof/targets/{target_id}/actions", dependencies=[Depends(require_writer)])
def act_proof_target(target_id: str, body: TargetAction) -> dict:
    """The same review actions as a real page, recorded in the test case's own store."""
    try:
        return {"ok": True, "action": proof.live.store.act(target_id, body.who, body.action, body.note, time.time(),
                                                           body.outcome, body.recipient, None, body.corrected_state)}
    except KeyError:
        raise HTTPException(404, "No such test page")
    except ValueError as e:
        raise HTTPException(422, str(e))


@app.get("/proof/observations/{oid}/receipt")
def proof_receipt(oid: int) -> dict:
    return build_receipt(proof.live, oid, "/proof")


@app.get("/proof/observations/{oid}/page.md")
def proof_page(oid: int) -> Response:
    return artifact_response(proof.live, f"{oid}.md", "text/markdown; charset=utf-8", "No page text stored for this observation")


@app.get("/proof/observations/{oid}/screenshot.jpg")
def proof_screenshot(oid: int) -> Response:
    return artifact_response(proof.live, f"{oid}.jpg", "image/jpeg", "No screenshot stored for this observation")


@app.get("/afterimage/live/cases/{case_id}/report.csv")
def live_report(case_id: str) -> Response:
    """The audit trail of a case: every Context.dev observation and every person's action, with times."""
    case = live.store.case(case_id)
    if not case:
        raise HTTPException(404, "No such case")
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["time_utc", "time_helsinki", "page", "url", "kind", "state_or_action", "by", "outcome", "recipient", "text",
                "identity_basis", "verbatim_excerpt", "context_dev_request_id", "page_text_sha256"])
    for r in live.store.report_rows(case_id):
        w.writerow([datetime.fromtimestamp(r["at"], timezone.utc).isoformat(timespec="seconds"), fmt_hel(r["at"]), r["page"],
                    r["url"], r["kind"], r["detail"], r["by"], r["outcome"], r["recipient"], r["text"], r["identity"],
                    r["excerpt"], r["request_id"], r["content_sha256"]])
    name = "recall-watch-" + case_id + ".csv"
    return Response(out.getvalue(), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.post("/afterimage/live/targets/{target_id}/actions", dependencies=[Depends(require_writer)])
def act_live_target(target_id: str, body: TargetAction) -> dict:
    try:
        now = time.time()
        return {"ok": True, "action": live.store.act(target_id, body.who, body.action, body.note, now, body.outcome, body.recipient,
                                                     now + body.due_hours * 3600 if body.due_hours else None, body.corrected_state)}
    except KeyError:
        raise HTTPException(404, "No such watched page")
    except ValueError as e:
        raise HTTPException(422, str(e))


@app.post("/afterimage/demo/targets/{target_id}/actions")
def act_demo_target(target_id: str, body: TargetAction) -> dict:
    try:
        return {"ok": True, "action": afterimage.act(target_id, body.who, body.action, body.note, outcome=body.outcome,
                                                     recipient=body.recipient, due_hours=body.due_hours,
                                                     corrected_state=body.corrected_state)}
    except KeyError:
        raise HTTPException(404, "No such demo page")
    except ValueError as e:
        raise HTTPException(422, str(e))


@app.get("/health")
def health(response: Response) -> dict:
    """Machine-readable monitoring status for an external uptime check.

    An unattended system needs a route that can be watched when nobody has the screen open. The
    kitchen poller of the first prototype counts only when it runs (KITCHEN=1).
    """
    real = time.time()
    stale = KITCHEN and (status["last_ok"] is None or real - status["last_ok"] > max(3 * POLL_S, 10))
    # The Recall Watch live loop is checked separately: it can fail while the kitchen board is fine.
    live_poll = live.status["last_poll"]
    live_stale = live.configured and (live_poll is None or real - live_poll > 3 * LIVE_POLL_S)
    if stale or live_stale:
        response.status_code = 503
    return {"ok": not (stale or live_stale), "source": SOURCE, "last_ok": status["last_ok"], "last_error": status["last_error"],
            "afterimage": {"configured": live.configured, "last_poll": live_poll, "stale": live_stale,
                           "last_error": live.status["last_error"]}}


@app.get("/blocked", **LEGACY)
def blocked() -> dict:
    """Every open "quarantine" hit on the shelf, for purchasing systems to hold for verification.

    These are the hits that put the screen at "quarantine": an official notice with the same EAN
    code. A hit stays open while it is new or held pending its lot check. `POST /orders/check`
    holds the same matches.
    """
    now = clock()
    return {"as_of": now, "products": [
        {"id": h["product"], "name": h["product_name"], "ean": h["ean"], "notice": h["url"], "why": h["why"],
         "action": "hold_and_verify_lot", "stock_location": h["stock_location"], "candidate_stock": h["candidate_stock"],
         "next_service": h["next_service"], "open_order": h["open_order"]} for h in board.blocked(now)]}


class OrderItem(BaseModel):
    """The fields a purchasing system usually has before creating an order."""
    id: str = ""
    name: str
    brand: str = ""
    ean: str = ""
    supplier: str = ""


class OrderCheck(BaseModel):
    items: list[OrderItem]


@app.post("/orders/check", **LEGACY)
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
    action: str   # held, confirmed_affected, released_not_this_lot or not_on_shelf
    who: str
    lot: str = ""
    expiry: str = ""
    note: str = ""


class StockImport(BaseModel):
    csv: str


@app.post("/ack", dependencies=[Depends(require_writer)], **LEGACY)
def ack(body: Ack) -> dict:
    """Append a kitchen event. A hold remains open until a lot-based outcome closes it."""
    try:
        board.record_event(body.notice, body.product, body.action, body.who, clock(),
                           lot=body.lot, expiry=body.expiry, note=body.note)
    except KeyError:
        raise HTTPException(404, "No such hit")
    except ValueError as e:
        raise HTTPException(422, str(e))
    return {"ok": True}


@app.get("/audit", **LEGACY)
@app.get("/audit/{notice:path}/{product}", **LEGACY)
def audit(notice: str, product: str) -> dict:
    """Return the append-only chain of custody for a visible candidate.

    A notice id is domain plus path, so it contains slashes. The path form takes the last segment as
    the product; `/audit?notice=...&product=...` works too.
    """
    rows = board.db.execute(
        "SELECT event_id, action, who, lot, expiry, note, at FROM events WHERE notice_id = ? AND product_id = ? ORDER BY event_id",
        (notice, product),
    ).fetchall()
    return {"notice": notice, "product": product, "events": [
        {"id": r[0], "action": r[1], "who": r[2], "lot": r[3], "expiry": r[4], "note": r[5], "at": r[6]}
        for r in rows
    ]}


@app.post("/imports/stock", dependencies=[Depends(require_writer)], **LEGACY)
def import_stock(body: StockImport) -> dict:
    """Replace the demo stock feed and recompute all current candidates immediately."""
    try:
        products = products_from_csv(body.csv)
        kept = board.replace_shelf(products)
        status["stock_source"] = "CSV import"
    except ValueError as e:
        raise HTTPException(422, str(e))
    return {"ok": True, "products": len(products), "kept_on_hold": kept, "recomputed_notices": len(board.notices)}


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
    monitor = (change or {}).get("monitor_id") if isinstance(event, dict) and event.get("event") == "change.detected" else None
    if monitor in KINDS:
        WAKE.set()
    pages = {**live.monitor_ids(), **proof.live.monitor_ids()}
    if monitor and monitor in pages:
        LIVE_WAKE.set()   # an approved public page changed: re-scrape it now, not at the next poll
    return {"ok": True, "used": bool(monitor in KINDS or (monitor and monitor in pages))}
