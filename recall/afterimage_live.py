"""The live Recall Watch loop: official notice, approved public pages, Context.dev monitors, re-scrapes.

    official notice (Food Authority list monitor, or an operator opens it)
      -> case with the Finnish title and a recorded machine working translation
      -> suggested pages: Context.dev search, limited to the approved domains
      -> an operator approves a page -> one structured scrape + one Context.dev page monitor
      -> the monitor reports a change -> structured re-scrape -> new observation
      -> the timeline and the review queue update; a page that is listed again re-opens review

Guard rails, because every scrape spends Context.dev credits and every URL is fetched by us:
only approved domains, a cap on pages per case, a minimum interval between scrapes of one page,
an hourly scrape budget, and error messages that never echo upstream detail to the browser.
"""

from __future__ import annotations

import base64
import json
import logging
import threading
import time
from collections import deque
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .cases import CaseStore
from .contextdev import MACHINE_TRANSLATION, ContextDev, domain, parse_ts
from .engine import Notice, name_tokens
from .identity import identity_basis
from .images import ImageRefused, fetch_image, sha256

log = logging.getLogger("recall.afterimage")
HELSINKI = ZoneInfo("Europe/Helsinki")
OFFICIAL_DOMAIN = "ruokavirasto.fi"
MONITOR_PREFIX = "Recall Watch: page"


class Refused(Exception):
    """A request that the guard rails reject. The message is safe to show to the user."""

    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


class Budget:
    """At most `per_hour` paid calls in any sliding hour."""

    def __init__(self, per_hour: int):
        self.per_hour, self.calls = per_hour, deque()
        self.lock = threading.Lock()

    def take(self, now: float) -> bool:
        with self.lock:
            while self.calls and now - self.calls[0] > 3600:
                self.calls.popleft()
            if len(self.calls) >= self.per_hour:
                return False
            self.calls.append(now)
            return True

    def left(self, now: float) -> int:
        with self.lock:
            return self.per_hour - sum(1 for t in self.calls if now - t <= 3600)


def allowed(url: str, domains: list[str]) -> bool:
    host = domain(url)
    return any(host == d or host.endswith("." + d) for d in domains)


def notice_clock(date: str, monitor_detected_at: float | None) -> tuple[float, str]:
    """When the SLA clock starts, and why.

    The authority's page states a date, not a time.  A list-monitor detection gives a minute; when
    there is none, the clock starts at 00:00 Helsinki time on the notice date.  An early start is
    deliberate: it can only make a reported lag longer, never flatter the publisher.
    """
    if monitor_detected_at:
        return monitor_detected_at, "Context.dev list monitor first saw the notice"
    day = datetime.fromisoformat(date).replace(tzinfo=HELSINKI)
    return day.timestamp(), "Notice states a date only: clock starts 00:00 Helsinki time on that date"


class LiveCases:
    def __init__(self, store: CaseStore, cfg: dict, client_factory=ContextDev, known_notice=lambda url: None,
                 image_fetcher=fetch_image,
                 list_url: str = "", list_monitor_name: str = ""):
        self.store, self.cfg = store, cfg
        self.list_url, self.list_monitor_name = list_url, list_monitor_name
        self.image_fetcher = image_fetcher
        self.list_monitor_id: str | None = None
        self.list_seen: set[str] | None = None   # notice URLs on the list at the last read; None before the first read
        self.list_read_at: float | None = None
        self.next_list = 0.0
        self.known_notice = known_notice     # the authority's list entry for a URL, when we hold it
        self.client_factory = client_factory
        self._client = None
        self.domains = [d.lower() for d in cfg.get("allowed_domains", [])]
        self.budget = Budget(int(cfg.get("scrape_budget_per_hour", 60)))
        # A restart does not reset the budget: every paid call of the last hour is stored, whatever its
        # kind (page read, notice read, list read, search). Databases from before the spend log count
        # their observations instead.
        since = time.time() - 3600
        for t in sorted(store.spend_times_since(since) or store.observation_times_since(since)):
            self.budget.calls.append(t)
        self.images_tried: set[str] = set()
        self.monitor_tried: dict[str, float] = {}       # target id to the last monitor creation attempt
        self.first_read_tried: dict[str, float] = {}    # target id to the last retry of a failed first read
        self.monitor_status: dict[str, dict] = {}   # monitor id to its last retrieved state, refreshed by the poll
        self.next_monitor_check = 0.0
        self.status = {"last_poll": None, "last_error": None, "monitor_changes": 0, "scheduled_rescrapes": 0}
        self.since = time.time() - 3600
        # A restart reads the changes of the last hour again. A change that already produced an
        # observation must not produce a second one: that would be a re-read with no new change.
        self.seen_changes: set[str] = store.change_ids()
        self.lock = threading.RLock()

    # Context.dev ------------------------------------------------------------
    @property
    def configured(self) -> bool:
        try:
            self.client
            return True
        except RuntimeError:
            return False

    @property
    def client(self) -> ContextDev:
        if self._client is None:
            self._client = self.client_factory()
        return self._client

    def _spend(self, now: float, kind: str = "read") -> None:
        """One paid Context.dev call. Every call counts once, and is stored so a restart keeps the count."""
        if not self.budget.take(now):
            raise Refused("The hourly Context.dev budget is used up. The page stays watched by its monitor.", 429)
        self.store.record_spend(now, kind)

    # Cases ------------------------------------------------------------------
    def case_from_notice(self, n: Notice, opened_by: str, now: float, clock: tuple[float, str] | None = None) -> dict:
        at, basis = clock or notice_clock(n.date, n.monitor_detected_at)
        case = self.store.open_case({
            "notice_url": n.url, "title_fi": n.title, "title_en": n.title_en,
            "translation_provenance": MACHINE_TRANSLATION if n.title_en else "No English translation was extracted.",
            "products": n.products, "gtins": n.eans, "lot": n.lot, "notice_date": n.date, "images": n.images,
            "notice_at": at, "notice_at_basis": basis, "sla_hours": float(self.cfg.get("sla_hours", 4)),
            "opened_at": now, "opened_by": opened_by})
        self.keep_notice_photos(case)
        return self.store.case(case["id"])

    def open_case(self, notice_url: str, who: str, now: float) -> dict:
        if domain(notice_url) != OFFICIAL_DOMAIN:
            raise Refused("A case starts from an official notice on ruokavirasto.fi.")
        existing = next((c for c in self.store.cases() if c["notice_url"] == notice_url), None)
        if existing:
            return existing
        if len(self.store.cases()) >= int(self.cfg.get("max_cases", 20)):
            raise Refused("The pilot holds at most %d cases." % int(self.cfg.get("max_cases", 20)))
        if not self.configured:
            raise Refused("Live reading is not configured on this server.", 503)
        self._spend(now)
        try:
            n = self.client.read_notice({"url": notice_url, "title": ""}, now)
        except Exception:
            log.exception("reading notice %s failed", notice_url)
            raise Refused("Context.dev could not read that notice. Try again later.", 502)
        listed = self.known_notice(notice_url)
        if listed:   # the authority's own list gives the headline and date; the extraction adds the translation
            n.title, n.date, n.monitor_detected_at = listed.title, listed.date, listed.monitor_detected_at
        if not n.title:
            raise Refused("Context.dev did not find the notice headline on that page.", 502)
        case = self.case_from_notice(n, who or "Operator", now)
        self.suggest(case["id"], now)
        return case

    def suggest(self, case_id: str, now: float) -> list[dict]:
        """Candidate public pages for the recalled products, only from approved domains."""
        case = self.store.case(case_id)
        if not case or not self.domains or not self.configured or not (case["products"] or case["gtins"]):
            return []
        try:
            # A GTIN query finds the exact item on shops that put the code in the address.
            queries = [g.lstrip("0") for g in case["gtins"][:2]] + case["products"][:3]
            for query in queries:
                self._spend(now, "search")      # each search is its own paid call
                for r in self.client.search(query, self.domains, limit=6):
                    if allowed(r["url"], self.domains):
                        self.store.suggest(case_id, r["url"], r["title"], now, query)
        except Refused:
            pass
        except Exception:
            log.exception("search for case %s failed", case_id)
            self.status["last_error"] = "search failed"
        return self.store.suggestions(case_id)

    def ranked_suggestions(self, case_id: str, limit: int = 6) -> list[dict]:
        """Suggestions whose titles name a recalled product most precisely come first.

        A search for one flavour also finds its sister products (another flavour, a BCAA version).
        A title with words that the recalled product name lacks ranks lower, but a person still decides.
        """
        case = self.store.case(case_id)
        names = [name_tokens(p) for p in (case or {}).get("products", [])]

        gtins = [g.lstrip("0") for g in (case or {}).get("gtins", []) if g]

        def score(s: dict) -> float:
            if any(g in s["url"] for g in gtins):
                return 2.0          # the official GTIN is in the address: the same item
            title = name_tokens(s["title"].replace("®", ""))
            return max((len(title & n) / max(1, len(title | n)) for n in names if len(title & n) >= 2), default=0)
        # A title that shares only the brand with the recalled product is a different product: drop it.
        ranked = [(score(s), s) for s in self.store.suggestions(case_id)]
        return [s for sc, s in sorted(ranked, key=lambda x: x[0], reverse=True) if sc >= 0.3][:limit]

    # Targets ----------------------------------------------------------------
    def add_target(self, case_id: str, url: str, publisher: str, page_type: str, who: str, now: float,
                   origin: str = "manual", origin_detail: str = "") -> dict:
        # Under the poll's lock: the poll's "monitor not created yet" retry must not create a second
        # monitor for a page that is being added right now.
        with self.lock:
            return self._add_target(case_id, url, publisher, page_type, who, now, origin, origin_detail)

    def _add_target(self, case_id: str, url: str, publisher: str, page_type: str, who: str, now: float,
                    origin: str, origin_detail: str) -> dict:
        url = url.strip()
        if not url.startswith("https://"):
            raise Refused("Enter a full https:// address.")
        case = self.store.case(case_id)
        if not case:
            raise Refused("No such case.", 404)
        if not allowed(url, self.domains):
            raise Refused("This domain is not on the approved list for the pilot: " + ", ".join(self.domains) + ".", 403)
        if len(self.store.targets(case_id)) >= int(self.cfg.get("max_targets_per_case", 10)):
            raise Refused("This case already watches the maximum number of pages.")
        if not self.configured:
            raise Refused("Live reading is not configured on this server.", 503)
        if origin == "manual":
            hit = next((x for x in self.store.suggestions(case_id) if x["url"] == url), None)
            if hit:   # a suggestion keeps the search that found it
                origin = "suggestion"
                origin_detail = f"Context.dev search for “{hit['query']}” on approved domains, {fmt_hel(hit['found_at'])}"
            else:
                origin_detail = origin_detail or "Pasted by a person"
        target, new = self.store.add_target(case_id, url, publisher.strip() or domain(url), page_type.strip() or "Product page",
                                            who or "Operator", now, origin, origin_detail)
        # The monitor comes first: if the first read fails, Context.dev still watches the page and the
        # poller retries the read. Adding the same page again also retries a missing first read.
        self._monitor(self.store.target(target["id"]), case, now)
        if new or not self.store.observations(target["id"]):
            self.scrape(target["id"], now, trigger="added")
        return self.store.target(target["id"])

    def _monitor(self, target: dict, case: dict, now: float) -> None:
        if not self.cfg.get("create_monitors", True) or target.get("monitor_id"):
            return
        try:
            m = self.client.create_page_monitor(f"{MONITOR_PREFIX} {target['id']}", target["url"],
                                                " / ".join(case["products"][:3]) or case["title_fi"])
            self.store.set_monitor(target["id"], m.get("id") or "")
        except Exception:
            log.exception("creating monitor for %s failed", target["url"])
            self.status["last_error"] = "monitor creation failed; scheduled re-checks continue"

    def scrape(self, tid: str, now: float, trigger: str, change_id: str = "", window_start: float | None = None) -> dict:
        target = self.store.target(tid)
        case = self.store.case(target["case_id"])
        last = self.store.observations(tid)
        min_gap = float(self.cfg.get("min_rescrape_seconds", 300))
        if trigger == "manual" and last and now - last[-1]["at"] < min_gap:
            raise Refused(f"This page was read {int(now - last[-1]['at'])} s ago. Wait {int(min_gap)} s between reads.", 429)
        self._spend(now)
        product = " / ".join(case["products"][:3]) or case["title_fi"]
        try:
            r = self.client.observe_public_page(target["url"], product, ", ".join(case["gtins"]))
        except Exception:
            log.exception("scrape of %s failed", target["url"])
            raise Refused("Context.dev could not read this page. Its monitor keeps watching.", 502)
        basis, detail = identity_basis(case["products"], case["gtins"], target["url"], r.get("product") or {},
                                       r.get("markdown") or "", r.get("page_title") or "")
        shot = r.get("screenshot_jpeg_b64") or ""
        obs = self.store.observe(tid, now, r["state"], r["evidence"], trigger, page_title=r.get("page_title", ""),
                                  change_id=change_id, window_start=window_start, extract_ms=r.get("extract_ms"),
                                  identity=basis, identity_detail=detail, excerpt=r.get("excerpt") or "",
                                  markdown=r.get("markdown") or "", screenshot=base64.b64decode(shot) if shot else b"",
                                  request_id=r.get("request_id") or "", cache=r.get("cache") or "", credits=r.get("credits"),
                                  product=r.get("product") or {})
        self.keep_page_photo(obs)
        return self.store.observation(obs["id"])

    def keep_photo(self, url: str, name_stem: str) -> dict:
        """Download one photo and store it next to the database. Returns the fields to record with it."""
        try:
            data, ext = self.image_fetcher(url)
        except ImageRefused as e:
            return {"local": None, "refused": str(e)}
        except Exception as e:
            log.warning("photo %s not kept: %s", url, e)
            return {"local": None, "refused": type(e).__name__}
        name = f"{name_stem}.{ext}"
        self.store.put_artifact(name, data)
        return {"local": name, "sha256": sha256(data), "bytes": len(data), "kept_at": time.time()}

    def keep_page_photo(self, obs: dict) -> None:
        product = json.loads(obs.get("product") or "{}")
        if product.get("image") and "image_local" not in product:
            kept = self.keep_photo(product["image"], f"obs-{obs['id']}")
            product.update({f"image_{k}": v for k, v in kept.items()})
            self.store.set_observation_product(obs["id"], product)

    def keep_notice_photos(self, case: dict) -> None:
        if not case["images"] or all("local" in i for i in case["images"]):
            return
        images = [i if "local" in i else {**i, **self.keep_photo(i["url"], f"case-{case['id']}-{n}")}
                  for n, i in enumerate(case["images"])]
        self.store.set_images(case["id"], images)

    # The loop ---------------------------------------------------------------
    def monitor_ids(self) -> dict[str, str]:
        return {t["monitor_id"]: t["id"] for t in self.store.targets() if t["monitor_id"]}

    def watch_list(self, now: float, detected_at: float | None = None) -> list[dict]:
        """Read the authority's list and open a case for each recent notice that has none.

        The list gives a date, not a time.  When an earlier read of ours did not show the notice,
        the notice appeared after that read, so the SLA clock can start there instead of at 00:00.
        """
        if not self.list_url:
            return []
        self._spend(now, "list")
        items = self.client.recall_list(self.list_url)
        previous, previous_at = self.list_seen, self.list_read_at
        self.list_seen, self.list_read_at = {i["url"] for i in items}, now
        horizon = (datetime.fromtimestamp(now, HELSINKI).date().toordinal() - int(self.cfg.get("auto_open_days", 3)))
        cased = {c["notice_url"] for c in self.store.cases()}
        opened = []
        for item in items:
            if item["url"] in cased or datetime.fromisoformat(item["date"]).toordinal() < horizon:
                continue
            try:
                self._spend(now)
                n = self.client.read_notice(item, now)
            except Refused:
                break
            except Exception:
                log.exception("reading notice %s failed", item["url"])
                continue
            day_start, _ = notice_clock(item["date"], None)
            clock = None
            if previous is not None and item["url"] not in previous and previous_at and previous_at > day_start:
                clock = (previous_at, f"Not on the authority's list at our read at {fmt_hel(previous_at)}; on it at "
                                      f"{fmt_hel(now)}. The clock starts at the earlier read.")
            if detected_at and item["url"] not in (previous or set()):
                # The Context.dev list monitor saw the change at `detected_at`; the notice appeared after the
                # monitor's previous run, at most one interval earlier. That is the latest safe start.
                start = max(day_start, detected_at - 60 * float(self.cfg.get("monitor_interval_minutes", 10)))
                if not clock or start > clock[0]:
                    clock = (start, f"The Context.dev list monitor saw the change at {fmt_hel(detected_at)}. The clock "
                                    f"starts one monitor interval earlier, at {fmt_hel(start)}.")
            case = self.case_from_notice(n, "Recall Watch list check", now, clock)
            self.suggest(case["id"], now)
            opened.append(self.store.case(case["id"]))
        return opened

    def backfill_images(self, now: float) -> None:
        """Cases opened before photos were read get the official product photos once (5 credits each)."""
        for c in self.store.cases():
            self.keep_notice_photos(c)
            for t in self.store.targets(c["id"]):     # page photos of reads made before photos were kept
                last = self.store.observations(t["id"])
                if last:
                    self.keep_page_photo(last[-1])
            if c["images"] or c["id"] in self.images_tried:
                continue
            self.images_tried.add(c["id"])
            try:
                self._spend(now)
                n = self.client.read_notice({"url": c["notice_url"], "title": c["title_fi"], "date": c["notice_date"]}, now)
                self.store.set_images(c["id"], n.images)
                self.keep_notice_photos(self.store.case(c["id"]))
            except Exception:
                log.exception("reading photos of %s failed", c["notice_url"])

    def _list_monitor(self) -> str | None:
        if self.list_monitor_id is None and self.list_monitor_name:
            self.list_monitor_id = next((m["id"] for m in self.client.monitors() if m.get("name") == self.list_monitor_name), "")
        return self.list_monitor_id or None

    def poll(self, now: float) -> None:
        """React to monitor changes at once; re-read quiet pages on a slow schedule as a fallback."""
        with self.lock:
            by_monitor = self.monitor_ids()
            list_monitor = self._list_monitor() if self.cfg.get("auto_open_cases", True) else None
            kinds = {m: "page" for m in by_monitor} | ({list_monitor: "list"} if list_monitor else {})
            list_now = self.cfg.get("auto_open_cases", True) and now >= self.next_list
            list_detected: float | None = None
            if kinds:
                changes = [c for c in self.client.changes(self.since, kinds) if c["id"] not in self.seen_changes]
                pending: list[float] = []      # detection times of changes that could not be handled yet
                handled_up_to = self.since
                for c in sorted(changes, key=lambda c: parse_ts(c.get("detected_at"), now)):
                    detected = parse_ts(c.get("detected_at"), now)
                    if c.get("monitor_id") == list_monitor:
                        list_now = True        # the authority's list changed: read it now
                        list_detected = max(list_detected or 0, detected)
                        self.seen_changes.add(c["id"])
                        handled_up_to = max(handled_up_to, detected)
                        continue
                    tid = by_monitor.get(c.get("monitor_id"))
                    if tid:
                        # The page changed after the monitor's previous run, at most one interval earlier.
                        window = detected - 60 * float(self.cfg.get("monitor_interval_minutes", 10))
                        try:
                            self.scrape(tid, now, "monitor_change", change_id=c["id"], window_start=window)
                            self.status["monitor_changes"] += 1
                        except Refused as e:
                            # Not marked as seen: the next poll asks for it again and retries the read.
                            self.status["last_error"] = str(e)
                            pending.append(detected)
                            continue
                    self.seen_changes.add(c["id"])
                    handled_up_to = max(handled_up_to, detected)
                # `since` never moves past a change that is still waiting for its read.
                self.since = min([handled_up_to, *pending]) if pending else handled_up_to
            recheck = 60 * float(self.cfg.get("recheck_minutes", 60))
            retry = 60 * float(self.cfg.get("first_read_retry_minutes", 10))
            for t in self.store.targets():
                obs = self.store.observations(t["id"])
                if not t["monitor_id"] and now - self.monitor_tried.get(t["id"], 0) >= retry:
                    self.monitor_tried[t["id"]] = now        # a monitor that failed to start is retried
                    self._monitor(t, self.store.case(t["case_id"]), now)
                due = (now - obs[-1]["at"] >= recheck) if obs else (now - self.first_read_tried.get(t["id"], 0) >= retry)
                if not due:
                    continue
                if not obs:
                    self.first_read_tried[t["id"]] = now     # a page whose first read failed is not left unwatched
                try:
                    self.scrape(t["id"], now, "scheduled" if obs else "added")
                    self.status["scheduled_rescrapes"] += 1
                except Refused as e:
                    self.status["last_error"] = str(e)
            if list_now:
                try:
                    self.status["cases_opened"] = self.status.get("cases_opened", 0) + len(self.watch_list(now, list_detected))
                except Refused as e:
                    self.status["last_error"] = str(e)
                self.next_list = now + 60 * float(self.cfg.get("list_minutes", 60))
            if self.cfg.get("read_notice_photos", True):
                self.backfill_images(now)
            if by_monitor and now >= self.next_monitor_check:
                for mid in by_monitor:
                    try:   # proof that each page monitor really runs: its last and next run, from Context.dev
                        status = {**self.client.monitor(mid), "checked_at": now}
                        try:
                            status["runs"] = self.client.monitor_runs(mid)
                        except Exception:
                            status["runs"] = []
                        self.monitor_status[mid] = status
                    except Exception:
                        log.exception("reading monitor %s failed", mid)
                self.next_monitor_check = now + 600
            self.status["last_poll"] = now

    def summary(self, now: float) -> dict:
        return {"configured": self.configured, "allowed_domains": self.domains,
                "sla_hours": float(self.cfg.get("sla_hours", 4)),
                "max_targets_per_case": int(self.cfg.get("max_targets_per_case", 10)),
                "budget_left_this_hour": self.budget.left(now),
                "monitor_interval_minutes": float(self.cfg.get("monitor_interval_minutes", 10)),
                "recheck_minutes": float(self.cfg.get("recheck_minutes", 60)),
                **self.status}


def fmt_hel(ts: float) -> str:
    return datetime.fromtimestamp(ts, HELSINKI).strftime("%d %b %H:%M")


def iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if ts else None
