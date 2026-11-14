"""Recall notices in, one answer for the kitchen out.

Plain code decides the answer. Context.dev finds the notices and reads their fields.
This module compares each notice with the products on the shelf:

    0 clear   no open notice names a product on the shelf
    1 check   a notice names a product like one on the shelf: same brand and name, but another pack or code
    2 quarantine  an official notice names a product on the shelf: same EAN code, or same brand and name

A notice counts for `active_days` after its date. A hit stops counting when a person records what
they did about it. A notice that is not from the authority never gives more than "check".
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from dataclasses import asdict, dataclass, field

CLEAR, CHECK, QUARANTINE = 0, 1, 2
# PULL remains an alias for integrations built before the safety wording changed.
PULL = QUARANTINE
LEVEL_NAMES = {CLEAR: "clear", CHECK: "check", QUARANTINE: "quarantine"}
HAZARDS = ("allergen", "microbe", "foreign_object", "chemical", "spoilage", "labelling", "contact_material", "other")
ALLERGENS = ("milk", "egg", "fish", "crustaceans", "molluscs", "gluten", "peanut", "nuts", "soy", "mustard",
             "sesame", "celery", "lupin", "sulphites")
ACTIONS = ("quarantined", "confirmed_affected", "not_this_lot", "not_on_shelf")
DAY = 86400

_SIZE = re.compile(r"^(\d+x)?\d+([.,]\d+)?(g|kg|mg|ml|l|dl|cl|kpl)?$")
_UNIT = re.compile(r"^(g|kg|mg|ml|l|dl|cl|kpl)$")
_COMPANY_WORDS = {"oy", "oyj", "ab", "ky", "oü", "ltd", "gmbh", "suomi", "suomen", "finland", "osuuskunta", "tuore", "professional"}


def words(text: str) -> list[str]:
    return [w for w in re.sub(r"[^0-9a-zà-öø-ÿ,.]+", " ", (text or "").lower()).replace(" - ", " ").split() if w.strip(".,")]


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
    return re.sub(r"\D", "", str(code or "")).lstrip("0")


@dataclass
class Product:
    id: str
    name: str
    brand: str = ""
    ean: str = ""
    supplier: str = ""

    def __post_init__(self):
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
    published_at: float             # unix seconds of that date
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

    def __post_init__(self):
        if self.hazard not in HAZARDS:
            self.hazard = "other"
        self.allergens = [a for a in self.allergens if a in ALLERGENS]
        self.eans = [e for e in (norm_ean(e) for e in self.eans) if len(e) >= 8]
        self.products = [p for p in self.products if p]


def match(n: Notice, p: Product) -> tuple[int, str]:
    """How strongly a notice names a product, and the reason in words."""
    level, why = CLEAR, ""
    if p.ean and p.ean in n.eans:
        level, why = PULL, "same EAN code"
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
                cand = (CHECK, "same brand and name, another pack or code") if other_pack else (PULL, "same brand and product name")
            elif not brand and share >= 0.8 and len(got) >= 3:
                cand = (CHECK, "a similar product name")   # only when the notice names no brand and no company
            else:
                continue
            if cand[0] > level:
                level, why = cand
    if level == PULL and not n.official:
        level, why = CHECK, why + ", not yet on the authority's list"
    return level, why


def match_evidence(level: int, why: str) -> tuple[str, str]:
    """A compact explanation for the person who must verify the physical pack."""
    if why == "same EAN code":
        return "high", "EAN code"
    if level == QUARANTINE:
        return "medium", "brand and product name"
    if "similar" in why:
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
            CREATE TABLE IF NOT EXISTS acks (notice_id TEXT, product_id TEXT, action TEXT, who TEXT, at REAL,
                                             PRIMARY KEY (notice_id, product_id));
            CREATE TABLE IF NOT EXISTS history (t REAL, level INTEGER);
        """)
        self.notices: dict[str, Notice] = {}
        self.hits: dict[str, list[tuple[str, int, str]]] = {}   # notice id to (product id, level, reason)
        for (body,) in self.db.execute("SELECT body FROM notices"):
            self._keep(Notice(**json.loads(body)))

    def _keep(self, n: Notice) -> None:
        self.notices[n.id] = n
        self.hits[n.id] = [(p.id, lv, why) for p in self.shelf for lv, why in [match(n, p)] if lv]

    def add(self, n: Notice) -> bool:
        """Store a notice. Returns True when it is new."""
        with self.lock:
            if n.id in self.notices:
                return False
            self.db.execute("INSERT INTO notices VALUES (?, ?)", (n.id, json.dumps(asdict(n), ensure_ascii=False)))
            self.db.commit()
            self._keep(n)
            return True

    def ack(self, notice_id: str, product_id: str, action: str, who: str, now: float) -> None:
        if action not in ACTIONS:
            raise ValueError(f"action must be one of {ACTIONS}")
        if not who.strip():
            raise ValueError("a name is needed")
        with self.lock:
            if not any(pid == product_id for pid, _, _ in self.hits.get(notice_id, [])):
                raise KeyError("no such hit")
            self.db.execute("INSERT OR REPLACE INTO acks VALUES (?, ?, ?, ?, ?)", (notice_id, product_id, action, who.strip()[:60], now))
            self.db.commit()

    def clear(self) -> None:
        """Forget everything. The replay calls this when it starts again."""
        with self.lock:
            self.db.executescript("DELETE FROM notices; DELETE FROM acks; DELETE FROM history;")
            self.notices, self.hits = {}, {}

    def state(self, now: float) -> dict:
        with self.lock:
            acks = {(n, p): {"action": a, "who": w, "at": at} for n, p, a, w, at in self.db.execute("SELECT * FROM acks")}
            active = sorted((n for n in self.notices.values() if n.published_at <= now < n.published_at + self.active_days * DAY),
                            key=lambda n: (n.published_at, n.detected_at), reverse=True)
            hits, level = [], CLEAR
            for n in active:
                for pid, lv, why in self.hits[n.id]:
                    ack = acks.get((n.id, pid))
                    confidence, basis = match_evidence(lv, why)
                    hits.append({"notice": n.id, "product": pid, "product_name": self.by_id[pid].name, "level": lv,
                                 "level_name": LEVEL_NAMES[lv], "why": why, "confidence": confidence,
                                 "match_basis": basis, "official": n.official, "ack": ack})
                    if not ack:
                        level = max(level, lv)
            hits.sort(key=lambda h: (h["ack"] is not None, -h["level"]))
            last = self.db.execute("SELECT t, level FROM history ORDER BY rowid DESC LIMIT 1").fetchone()
            if not last or last[1] != level:
                self.db.execute("INSERT INTO history VALUES (?, ?)", (now, level))
                self.db.commit()
                last = (now, level)
            open_hits = [h for h in hits if not h["ack"]]
            return {
                "now": now, "level": level, "level_name": LEVEL_NAMES[level], "since": last[0],
                "answer": answer(level, open_hits, self.notices),
                "hits": hits[:200], "hit_count": len(open_hits),
                "blocked": sorted({h["product"] for h in open_hits if h["level"] == QUARANTINE}),
                "notices": [{**asdict(n), "age_s": now - n.detected_at, "hits": len(self.hits[n.id])} for n in active],
                "year": [{"id": n.id, "title": n.title, "t": n.published_at, "hazard": n.hazard,
                          "allergens": n.allergens, "level": max((lv for _, lv, _ in self.hits[n.id]), default=CLEAR)}
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
        return f"Quarantine {top['product_name']} and verify the lot on the pack. {n.summary}{risk}{lot}"
    return f"Check {top['product_name']}. A recall names a product like it ({top['why']}). {n.summary}{risk}{lot}"
