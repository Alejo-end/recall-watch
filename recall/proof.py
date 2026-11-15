"""The controlled live proof: a public test page that the team owns, changed during the demo.

    uv run python -m recall.proof setup     # publish the test page as a public GitHub gist, open the test case
    uv run python -m recall.proof status    # print the page address and its last known state

The real cases show what Context.dev saw on retailer pages, but nobody can make a retailer change a
page during judging. This module closes that gap with a page we control:

    the team changes the test page (one GitHub gist revision)
      -> Context.dev runs the page monitor now (POST /v1/monitors/{id}/run)
      -> the monitor reports a change
      -> the normal Recall Watch loop re-reads the page with a fresh structured scrape
      -> a new observation with its own evidence receipt
      -> the review queue updates: a resolved page re-opens, or an open page closes its purchase path

The test case lives in its own database (data/proof.sqlite). It never shares a case, a row, a chart
or an export with the real retailer evidence. The page and the case say on their face that they are
a test. The product, its EAN and the notice are invented: the EAN uses the 200 prefix, which GS1
reserves for in-store numbering, so it can never name a real product.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import httpx

from .afterimage_live import HELSINKI, LiveCases, Refused
from .cases import STATE_LABEL, CaseStore

log = logging.getLogger("recall.proof")

PRODUCT = "Demo Chili 400 g"
GTIN = "2000000000015"     # GS1 prefix 200: in-store numbers only, never a real product
FILENAME = "test-shop-demo-chili.md"
# The description shows on the page. "Not a real product" there makes the classifier answer "unverified".
DESCRIPTION = "Demo page of a Junction 2026 team, edited live during its demo."
NOTICE_ID = "test:recall-watch-controlled-proof"
STATES = {"on_sale": "for sale", "recall": "withdrawn with a notice"}
# Run statuses that are still in progress. Anything else is a finished run.
RUNNING = {"queued", "pending", "running", "in_progress", "started"}


def version_line(state: str, at: float) -> str:
    """The last line of the page. Its time to the second tells one revision from the next."""
    return f"Page version: {STATES[state]}, edited {datetime.fromtimestamp(at, HELSINKI).strftime('%d %b %H:%M:%S')} Helsinki time."


def public_page(url: str) -> str:
    """The test page as anyone on the internet sees it, without our GitHub login."""
    r = httpx.get(url, timeout=15, follow_redirects=True, headers={"Cache-Control": "no-cache"})
    r.raise_for_status()
    return r.text


def page_markdown(state: str, at: float) -> str:
    """The test page. The words follow the patterns a real shop uses, so the normal classifier reads it.

    The "on sale" version avoids the words "recall" and "withdrawn" on purpose: the label at the top
    must not by itself look like a recall message.
    """
    head = (f"# Test shop: {PRODUCT}\n\n"
            "> Demo page of a Junction 2026 team, edited live during its demo. This is not a real shop.\n\n"
            f"**{PRODUCT}**, frozen, chopped\n\nEAN {GTIN}\n\n")
    if state == "on_sale":
        body = "Price 2,49 €\n\nIn stock. Add to cart.\n\n"
    elif state == "recall":
        body = ("**Recall: do not eat this product.** The product is withdrawn from sale. "
                "Return it to the shop for a refund.\n\nNot for sale.\n\n")
    else:
        raise ValueError(f"unknown test page state {state!r}")
    return head + body + f"_{version_line(state, at)}_\n"


def github_token() -> str:
    token = os.environ.get("GITHUB_TOKEN", "")
    if token:
        return token
    try:
        return subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


class Gist:
    """One public GitHub gist with one Markdown file: the test page."""

    def __init__(self, token: str | None = None, transport: httpx.BaseTransport | None = None):
        token = token if token is not None else github_token()
        if not token:
            raise RuntimeError("No GitHub token: set GITHUB_TOKEN or run `gh auth login`.")
        self.http = httpx.Client(base_url="https://api.github.com", transport=transport, timeout=20,
                                 headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                                          "User-Agent": "recall-watch-proof"})

    def create(self, content: str) -> dict:
        r = self.http.post("/gists", json={"description": DESCRIPTION, "public": True,
                                           "files": {FILENAME: {"content": content}}})
        r.raise_for_status()
        return self._out(r.json())

    def update(self, gist_id: str, content: str) -> dict:
        r = self.http.patch(f"/gists/{gist_id}", json={"description": DESCRIPTION, "files": {FILENAME: {"content": content}}})
        r.raise_for_status()
        return self._out(r.json())

    @staticmethod
    def _out(g: dict) -> dict:
        history = g.get("history") or [{}]
        return {"id": g["id"], "url": g["html_url"], "version": history[0].get("version") or ""}


class Proof:
    """The test case, its page and the record of the latest proof run."""

    def __init__(self, live: LiveCases, state_path: Path, gist_factory=Gist, sleep=time.sleep,
                 run_timeout_s: float = 150, change_timeout_s: float = 60, poll_s: float = 3,
                 page_fetcher=public_page, publish_timeout_s: float = 45, max_runs: int = 3, retry_s: float = 15):
        self.live, self.state_path, self.gist_factory, self.sleep = live, Path(state_path), gist_factory, sleep
        self.page_fetcher, self.publish_timeout_s = page_fetcher, publish_timeout_s
        self.max_runs, self.retry_s = max_runs, retry_s
        self.run_timeout_s, self.change_timeout_s, self.poll_s = run_timeout_s, change_timeout_s, poll_s
        self.lock = threading.Lock()
        self.run: dict | None = None
        self.running = False

    # Stored page state ------------------------------------------------------
    def saved(self) -> dict:
        try:
            return json.loads(self.state_path.read_text())
        except (OSError, ValueError):
            return {}

    def _save(self, **fields) -> dict:
        data = {**self.saved(), **fields}
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(data, indent=2))
        return data

    @property
    def page(self) -> dict:
        return self.saved()

    @property
    def set_up(self) -> bool:
        return bool(self.page.get("gist_id"))

    def case(self) -> dict | None:
        return next((c for c in self.live.store.cases() if c["notice_url"] == NOTICE_ID), None)

    def target(self) -> dict | None:
        c = self.case()
        return (self.live.store.targets(c["id"]) or [None])[0] if c else None

    # Setup ------------------------------------------------------------------
    def setup(self, now: float, who: str = "Recall Watch team") -> dict:
        """Publish the test page (once) and open the test case with the page as its only target."""
        page = self.page
        if not page.get("gist_id"):
            g = self.gist_factory().create(page_markdown("on_sale", now))
            page = self._save(gist_id=g["id"], url=g["url"], state="on_sale", version=g["version"], changed_at=now)
        case = self.live.store.open_case({
            "notice_url": NOTICE_ID, "publisher": "Recall Watch team (test notice)",
            "title_fi": f"Testi: {PRODUCT} vedetään takaisin", "title_en": f"Test notice: {PRODUCT} is withdrawn",
            "translation_provenance": "Written by the team for the controlled test. Not an official notice.",
            "products": [PRODUCT], "gtins": [GTIN], "lot": "",
            "notice_date": datetime.fromtimestamp(now, HELSINKI).date().isoformat(), "notice_at": now,
            "notice_at_basis": "Test notice: the clock starts when the team opened the test case",
            "sla_hours": 4.0, "opened_at": now, "opened_by": who})
        if not self.live.store.targets(case["id"]):
            self.live.add_target(case["id"], page["url"], "Test shop (team-owned)", "Test page, not a retailer", who, now,
                                 "pilot_configuration", "A public GitHub gist that the team owns and edits during the demo")
        return {"page": self.page, "case": self.live.store.case(case["id"]), "target": self.target()}

    # The proof run ----------------------------------------------------------
    def status(self, now: float) -> dict:
        case, target = self.case(), self.target()
        view = self.live.store.view(case["id"], now) if case else None
        row = view["targets"][0] if view and view["targets"] else None
        mid = (target or {}).get("monitor_id") or ""
        return {"set_up": self.set_up and bool(target), "configured": self.live.configured, "page": self.page,
                "case": view["case"] if view else None, "row": row, "running": self.running, "run": self.run,
                "monitor": self.live.monitor_status.get(mid) if mid else None, "now": now,
                "next_state": "recall" if self.page.get("state") == "on_sale" else "on_sale", "states": STATES}

    def start(self, to: str, who: str, now: float) -> dict:
        if to not in STATES:
            raise Refused("Choose the page version: on_sale or recall.")
        if not who.strip():
            raise Refused("Type your name in the header first.")
        target = self.target()
        if not (self.set_up and target):
            raise Refused("The test page is not set up. Run: uv run python -m recall.proof setup", 409)
        if not target.get("monitor_id"):
            raise Refused("The test page has no Context.dev monitor yet. Try again in a minute.", 409)
        with self.lock:
            if self.running:
                raise Refused("A proof run is in progress. Wait for it to finish.", 409)
            self.running = True
            self.run = {"to": to, "who": who.strip(), "started_at": now, "steps": [], "finished_at": None, "ok": None}
        threading.Thread(target=self._run, args=(dict(target), to), daemon=True).start()
        return self.run

    def _step(self, key: str, label: str, detail: str = "", status: str = "done", **extra) -> dict:
        step = {"key": key, "label": label, "detail": detail, "status": status, "at": time.time(), **extra}
        steps = self.run["steps"]
        for i, s in enumerate(steps):
            if s["key"] == key:
                steps[i] = step
                return step
        steps.append(step)
        return step

    def _run(self, target: dict, to: str) -> None:
        tid, mid, client = target["id"], target["monitor_id"], self.live.client
        before = {o["id"] for o in self.live.store.observations(tid)}
        try:
            t0 = time.time()
            g = self.gist_factory().update(self.page["gist_id"], page_markdown(to, t0))
            self._save(state=to, version=g["version"], changed_at=t0)
            self._step("page", "The team changed the test page",
                       f"Now {STATES[to]}. GitHub gist revision {g['version'][:7]}. Waiting until the public page shows it.",
                       "running", url=g["url"])
            # GitHub can serve the old page for a few seconds after an edit. A monitor run in that
            # window compares the old page with itself and reports no change, so wait for the new one.
            shown = self._wait_public(g["url"], version_line(to, t0))
            self._step("page", "The team changed the test page",
                       f"Now {STATES[to]}. GitHub gist revision {g['version'][:7]}, "
                       + (f"public after {shown - t0:.0f} s." if shown else "not yet visible at the public address."),
                       "done" if shown else "fallback", url=g["url"])

            # A run that starts right after the edit can still read the old page and report no change.
            # The app then runs the monitor again, up to `max_runs` times, and says so on the screen.
            run, run_id, misses = None, "", 0
            for attempt in range(self.max_runs):
                run_id = client.run_monitor(mid)
                self._step("monitor", "Context.dev monitor runs now", f"Run {run_id} queued for monitor {mid}"
                           + (f" (attempt {attempt + 1}; the earlier run saw no change yet)." if attempt else "."),
                           "running", run_id=run_id)
                run = self._wait_for_run(mid, run_id)
                if run is None or run.get("change_detected"):
                    break
                misses += 1
                if attempt + 1 < self.max_runs:
                    self.sleep(self.retry_s)
            detected = bool(run and run.get("change_detected"))
            earlier = f" {misses} earlier run{'s' if misses > 1 else ''} still saw the old page." if misses else ""
            if run is None:
                self._step("monitor", "Context.dev monitor run", f"Run {run_id} did not finish within "
                           f"{int(self.run_timeout_s)} s.", "failed", run_id=run_id)
            elif detected:
                self._step("monitor", "Context.dev detected the change",
                           f"Run {run_id} finished: change {run.get('change_id') or '(id pending)'} detected.{earlier}",
                           run_id=run_id, change_id=run.get("change_id") or "")
            else:
                why = run.get("skip_reason") or "no change reported"
                self._step("monitor", "Context.dev monitor run finished",
                           f"{misses} run{'s' if misses > 1 else ''}, last {run_id}: {why}.", "failed", run_id=run_id)

            obs = None
            self.live.next_monitor_check = 0     # the next poll refreshes the monitor's runs for the receipt
            if detected:
                # The normal loop reads the account's changes and re-scrapes the page with the change id.
                self._step("scrape", "Fresh structured scrape", "Waiting for the change in Context.dev's change feed.", "running")
                deadline = time.time() + self.change_timeout_s
                while obs is None and time.time() < deadline:
                    self.live.poll(time.time())
                    change_id = run.get("change_id") or ""
                    obs = next((o for o in reversed(self.live.store.observations(tid)) if o["id"] not in before
                                and (not change_id or o.get("change_id") == change_id)), None)
                    if obs is None:
                        self.sleep(self.poll_s)
            if obs is None:
                # Honest fallback: a direct read, labelled as such, so the run still ends with evidence.
                obs = self.live.scrape(tid, time.time(), "proof_fallback")
                self._step("scrape", "Direct structured scrape (fallback)",
                           "The monitor path did not deliver a re-read in time, so Recall Watch read the page directly.",
                           "fallback", **self._call(obs))
            else:
                self._step("scrape", "Fresh structured scrape after the monitor change",
                           f"Triggered by change {obs.get('change_id') or '?'}.", **self._call(obs))
            full = self.live.store.observation(obs["id"])
            self._step("receipt", "New evidence receipt",
                       f"Observation {full['id']}: {STATE_LABEL.get(full['state'], full['state'])}. "
                       f"“{(full.get('excerpt') or full.get('evidence') or '')[:140]}”", observation_id=full["id"])
            row = self.status(time.time())["row"] or {}
            self._step("queue", "Review queue updated",
                       f"{row.get('state_label', '')} · {('re-opened' if row.get('reopened') else row.get('review', ''))}"
                       f" · {row.get('next_step', '')}", review=row.get("review"), needs_action=row.get("needs_action"))
            self.run["ok"] = detected and full["trigger"] == "monitor_change"
        except Refused as e:
            self._step("error", "Stopped", str(e), "failed")
            self.run["ok"] = False
        except Exception as e:
            log.exception("proof run failed")
            self._step("error", "Stopped", f"{type(e).__name__}. The log on the demo machine has the detail.", "failed")
            self.run["ok"] = False
        finally:
            self.run["finished_at"] = time.time()
            self.running = False

    @staticmethod
    def _call(obs: dict) -> dict:
        return {"request_id": obs.get("request_id") or "", "cache": obs.get("cache") or "",
                "extract_ms": obs.get("extract_ms"), "observation_id": obs["id"],
                "detail_call": f"request {obs.get('request_id') or '?'} · cache {obs.get('cache') or '?'} · "
                               f"{obs.get('extract_ms') or '?'} ms"}

    def _wait_public(self, url: str, marker: str) -> float | None:
        deadline = time.time() + self.publish_timeout_s
        while time.time() < deadline:
            try:
                if marker in self.page_fetcher(url):
                    return time.time()
            except Exception:
                log.warning("reading the public test page failed")
            self.sleep(2)
        return None

    def _wait_for_run(self, mid: str, run_id: str) -> dict | None:
        deadline = time.time() + self.run_timeout_s
        while time.time() < deadline:
            self.sleep(self.poll_s)
            try:
                run = next((r for r in self.live.client.monitor_runs(mid, limit=10) if r.get("id") == run_id), None)
            except Exception:
                log.warning("reading runs of %s failed", mid)
                continue
            if run and (run.get("status") or "").lower() not in RUNNING:
                return run
        return None


def proof_config(cfg: dict) -> dict:
    """The guard rails of the test case: only the gist host, one page, no list reading, no photos."""
    return {"allowed_domains": ["gist.github.com"], "sla_hours": 4, "max_cases": 1, "max_targets_per_case": 1,
            "create_monitors": True, "monitor_interval_minutes": float(cfg.get("monitor_interval_minutes", 10)),
            "recheck_minutes": 24 * 60, "min_rescrape_seconds": 30, "scrape_budget_per_hour": 20,
            "auto_open_cases": False, "read_notice_photos": False}


def main(argv: list[str]) -> None:
    from .config import ROOT, load_env, load_watch
    load_env()
    store = CaseStore(str(ROOT / "data" / "proof.sqlite"), artifact_dir=str(ROOT / "data" / "proof-artifacts"))
    proof = Proof(LiveCases(store, proof_config(load_watch().get("afterimage", {}))), ROOT / "data" / "proof.json")
    cmd = argv[0] if argv else "status"
    if cmd == "setup":
        out = proof.setup(time.time())
        print(f"Test page: {out['page']['url']}\nCase: {out['case']['id']}\nPage monitor: {out['target']['monitor_id'] or 'not created'}")
    elif cmd == "status":
        page = proof.page
        print(json.dumps({"page": page, "target": proof.target()}, indent=2) if page else "Not set up. Run: uv run python -m recall.proof setup")
    else:
        sys.exit("Usage: python -m recall.proof setup|status")


if __name__ == "__main__":
    main(sys.argv[1:])
