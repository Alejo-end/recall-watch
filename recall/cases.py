"""Recall cases: one official notice, the public pages watched for it, and what each page showed.

A case answers one question for a compliance lead: how long after the official notice did each
approved public page stop showing the product as normally purchasable, and did it show a recall
message?  Every number on the screen comes from a stored observation with its own timestamp.

Nothing here claims that a recalled pack was in stock or sold.  A page state is what the page
visibly said when Context.dev read it.

The view builder (`build_case_view`) is pure: the live store and the demo replay both feed it,
but they never share a case, so a simulated row can never appear next to a live one.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import statistics
import threading
import time

from .workflow import LEGACY, dur, fmt, review_state, validate

STATES = ("active_listing", "not_purchasable", "recall_message", "removed", "unverified")
STATE_LABEL = {
    "active_listing": "Appears purchasable",
    "not_purchasable": "Shown as not available",
    "recall_message": "Recall message visible",
    "removed": "Page removed",
    "unverified": "Could not classify",
}
# The purchase path is closed when the page no longer offers the product for sale.
PURCHASE_CLOSED = {"not_purchasable", "recall_message", "removed"}


def _squash(text: str) -> str:
    """Text compared without case, markup and spacing, to check that an excerpt is really on the page."""
    return re.sub(r"[\W_]+", "", (text or "").lower())


def target_id(case_id: str, url: str) -> str:
    return "t-" + hashlib.sha256(f"{case_id}|{url}".encode()).hexdigest()[:12]


class CaseStore:
    """SQLite store. Observations and actions are append-only."""

    def __init__(self, db: str = ":memory:", artifact_dir: str | None = None):
        self.lock = threading.RLock()
        # Page snapshots and screenshots live next to the database as files; their hashes are in it.
        self.artifact_dir = artifact_dir or (os.path.join(os.path.dirname(os.path.abspath(db)), "artifacts")
                                             if db != ":memory:" else None)
        self._mem_artifacts: dict[str, bytes] = {}
        self.db = sqlite3.connect(db, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS cases (
                id TEXT PRIMARY KEY, notice_url TEXT NOT NULL UNIQUE, publisher TEXT NOT NULL,
                title_fi TEXT NOT NULL, title_en TEXT NOT NULL DEFAULT '', translation_provenance TEXT NOT NULL DEFAULT '',
                products TEXT NOT NULL DEFAULT '[]', gtins TEXT NOT NULL DEFAULT '[]', lot TEXT NOT NULL DEFAULT '',
                notice_date TEXT NOT NULL, notice_at REAL NOT NULL, notice_at_basis TEXT NOT NULL,
                sla_hours REAL NOT NULL, opened_at REAL NOT NULL, opened_by TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS targets (
                id TEXT PRIMARY KEY, case_id TEXT NOT NULL, url TEXT NOT NULL, publisher TEXT NOT NULL,
                page_type TEXT NOT NULL, added_at REAL NOT NULL, added_by TEXT NOT NULL,
                monitor_id TEXT NOT NULL DEFAULT '', UNIQUE (case_id, url)
            );
            CREATE TABLE IF NOT EXISTS observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT, target_id TEXT NOT NULL, at REAL NOT NULL,
                state TEXT NOT NULL, evidence TEXT NOT NULL, page_title TEXT NOT NULL DEFAULT '',
                trigger TEXT NOT NULL, change_id TEXT NOT NULL DEFAULT '', window_start REAL,
                extract_ms INTEGER
            );
            CREATE TABLE IF NOT EXISTS actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT, target_id TEXT NOT NULL, at REAL NOT NULL,
                who TEXT NOT NULL, action TEXT NOT NULL, note TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS owners (
                domain TEXT PRIMARY KEY, recipient TEXT NOT NULL, updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS spend (at REAL NOT NULL, kind TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS suggestions (
                case_id TEXT NOT NULL, url TEXT NOT NULL, title TEXT NOT NULL, found_at REAL NOT NULL,
                PRIMARY KEY (case_id, url)
            );
        """)
        # Databases from earlier prototypes gain the new fields.
        migrations = {
            "actions": (("outcome", "TEXT NOT NULL DEFAULT ''"), ("recipient", "TEXT NOT NULL DEFAULT ''"),
                        ("due_at", "REAL"), ("corrected_state", "TEXT NOT NULL DEFAULT ''")),
            "cases": (("images", "TEXT NOT NULL DEFAULT '[]'"),),
            "targets": (("origin", "TEXT NOT NULL DEFAULT ''"), ("origin_detail", "TEXT NOT NULL DEFAULT ''")),
            "observations": (("raw_state", "TEXT NOT NULL DEFAULT ''"), ("identity", "TEXT NOT NULL DEFAULT ''"),
                             ("identity_detail", "TEXT NOT NULL DEFAULT ''"), ("excerpt", "TEXT NOT NULL DEFAULT ''"),
                             ("excerpt_verified", "INTEGER"), ("content_sha256", "TEXT NOT NULL DEFAULT ''"),
                             ("request_id", "TEXT NOT NULL DEFAULT ''"), ("cache", "TEXT NOT NULL DEFAULT ''"),
                             ("credits", "REAL"), ("product", "TEXT NOT NULL DEFAULT '{}'"),
                             ("has_screenshot", "INTEGER NOT NULL DEFAULT 0")),
            "suggestions": (("query", "TEXT NOT NULL DEFAULT ''"),),
        }
        for table, cols in migrations.items():
            have = {r[1] for r in self.db.execute(f"PRAGMA table_info({table})")}
            for col, ddl in cols:
                if col not in have:
                    self.db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
        self.db.commit()

    # Writes -----------------------------------------------------------------
    def open_case(self, case: dict) -> dict:
        with self.lock:
            row = self.db.execute("SELECT * FROM cases WHERE notice_url = ?", (case["notice_url"],)).fetchone()
            if row:
                return self._case(row)
            cid = "c-" + hashlib.sha256(case["notice_url"].encode()).hexdigest()[:10]
            self.db.execute(
                "INSERT INTO cases (id, notice_url, publisher, title_fi, title_en, translation_provenance, products, gtins, lot,"
                " notice_date, notice_at, notice_at_basis, sla_hours, opened_at, opened_by, images)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (cid, case["notice_url"], case.get("publisher", "Finnish Food Authority"), case["title_fi"],
                 case.get("title_en", ""), case.get("translation_provenance", ""),
                 json.dumps(case.get("products", []), ensure_ascii=False), json.dumps(case.get("gtins", [])),
                 case.get("lot", ""), case["notice_date"], case["notice_at"], case["notice_at_basis"],
                 case.get("sla_hours", 4.0), case.get("opened_at", time.time()), case.get("opened_by", "Recall Watch"),
                 json.dumps(case.get("images", []))))
            self.db.commit()
            return self.case(cid)

    def add_target(self, case_id: str, url: str, publisher: str, page_type: str, who: str, now: float,
                   origin: str = "manual", origin_detail: str = "") -> tuple[dict, bool]:
        """Returns the target and whether it is new. `origin` says how the page was found and approved."""
        with self.lock:
            tid = target_id(case_id, url)
            if self.target(tid):
                return self.target(tid), False
            self.db.execute("INSERT INTO targets (id, case_id, url, publisher, page_type, added_at, added_by, origin, origin_detail)"
                            " VALUES (?,?,?,?,?,?,?,?,?)", (tid, case_id, url, publisher, page_type, now, who, origin, origin_detail))
            self.db.commit()
            return self.target(tid), True

    def set_origin(self, tid: str, origin: str, detail: str) -> None:
        with self.lock:
            self.db.execute("UPDATE targets SET origin = ?, origin_detail = ? WHERE id = ? AND origin = ''", (origin, detail, tid))
            self.db.commit()

    def set_images(self, case_id: str, images: list[dict]) -> None:
        with self.lock:
            self.db.execute("UPDATE cases SET images = ? WHERE id = ?", (json.dumps(images), case_id))
            self.db.commit()

    def set_monitor(self, tid: str, monitor_id: str) -> None:
        with self.lock:
            self.db.execute("UPDATE targets SET monitor_id = ? WHERE id = ?", (monitor_id, tid))
            self.db.commit()

    def observe(self, tid: str, at: float, state: str, evidence: str, trigger: str, page_title: str = "",
                change_id: str = "", window_start: float | None = None, extract_ms: int | None = None,
                identity: str = "", identity_detail: str = "", excerpt: str = "", markdown: str = "",
                screenshot: bytes = b"", request_id: str = "", cache: str = "", credits: float | None = None,
                product: dict | None = None) -> dict:
        """Append one observation. A page without a supported product match counts as "could not classify"."""
        raw = state if state in STATES else "unverified"
        state = "unverified" if identity == "none" else raw
        digest = hashlib.sha256(markdown.encode()).hexdigest() if markdown else ""
        verified = (_squash(excerpt) in _squash(markdown)) if excerpt and markdown else None
        with self.lock:
            cur = self.db.execute(
                "INSERT INTO observations (target_id, at, state, evidence, page_title, trigger, change_id, window_start, extract_ms,"
                " raw_state, identity, identity_detail, excerpt, excerpt_verified, content_sha256, request_id, cache, credits,"
                " product, has_screenshot) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (tid, at, state, evidence[:600], page_title[:200], trigger, change_id, window_start, extract_ms, raw, identity,
                 identity_detail[:300], excerpt[:400], verified, digest, request_id, cache, credits,
                 json.dumps(product or {}, ensure_ascii=False), int(bool(screenshot))))
            oid = cur.lastrowid
            if markdown:
                self._put(f"{oid}.md", markdown.encode())
            if screenshot:
                self._put(f"{oid}.jpg", screenshot)
            self.db.commit()
            return dict(self.db.execute("SELECT * FROM observations WHERE id = ?", (oid,)).fetchone())

    def _put(self, name: str, data: bytes) -> None:
        if self.artifact_dir:
            os.makedirs(self.artifact_dir, exist_ok=True)
            with open(os.path.join(self.artifact_dir, name), "wb") as f:
                f.write(data)
        else:
            self._mem_artifacts[name] = data

    def artifact(self, name: str) -> bytes | None:
        if not re.fullmatch(r"(\d+\.(md|jpg)|(obs-\d+|case-c-[0-9a-f]+-\d+)\.(jpg|png|webp|gif|avif))", name):
            return None
        if self.artifact_dir:
            path = os.path.join(self.artifact_dir, name)
            return open(path, "rb").read() if os.path.exists(path) else None
        return self._mem_artifacts.get(name)

    def put_artifact(self, name: str, data: bytes) -> None:
        self._put(name, data)

    def set_observation_product(self, oid: int, product: dict) -> None:
        with self.lock:
            self.db.execute("UPDATE observations SET product = ? WHERE id = ?", (json.dumps(product, ensure_ascii=False), oid))
            self.db.commit()

    def artifacts_withheld(self) -> bool:
        row = self.db.execute("SELECT value FROM meta WHERE key = 'retailer_artifacts'").fetchone()
        return bool(row and row[0] == "withheld")

    def observation(self, oid: int) -> dict | None:
        row = self.db.execute("SELECT * FROM observations WHERE id = ?", (oid,)).fetchone()
        return dict(row) if row else None

    def act(self, tid: str, who: str, action: str, note: str, at: float, outcome: str = "", recipient: str = "",
            due_at: float | None = None, corrected_state: str = "") -> dict:
        target = self.target(tid)
        if not target:
            raise KeyError(tid)
        latest = self.observations(tid)
        kind = validate(action, who, note, outcome, recipient, corrected_state, STATES,
                        latest[-1]["state"] if latest else None)
        with self.lock:
            cur = self.db.execute(
                "INSERT INTO actions (target_id, at, who, action, note, outcome, recipient, due_at, corrected_state)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (tid, at, who.strip()[:120], kind, note.strip()[:1000], outcome, recipient.strip()[:200], due_at,
                 corrected_state if outcome == "classification_wrong" else ""))
            if kind == "owner_contacted" and recipient.strip():
                from .contextdev import domain
                self.db.execute("INSERT OR REPLACE INTO owners VALUES (?,?,?)", (domain(target["url"]), recipient.strip()[:200], at))
            self.db.commit()
            return dict(self.db.execute("SELECT * FROM actions WHERE id = ?", (cur.lastrowid,)).fetchone())

    def owner_for(self, url: str) -> str:
        from .contextdev import domain
        row = self.db.execute("SELECT recipient FROM owners WHERE domain = ?", (domain(url),)).fetchone()
        return row[0] if row else ""

    def report_rows(self, case_id: str) -> list[dict]:
        """Every observation and every action of a case, oldest first: the audit trail."""
        rows = []
        for t in self.targets(case_id):
            for o in self.observations(t["id"]):
                rows.append({"at": o["at"], "page": t["publisher"], "url": t["url"], "kind": "observation",
                             "detail": o["state"], "by": "Context.dev (" + o["trigger"] + ")", "text": o["evidence"],
                             "outcome": "", "recipient": "", "identity": o.get("identity") or "",
                             "excerpt": o.get("excerpt") or "", "request_id": o.get("request_id") or "",
                             "content_sha256": o.get("content_sha256") or ""})
            for a in self.actions(t["id"]):
                rows.append({"at": a["at"], "page": t["publisher"], "url": t["url"], "kind": "action",
                             "detail": LEGACY.get(a["action"], a["action"]), "by": a["who"], "text": a["note"],
                             "outcome": a.get("outcome") or "", "recipient": a.get("recipient") or "", "identity": "",
                             "excerpt": "", "request_id": "", "content_sha256": ""})
        return sorted(rows, key=lambda r: r["at"])

    def suggest(self, case_id: str, url: str, title: str, at: float, query: str = "") -> None:
        with self.lock:
            self.db.execute("INSERT OR IGNORE INTO suggestions (case_id, url, title, found_at, query) VALUES (?,?,?,?,?)",
                            (case_id, url, title[:200], at, query[:200]))
            self.db.commit()

    # Reads ------------------------------------------------------------------
    def _case(self, row) -> dict:
        d = dict(row)
        d["products"], d["gtins"] = json.loads(d["products"]), json.loads(d["gtins"])
        d["images"] = json.loads(d.get("images") or "[]")
        return d

    def case(self, cid: str) -> dict | None:
        row = self.db.execute("SELECT * FROM cases WHERE id = ?", (cid,)).fetchone()
        return self._case(row) if row else None

    def cases(self) -> list[dict]:
        return [self._case(r) for r in self.db.execute("SELECT * FROM cases ORDER BY notice_at DESC")]

    def target(self, tid: str) -> dict | None:
        row = self.db.execute("SELECT * FROM targets WHERE id = ?", (tid,)).fetchone()
        return dict(row) if row else None

    def targets(self, case_id: str | None = None) -> list[dict]:
        if case_id is None:
            return [dict(r) for r in self.db.execute("SELECT * FROM targets ORDER BY added_at")]
        return [dict(r) for r in self.db.execute("SELECT * FROM targets WHERE case_id = ? ORDER BY added_at", (case_id,))]

    def observations(self, tid: str) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM observations WHERE target_id = ? ORDER BY at, id", (tid,))]

    def actions(self, tid: str) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM actions WHERE target_id = ? ORDER BY at, id", (tid,))]

    def record_spend(self, at: float, kind: str) -> None:
        with self.lock:
            self.db.execute("INSERT INTO spend VALUES (?, ?)", (at, kind))
            self.db.commit()

    def spend_times_since(self, t: float) -> list[float]:
        return [r[0] for r in self.db.execute("SELECT at FROM spend WHERE at > ? ORDER BY at", (t,))]

    def observation_times_since(self, t: float) -> list[float]:
        return [r[0] for r in self.db.execute("SELECT at FROM observations WHERE at > ? ORDER BY at", (t,))]

    def change_ids(self) -> set[str]:
        """The Context.dev change ids that already produced an observation."""
        return {r[0] for r in self.db.execute("SELECT DISTINCT change_id FROM observations WHERE change_id != ''")}

    def suggestions(self, case_id: str) -> list[dict]:
        watched = {t["url"] for t in self.targets(case_id)}
        rows = self.db.execute("SELECT url, title, found_at, query FROM suggestions WHERE case_id = ? ORDER BY found_at", (case_id,))
        return [dict(r) for r in rows if r["url"] not in watched]

    def view(self, case_id: str, now: float) -> dict:
        case = self.case(case_id)
        if not case:
            raise KeyError(case_id)
        targets = self.targets(case_id)
        return build_case_view(case, targets, {t["id"]: self.observations(t["id"]) for t in targets},
                               {t["id"]: self.actions(t["id"]) for t in targets}, now)


# The view -------------------------------------------------------------------

def _first(rows: list[dict], test) -> dict | None:
    return next((r for r in rows if test(r)), None)


def target_timeline(obs: list[dict], acts: list[dict], notice_at: float, sla_s: float, now: float,
                    lot_limited: bool = False, publisher: str = "the owner", when=fmt) -> dict:
    """The milestones of one public page, measured from the official notice.

    The exact moment a page changed is unknown: it happened after the last observation that still
    showed the old state (or the monitor's previous run) and at or before the observation that
    shows the new one.  The lag is therefore reported as an upper bound with that window.
    """
    out: dict = {"observations": len(obs)}
    if not obs:
        return {**out, "state": None, "sla": "not_observed", "needs_action": False, "milestones": {},
                "review": "open", "next_step": "Waiting for the first Context.dev read.", "contact": None, "resolution": None}
    latest = obs[-1]
    since = latest
    for o in reversed(obs):
        if o["state"] != latest["state"]:
            break
        since = o
    first_stale = _first(obs, lambda o: o["state"] == "active_listing")

    def response(test) -> dict | None:
        """The first observation passing `test` after the page was seen purchasable, or the first at all."""
        start = obs.index(first_stale) if first_stale else 0
        hit = _first(obs[start:], test)
        if not hit:
            return None
        i = obs.index(hit)
        if i == 0:   # the page already showed it on our first read: we did not see the change
            return {"at": hit["at"], "lag_s": hit["at"] - notice_at, "changed_after": None, "seen_before_watch": True}
        changed_after = max(obs[i - 1]["at"], hit.get("window_start") or 0)
        return {"at": hit["at"], "lag_s": hit["at"] - notice_at, "changed_after": changed_after,
                "seen_before_watch": False}

    closed = response(lambda o: o["state"] in PURCHASE_CLOSED)
    recall = response(lambda o: o["state"] == "recall_message")
    relisted = bool(closed and latest["state"] == "active_listing" and latest["at"] > closed["at"])
    due = notice_at + sla_s
    stale_after_due = any(o["state"] == "active_listing" and o["at"] > due for o in obs)

    if latest["state"] == "active_listing" and lot_limited and now > due:
        # The recall names specific lots, so a page can rightly sell newer stock. Only a person
        # who checks the listed lot can say whether the page is a problem.
        sla = "verify_lot"
    elif latest["state"] == "active_listing":
        sla = "breached" if now > due else "open"
    elif stale_after_due and not lot_limited:
        sla = "breached"
    elif closed and closed["at"] <= due:
        sla = "met"
    elif closed and closed["seen_before_watch"]:
        sla = "not_measurable"            # already closed when first watched, after the deadline
    elif closed:
        changed_after = closed["changed_after"]
        sla = "breached" if changed_after is not None and changed_after > due else "indeterminate"
    else:
        sla = "unclassified"

    review = review_state(obs, acts, since["at"], now, publisher, latest["state"], PURCHASE_CLOSED, when)
    needs_action = review["needs_action"]
    reopened = review["review"] == "reopened"
    resolution = review["resolution"]
    if resolution and resolution.get("outcome") == "lot_outside_recall" and sla == "verify_lot":
        sla = "lot_confirmed"      # the owner's word, recorded with a name and a note
    # Identity: computed per observation from the product block, the page text and the address.
    identity = latest.get("identity") or ""
    confirmed = next((a for a in reversed(acts) if a["action"] == "identity_confirmed"), None)
    identity_pending = identity == "partial_name" and not confirmed and review["review"] not in {"resolved"}
    if confirmed and identity in {"partial_name", "none"}:
        identity = "confirmed"
    if identity_pending:
        sla = "identity_check"     # a partial name match never enters the SLA without a person
        needs_action = True
        review["next_step"] = ("Confirm that this page sells the recalled product: " + (latest.get("identity_detail") or "")
                               + ". Compare the official photo with the page photo.")
    review = {**review, "needs_action": needs_action}
    return {**out, "state": latest["state"], "state_label": STATE_LABEL[latest["state"]], "state_since": since["at"],
            "evidence": latest["evidence"], "excerpt": latest.get("excerpt") or "",
            "excerpt_verified": bool(latest.get("excerpt_verified")), "page_title": latest.get("page_title") or "", "observed_at": latest["at"],
            "trigger": latest["trigger"], "first_observed_at": obs[0]["at"],
            "milestones": {"first_stale": first_stale["at"] if first_stale else None, "purchase_closed": closed,
                           "recall_visible": recall,
                           "reviewer_closed": next((a["at"] for a in reversed(acts) if a["action"] == "closed"), None)},
            "relisted": relisted, "sla": sla, "sla_due": due, "needs_action": needs_action, "reopened": reopened,
            **review,
            "identity": identity, "identity_detail": latest.get("identity_detail") or "", "identity_pending": identity_pending,
            "identity_confirmed": {k: confirmed[k] for k in ("who", "at", "note")} if confirmed else None,
            "latest_observation_id": latest.get("id"),
            "product": json.loads(latest.get("product") or "{}") if isinstance(latest.get("product"), str) else (latest.get("product") or {}),
            "history": [{"id": o.get("id"), "at": o["at"], "state": o["state"], "trigger": o["trigger"], "evidence": o["evidence"],
                         "identity": o.get("identity") or "", "has_receipt": bool(o.get("request_id"))} for o in obs]}


def build_case_view(case: dict, targets: list[dict], obs: dict[str, list[dict]], acts: dict[str, list[dict]], now: float,
                    simulated: bool = False) -> dict:
    sla_s = case["sla_hours"] * 3600
    when = (lambda t: "T+" + dur(t - case["notice_at"])) if simulated else fmt
    lot_limited = bool(case.get("lot"))
    rows = []
    for t in targets:
        tl = target_timeline(obs.get(t["id"], []), acts.get(t["id"], []), case["notice_at"], sla_s, now, lot_limited,
                             t["publisher"], when)
        rows.append({**t, **tl, "actions": acts.get(t["id"], [])})
    order = {"breached": 0, "verify_lot": 0, "identity_check": 0, "open": 1, "lot_confirmed": 5, "unclassified": 2, "indeterminate": 3, "not_measurable": 4, "met": 5, "not_observed": 6}
    rows.sort(key=lambda r: (not r["needs_action"], order.get(r["sla"], 9), r["added_at"]))
    closed_lags = [r["milestones"]["purchase_closed"]["lag_s"] for r in rows
                   if r["milestones"].get("purchase_closed") and not r["milestones"]["purchase_closed"]["seen_before_watch"]]
    summary = {
        "targets": len(rows),
        "purchase_closed": sum(r["state"] in PURCHASE_CLOSED for r in rows),
        "recall_visible": sum(r["state"] == "recall_message" for r in rows),
        "appears_purchasable": sum(r["state"] == "active_listing" for r in rows),
        "needs_action": sum(r["needs_action"] for r in rows),
        "sla_breached": sum(r["sla"] == "breached" for r in rows),
        "sla_met": sum(r["sla"] == "met" for r in rows),
        "verify_lot": sum(r["sla"] == "verify_lot" for r in rows),
        "identity_checks": sum(r.get("identity_pending", False) for r in rows),
        "waiting_on_owner": sum(r.get("review") == "waiting_on_owner" for r in rows),
        "overdue": sum(r.get("review") == "waiting_on_owner" and r["needs_action"] for r in rows),
        "resolved": sum(r.get("review") == "resolved" for r in rows),
        "observations": sum(r["observations"] for r in rows),
        "reviewer_corrections": sum(1 for r in rows for a in r["actions"] if a.get("outcome") == "classification_wrong"),
        "lot_limited": lot_limited,
        "median_measured_lag_s": statistics.median(closed_lags) if closed_lags else None,
        "measured_lags": len(closed_lags),
    }
    return {"case": case, "targets": rows, "summary": summary, "now": now, "sla_due": case["notice_at"] + sla_s}
