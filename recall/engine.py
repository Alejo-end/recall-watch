"""Recall notices in, one answer for the kitchen out.

Plain code decides the answer. Context.dev finds the notices and reads their fields.
This module compares each notice with the products on the shelf:

    0 clear   no open notice names a product on the shelf
    1 check   a notice names a product like one on the shelf: same brand and name, or a similar name
    2 quarantine  an official notice has the same EAN code as a product on the shelf

A notice counts for `active_days` after its date, and longer while a pack of it is on hold. A hit
stays open while it is new or held. It closes when a person records the lot on the pack, or that
the product is not on the shelf. A notice that is not from the authority never gives more than "check".
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass, field, replace

CLEAR, CHECK, QUARANTINE = 0, 1, 2
# PULL remains an alias for integrations built before the safety wording changed.
PULL = QUARANTINE
LEVEL_NAMES = {CLEAR: "clear", CHECK: "check", QUARANTINE: "quarantine"}
HAZARDS = ("allergen", "microbe", "foreign_object", "chemical", "spoilage", "labelling", "contact_material", "other")
ALLERGENS = ("milk", "egg", "fish", "crustaceans", "molluscs", "gluten", "peanut", "nuts", "soy", "mustard",
             "sesame", "celery", "lupin", "sulphites")
# A hold is deliberately not a terminal action.  The kitchen still needs to read the physical
# lot before it can release or confirm a candidate.  Legacy names are accepted by `ack()` so an
# older client does not silently lose its record, but new callers use the explicit event names.
STAFF_EVENTS = ("held", "confirmed_affected", "released_not_this_lot", "not_on_shelf")
LEGACY_ACTIONS = {"quarantined": "held", "not_this_lot": "released_not_this_lot"}
PENDING_CASE_STATES = {"new", "held_pending_verification"}
DAY = 86400
# What a match rests on. `match` returns one of these, and `match_evidence` puts it in words.
EAN, NAME, SIMILAR = "ean", "name", "similar"

_SIZE = re.compile(r"^(\d+x)?\d+([.,]\d+)?(g|kg|mg|ml|l|dl|cl|kpl)?$")
_UNIT = re.compile(r"^(g|kg|mg|ml|l|dl|cl|kpl)$")
_COMPANY_WORDS = {"oy", "oyj", "ab", "ky", "oü", "ltd", "gmbh", "suomi", "suomen", "finland", "osuuskunta", "tuore", "professional"}


def words(text: str) -> list[str]:
    return [w for w in re.sub(r"[^0-9a-zà-öø-ÿ,.]+", " ", (text or "").lower()).split() if w.strip(".,")]


def name_tokens(text: str) -> set[str]:
    """The words of a product name, without pack sizes."""
    return {w.strip(".,") for w in words(text) if not _SIZE.match(w.strip(".,")) and len(w.strip(".,")) > 1}


def size_tokens(text: str) -> set[str]:
    """The pack sizes in a name: "300 g" and "300g" both give "300g"."""
    ws, out = [w.strip(".,") for w in words(text)], set()
    for i, w in enumerate(ws):
        if _SIZE.match(w) and re.search(r"[a-z]", w):
            out.add(w.replace(",", "."))
        elif re.fullmatch(r"\d+([.,]\d+)?", w) and i + 1 < len(ws) and _UNIT.match(ws[i + 1]):
            out.add(w.replace(",", ".") + ws[i + 1])
    return out


def brand_tokens(brand: str, company: str = "") -> set[str]:
    return name_tokens(brand) or (name_tokens(company) - _COMPANY_WORDS)


def norm_ean(code) -> str:
    """Return a canonical GTIN-14 without discarding significant leading zeroes.

    EAN-8, UPC-A and EAN-13 values are valid GTIN representations.  Left-padding them to
    GTIN-14 allows an EAN-13 on a pack to match the GTIN-14 in a supplier feed without turning
    two different codes into the same value by stripping zeroes.
    """
    digits = re.sub(r"\D", "", str(code or ""))
    return digits.zfill(14) if len(digits) in (8, 12, 13) else digits


@dataclass
class Product:
    id: str
    name: str
    brand: str = ""
    ean: str = ""
    supplier: str = ""
    # These fields come from a stock / menu import in a real kitchen.  They are deliberately
    # operational context, not evidence that a particular lot is affected: a staff member must
    # still read the physical pack before they confirm the recall.
    stock_location: str = ""
    candidate_stock: str = ""
    next_service: str = ""
    open_order: str = ""
    lot: str = ""
    expiry: str = ""

    def __post_init__(self):
        self.ean_display = re.sub(r"\D", "", str(self.ean or ""))
        self.ean = norm_ean(self.ean)
        self._name = name_tokens(self.name)
        self._all = self._name | name_tokens(self.brand) | (name_tokens(self.supplier) - _COMPANY_WORDS)
        self._sizes = size_tokens(self.name)


@dataclass
class Notice:
    id: str
    url: str
    title: str
    date: str                       # the date on the notice, YYYY-MM-DD
    published_at: float             # unix seconds: 09:00 Finnish winter time on that date, or the first read when that is earlier
    detected_at: float              # unix seconds: when this system first read the notice
    source: str = ""                # the domain the reader can check
    official: bool = True           # False: found on the web, not on the authority's list
    company: str = ""
    brand: str = ""
    products: list[str] = field(default_factory=list)
    eans: list[str] = field(default_factory=list)
    lot: str = ""
    hazard: str = "other"
    allergens: list[str] = field(default_factory=list)
    summary: str = ""
    extracted_by: str = ""
    extract_ms: int | None = None          # how long Context.dev took to read the fields
    monitor_detected_at: float | None = None   # when a Context.dev monitor saw the change that led here
    title_en: str = ""                     # machine working translation of `title`; the Finnish title is authoritative
    images: list[dict] = field(default_factory=list)   # product photos published with the notice

    def __post_init__(self):
        if self.hazard not in HAZARDS:
            self.hazard = "other"
        self.allergens = [a for a in self.allergens if a in ALLERGENS]
        self.eans = [e for e in (norm_ean(e) for e in self.eans) if len(e) >= 8]
        self.products = [p for p in self.products if p]


def match(n: Notice, p: Product) -> tuple[int, str, str]:
    """How strongly a notice names a product, the reason in words, and what the match rests on."""
    level, why, basis = CLEAR, "", ""
    if p.ean and p.ean in n.eans:
        level, why, basis = PULL, "same EAN code", EAN
    else:
        brand = brand_tokens(n.brand, n.company)
        same_brand = bool(brand) and brand <= p._all
        for name in n.products:
            want = name_tokens(name) - brand
            if not want:
                continue
            got = want & p._name
            share = len(got) / len(want)
            sizes = size_tokens(name)
            other_pack = bool(sizes and p._sizes and not sizes & p._sizes) or bool(p.ean and n.eans)
            if same_brand and share >= 0.6:
                # Text can identify a product family but cannot establish the exact barcode on a
                # physical pack.  Only an official GTIN match can create a hold.
                cand = (CHECK, "same brand and name, another pack or code", NAME) if other_pack else (CHECK, "same brand and product name", NAME)
            elif not brand and share >= 0.8 and len(got) >= 3:
                cand = (CHECK, "a similar product name", SIMILAR)   # only when the notice names no brand and no company
            else:
                continue
            if cand[0] > level:
                level, why, basis = cand
    if level and not n.official:
        level, why = CHECK, why + ", not yet on the authority's list"
    return level, why, basis


def match_evidence(level: int, basis: str) -> tuple[str, str]:
    """The confidence and the basis of a match, for the person who must verify the physical pack.

    Only an EAN match at the level "quarantine" is high. A notice that is not from the authority
    gives "check" at most, so its EAN match is medium.
    """
    if basis == EAN:
        return ("high" if level == QUARANTINE else "medium"), "EAN code"
    if basis == SIMILAR:
        return "low", "similar product name"
    return "medium", "brand and product name"


class Board:
    """The notices, the hits on the shelf, what people did about them, and the level over time."""

    def __init__(self, shelf: list[Product], active_days: int = 30, db: str = ":memory:"):
        self.shelf, self.active_days = shelf, active_days
        self.by_id = {p.id: p for p in shelf}
        self.lock = threading.RLock()
        self.db = sqlite3.connect(db, check_same_thread=False)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS notices (id TEXT PRIMARY KEY, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS notice_versions (
                notice_id TEXT NOT NULL, version INTEGER NOT NULL, observed_at REAL NOT NULL, body TEXT NOT NULL,
                PRIMARY KEY (notice_id, version)
            );
            CREATE TABLE IF NOT EXISTS events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT, notice_id TEXT NOT NULL, product_id TEXT NOT NULL,
                action TEXT NOT NULL, who TEXT NOT NULL, lot TEXT NOT NULL DEFAULT '', expiry TEXT NOT NULL DEFAULT '',
                note TEXT NOT NULL DEFAULT '', at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS history (t REAL, level INTEGER);
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)
        self._migrate_acks()
        self.notices: dict[str, Notice] = {}
        self.hits: dict[str, list[tuple[str, int, str, str]]] = {}   # notice id to (product id, level, reason, basis)
        for (body,) in self.db.execute("SELECT body FROM notices"):
            self._keep(Notice(**json.loads(body)))

    def _migrate_acks(self) -> None:
        """Copy the acknowledgements of the first version into the event log, once.

        The first version kept one row per candidate in `acks` and no lot. Without this copy, every
        candidate that staff had already released or found not on the shelf would show as new work
        again. The copied events keep the original person and time, and say that they were migrated.
        The `acks` table itself is left in place as the original record.
        """
        with self.lock:
            has_acks = self.db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'acks'").fetchone()
            done = self.db.execute("SELECT 1 FROM meta WHERE key = 'acks_migrated'").fetchone()
            if not has_acks or done:
                return
            for nid, pid, action, who, at in self.db.execute(
                    "SELECT notice_id, product_id, action, who, at FROM acks ORDER BY at").fetchall():
                event = LEGACY_ACTIONS.get(action, action)
                if event not in STAFF_EVENTS:
                    continue
                self.db.execute(
                    "INSERT INTO events (notice_id, product_id, action, who, note, at) VALUES (?, ?, ?, ?, ?, ?)",
                    (nid, pid, event, who or "unknown", f"Migrated from the first version ({action}); no lot was recorded then.", at or 0))
            self.db.execute("INSERT INTO meta VALUES ('acks_migrated', ?)", (str(time.time()),))
            self.db.commit()

    @staticmethod
    def _evidence_body(n: Notice) -> dict:
        """Fields that change the evidence or decision, excluding read-time telemetry."""
        return {
            "url": n.url, "title": n.title, "date": n.date, "source": n.source, "official": n.official,
            "company": n.company, "brand": n.brand, "products": n.products, "eans": n.eans, "lot": n.lot,
            "hazard": n.hazard, "allergens": n.allergens, "summary": n.summary, "extracted_by": n.extracted_by,
        }

    @staticmethod
    def _decision_body(n: Notice) -> dict:
        """Only what changes a decision on a pack, normalised so a reworded extraction is not a change.

        The English summary and the wording of the lot field come from a language model and can
        differ between two reads of the same page. A revision counts only when the codes, the
        product names (as word sets, without pack sizes), the dates and numbers of the lot field,
        the hazard or the allergens change.
        """
        return {
            "eans": sorted({norm_ean(e) for e in n.eans}),
            "products": sorted(" ".join(sorted(name_tokens(p))) for p in n.products),
            "brand": sorted(name_tokens(n.brand)),
            "lot_codes": sorted(set(re.findall(r"\d+(?:[./-]\d+)*", n.lot or ""))),
            "hazard": n.hazard, "allergens": sorted(n.allergens), "official": n.official,
        }

    @classmethod
    def _fingerprint(cls, n: Notice) -> str:
        return json.dumps(cls._decision_body(n), ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def _keep(self, n: Notice) -> None:
        self.notices[n.id] = n
        self.hits[n.id] = [(p.id, lv, why, basis) for p in self.shelf for lv, why, basis in [match(n, p)] if lv]

    def has(self, notice_id: str) -> bool:
        with self.lock:
            return notice_id in self.notices

    def add(self, n: Notice) -> bool:
        """Store or revise a notice. Returns True only when its ID is new.

        A page can gain a lot number or correct its product list after the first scrape.  The
        latest version drives the task, while `notice_versions` keeps every structured evidence
        revision for review.
        """
        with self.lock:
            old = self.notices.get(n.id)
            if old and self._fingerprint(old) == self._fingerprint(n):
                return False
            observed_at = n.detected_at
            if old:
                # A revision does not move the notice later: it still counts from its first read.
                n = replace(n, published_at=min(old.published_at, n.published_at), detected_at=min(old.detected_at, n.detected_at))
            body = json.dumps(asdict(n), ensure_ascii=False)
            if old and not self.db.execute("SELECT 1 FROM notice_versions WHERE notice_id = ?", (n.id,)).fetchone():
                # A notice stored before versions existed: keep its original body as version 1 first.
                self.db.execute("INSERT INTO notice_versions VALUES (?, 1, ?, ?)",
                                (n.id, old.detected_at, json.dumps(asdict(old), ensure_ascii=False)))
            self.db.execute("INSERT OR REPLACE INTO notices VALUES (?, ?)", (n.id, body))
            version = self.db.execute("SELECT COALESCE(MAX(version), 0) + 1 FROM notice_versions WHERE notice_id = ?", (n.id,)).fetchone()[0]
            self.db.execute("INSERT INTO notice_versions VALUES (?, ?, ?, ?)", (n.id, version, observed_at, body))
            self._keep(n)
            # A released/confirmed pack was assessed against older web evidence.  If the source
            # changes (for example, it expands the lot list), bring that candidate back for a
            # human decision without deleting the earlier chain of custody.  A held pack remains
            # held: it is already safely out of service.
            if old:
                cases = self._case_states()
                for pid, *_ in self.hits[n.id]:
                    previous = cases.get((n.id, pid))
                    if previous and previous["state"] in {"released", "confirmed_affected", "not_on_shelf"}:
                        self.db.execute(
                            "INSERT INTO events (notice_id, product_id, action, who, note, at) VALUES (?, ?, ?, ?, ?, ?)",
                            (n.id, pid, "evidence_revised", "Recall Watch", "Source evidence changed; physical decision must be rechecked.", observed_at),
                        )
            self.db.commit()
            return old is None

    def record_event(self, notice_id: str, product_id: str, action: str, who: str, now: float,
                     lot: str = "", expiry: str = "", note: str = "") -> None:
        """Append a staff action without erasing the earlier chain of custody."""
        action = LEGACY_ACTIONS.get(action, action)
        if action not in STAFF_EVENTS:
            raise ValueError(f"action must be one of {STAFF_EVENTS}")
        if not who.strip():
            raise ValueError("a name is needed")
        if action in {"confirmed_affected", "released_not_this_lot"} and not lot.strip():
            raise ValueError("the physical lot or use-by value is needed to close a case")
        with self.lock:
            if not any(hit[0] == product_id for hit in self.hits.get(notice_id, [])):
                raise KeyError("no such hit")
            current = self._case_states().get((notice_id, product_id))
            if action in {"confirmed_affected", "released_not_this_lot"} and (not current or current["state"] != "held_pending_verification"):
                raise ValueError("hold the candidate before recording a lot-based outcome")
            self.db.execute("INSERT INTO events (notice_id, product_id, action, who, lot, expiry, note, at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                            (notice_id, product_id, action, who.strip()[:60], lot.strip()[:120], expiry.strip()[:60], note.strip()[:500], now))
            self.db.commit()

    def ack(self, notice_id: str, product_id: str, action: str, who: str, now: float,
            lot: str = "", expiry: str = "", note: str = "") -> None:
        """Compatibility name for callers built before the append-only event model."""
        self.record_event(notice_id, product_id, action, who, now, lot=lot, expiry=expiry, note=note)

    def replace_shelf(self, shelf: list[Product]) -> list[str]:
        """Replace the current import and immediately recompute every open recall candidate.

        A product with a pack on hold stays on the shelf even when the new import leaves it out:
        the pack is physically held and still needs its lot check. Returns the ids kept this way.
        """
        with self.lock:
            new_ids = {p.id for p in shelf}
            held = {pid for (_, pid), c in self._case_states().items() if c["state"] == "held_pending_verification"}
            kept = [self.by_id[pid] for pid in sorted(held - new_ids) if pid in self.by_id]
            shelf = list(shelf) + kept
            self.shelf = shelf
            self.by_id = {p.id: p for p in shelf}
            self.hits = {}
            for n in self.notices.values():
                self._keep(n)
            return [p.id for p in kept]

    def clear(self) -> None:
        """Forget everything. The replay calls this when it starts again."""
        with self.lock:
            self.db.executescript("DELETE FROM notices; DELETE FROM notice_versions; DELETE FROM events; DELETE FROM history;")
            self.notices, self.hits = {}, {}

    def _case_states(self) -> dict[tuple[str, str], dict]:
        """Return the latest event and derived state for each candidate, without collapsing history."""
        states: dict[tuple[str, str], dict] = {}
        with self.lock:
            rows = self.db.execute("SELECT event_id, notice_id, product_id, action, who, lot, expiry, note, at FROM events ORDER BY event_id").fetchall()
        for event_id, nid, pid, action, who, lot, expiry, note, at in rows:
            state = {
                "held": "held_pending_verification",
                "confirmed_affected": "confirmed_affected",
                "released_not_this_lot": "released",
                "not_on_shelf": "not_on_shelf",
                "evidence_revised": "new",
            }[action]
            states[(nid, pid)] = {"event_id": event_id, "action": action, "state": state, "who": who,
                                  "lot": lot, "expiry": expiry, "note": note, "at": at}
        return states

    def active(self, now: float) -> list[Notice]:
        """A copy of the list of notices that count at `now`, the newest first.

        A notice counts for `active_days` after its date. A notice with a pack still on hold counts
        after that too, until a person records the lot-based outcome.
        """
        with self.lock:
            cases = self._case_states()
            out = []
            for n in self.notices.values():
                ordinary_window = n.published_at <= now < n.published_at + self.active_days * DAY
                held_case = any(cases.get((n.id, hit[0]), {}).get("state") == "held_pending_verification"
                                for hit in self.hits.get(n.id, []))
                if ordinary_window or held_case:
                    out.append(n)
            return sorted(out, key=lambda n: (n.published_at, n.detected_at), reverse=True)

    def hits_at(self, now: float) -> list[dict]:
        """Every hit of the notices that count at `now`. Open hits come first, the highest level first."""
        with self.lock:
            cases = self._case_states()
            hits = []
            for n in self.active(now):
                for pid, lv, why, basis in self.hits[n.id]:
                    case = cases.get((n.id, pid))
                    case_state = case["state"] if case else "new"
                    confidence, basis_words = match_evidence(lv, basis)
                    product = self.by_id[pid]
                    hits.append({"notice": n.id, "product": pid, "product_name": product.name, "level": lv,
                                 "level_name": LEVEL_NAMES[lv], "why": why, "confidence": confidence,
                                 "match_basis": basis_words, "official": n.official, "case_state": case_state,
                                 "last_event": case, "ack": case,
                                 "stock_location": product.stock_location,
                                 "candidate_stock": product.candidate_stock,
                                 "next_service": product.next_service,
                                 "open_order": product.open_order, "imported_lot": product.lot,
                                 "imported_expiry": product.expiry})
            hits.sort(key=lambda h: (h["case_state"] not in PENDING_CASE_STATES, h["case_state"] == "held_pending_verification", -h["level"]))
            return hits

    def blocked(self, now: float) -> list[dict]:
        """Every open "quarantine" hit at `now`, with the product's EAN code and the address of the notice.

        A hit is open while it is new or held pending its lot check, the same rule that sets the level.
        """
        with self.lock:
            return [{**h, "ean": self.by_id[h["product"]].ean_display, "url": self.notices[h["notice"]].url}
                    for h in self.hits_at(now) if h["level"] == QUARANTINE and h["case_state"] in PENDING_CASE_STATES]

    def state(self, now: float) -> dict:
        with self.lock:
            active, hits = self.active(now), self.hits_at(now)
            level = max((h["level"] for h in hits if h["case_state"] in PENDING_CASE_STATES), default=CLEAR)
            last = self.db.execute("SELECT t, level FROM history ORDER BY rowid DESC LIMIT 1").fetchone()
            if not last or last[1] != level:
                self.db.execute("INSERT INTO history VALUES (?, ?)", (now, level))
                self.db.commit()
                last = (now, level)
            open_hits = [h for h in hits if h["case_state"] in PENDING_CASE_STATES]
            return {
                "now": now, "level": level, "level_name": LEVEL_NAMES[level], "since": last[0],
                "answer": answer(level, open_hits, self.notices),
                "hits": hits[:200], "hit_count": len(open_hits),
                "blocked": sorted({h["product"] for h in open_hits if h["level"] == QUARANTINE}),
                "notices": [{**asdict(n), "age_s": now - n.detected_at, "hits": len(self.hits[n.id])} for n in active],
                "year": [{"id": n.id, "title": n.title, "t": n.published_at, "hazard": n.hazard,
                          "allergens": n.allergens, "level": max((hit[1] for hit in self.hits[n.id]), default=CLEAR)}
                         for n in sorted(self.notices.values(), key=lambda n: n.published_at) if n.published_at <= now],
                "history": [{"t": t, "level": lv} for t, lv in self.db.execute("SELECT t, level FROM history ORDER BY rowid")][-400:],
                "shelf_size": len(self.shelf), "active_days": self.active_days,
            }


def answer(level: int, open_hits: list[dict], notices: dict[str, Notice]) -> str:
    """The sentence a cook reads. It names the product, the reason and the lot."""
    if level == CLEAR:
        return "No open recall names a product on this shelf."
    top = next(h for h in open_hits if h["level"] == level)
    n = notices[top["notice"]]
    risk = f" Allergens named: {', '.join(n.allergens)}." if n.allergens else ""
    lot = f" Lot: {n.lot}." if n.lot else ""
    if level == QUARANTINE:
        if top["case_state"] == "held_pending_verification":
            return f"{top['product_name']} is held pending physical lot verification. {n.summary}{risk}{lot}"
        return f"Quarantine {top['product_name']} and verify the lot on the pack. {n.summary}{risk}{lot}"
    return f"Check {top['product_name']}. A recall names a product like it ({top['why']}). {n.summary}{risk}{lot}"
