import time
from datetime import datetime, timezone

import pytest

from recall.afterimage_live import LiveCases, Refused
from recall.cases import CaseStore
from recall.proof import GTIN, Proof, page_markdown, proof_config


class FakeGist:
    created, updated = [], []

    def create(self, content):
        FakeGist.created.append(content)
        return {"id": "g1", "url": "https://gist.github.com/team/g1", "version": "v0abcdef"}

    def update(self, gist_id, content):
        FakeGist.updated.append(content)
        return {"id": gist_id, "url": "https://gist.github.com/team/g1", "version": f"v{len(FakeGist.updated)}abcdef"}


class FakeContext:
    """Context.dev as the proof sees it: a monitor run that does or does not report a change."""

    def __init__(self, detects=True):
        self.state, self.detects, self.runs, self.change, self.scrapes = "recall_message", detects, [], None, 0

    def observe_public_page(self, url, product, gtin):
        self.scrapes += 1
        excerpt = "Not for sale." if self.state != "active_listing" else "In stock. Add to cart."
        return {"state": self.state, "evidence": f"{self.state} seen", "page_title": "Test shop", "extract_ms": 800,
                "product": {"name": "Demo Chili 400 g"}, "markdown": f"Demo Chili 400 g\n\nEAN {GTIN}\n\n{excerpt}",
                "excerpt": excerpt, "request_id": f"req_{self.scrapes}", "cache": "miss", "credits": 6}

    def create_page_monitor(self, name, url, product):
        return {"id": "mon_proof"}

    def run_monitor(self, mid):
        run = {"id": f"run_{len(self.runs) + 1}", "status": "completed", "change_detected": self.detects,
               "change_id": "chg_1" if self.detects else None}
        self.runs.append(run)
        if self.detects:
            self.state = "active_listing"
            self.change = {"id": "chg_1", "monitor_id": mid,
                           "detected_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
        return run["id"]

    def monitor_runs(self, mid, limit=6):
        return list(reversed(self.runs))

    def changes(self, since, kinds):
        return [self.change] if self.change and self.change["monitor_id"] in kinds else []

    def monitor(self, mid):
        return {"id": mid, "status": "active"}


def proof(tmp_path, detects=True):
    FakeGist.created, FakeGist.updated = [], []
    fake = FakeContext(detects)
    live = LiveCases(CaseStore(), proof_config({}), client_factory=lambda: fake)
    return Proof(live, tmp_path / "proof.json", gist_factory=FakeGist, sleep=lambda s: None,
                 run_timeout_s=5, change_timeout_s=5, page_fetcher=lambda url: FakeGist.updated[-1]), fake


def finish(p):
    deadline = time.time() + 5
    while p.running and time.time() < deadline:
        time.sleep(0.01)
    assert not p.running


def test_the_page_labels_itself_as_a_test_and_only_the_recall_version_reads_as_a_recall():
    sale, recall = page_markdown("on_sale", 0), page_markdown("recall", 0)
    assert "Demo page" in sale and "not a real shop" in sale and GTIN in sale and "Add to cart" in sale
    assert "recall" not in sale.lower() and "withdrawn" not in sale.lower()
    assert "Recall: do not eat" in recall and "Add to cart" not in recall
    with pytest.raises(ValueError):
        page_markdown("sold_out", 0)


def test_setup_publishes_the_page_once_and_watches_it_in_its_own_case(tmp_path):
    p, fake = proof(tmp_path)
    out = p.setup(time.time())
    assert len(FakeGist.created) == 1 and out["page"]["state"] == "on_sale"
    assert out["target"]["monitor_id"] == "mon_proof" and fake.scrapes == 1
    assert out["case"]["publisher"].endswith("(test notice)") and out["case"]["gtins"] == [GTIN]
    p.setup(time.time())
    assert len(FakeGist.created) == 1 and len(p.live.store.cases()) == 1 and fake.scrapes == 1


def test_a_page_change_runs_the_monitor_rescrapes_and_reopens_the_resolved_page(tmp_path):
    p, fake = proof(tmp_path)
    p.setup(time.time())
    tid = p.target()["id"]
    p.live.store.act(tid, "Aino", "resolved", "", time.time(), "page_updated")
    assert p.status(time.time())["row"]["review"] == "resolved"

    p.start("on_sale", "Aino", time.time())
    finish(p)
    run = p.status(time.time())["run"]
    assert run["ok"] is True, run["steps"]
    assert [s["key"] for s in run["steps"]] == ["page", "monitor", "scrape", "receipt", "queue"]
    assert run["steps"][1]["change_id"] == "chg_1" and run["steps"][2]["request_id"] == "req_2"
    obs = p.live.store.observations(tid)
    assert [o["trigger"] for o in obs] == ["added", "monitor_change"] and obs[-1]["change_id"] == "chg_1"
    row = p.status(time.time())["row"]
    assert row["state"] == "active_listing" and row["reopened"] and row["needs_action"]
    assert p.page["state"] == "on_sale" and "Add to cart" in FakeGist.updated[-1]


def test_without_a_reported_change_the_run_ends_with_a_labelled_direct_read(tmp_path):
    p, fake = proof(tmp_path, detects=False)
    p.setup(time.time())
    p.start("recall", "Aino", time.time())
    finish(p)
    run = p.run
    assert run["ok"] is False
    assert {s["key"]: s["status"] for s in run["steps"]}["monitor"] == "failed"
    assert next(s for s in run["steps"] if s["key"] == "scrape")["status"] == "fallback"
    assert p.live.store.observations(p.target()["id"])[-1]["trigger"] == "proof_fallback"


def test_a_run_needs_a_name_a_set_up_page_and_no_other_run(tmp_path):
    p, fake = proof(tmp_path)
    with pytest.raises(Refused):
        p.start("recall", "Aino", time.time())       # not set up
    p.setup(time.time())
    with pytest.raises(Refused):
        p.start("recall", " ", time.time())
    with pytest.raises(Refused):
        p.start("sold_out", "Aino", time.time())
    p.running = True
    with pytest.raises(Refused):
        p.start("recall", "Aino", time.time())


def test_the_monitor_runs_only_after_the_public_page_shows_the_new_version(tmp_path):
    p, fake = proof(tmp_path)
    p.setup(time.time())
    p.page_fetcher, p.publish_timeout_s = (lambda url: "the old page"), 0.05
    p.start("recall", "Aino", time.time())
    finish(p)
    assert p.run["steps"][0]["status"] == "fallback" and "not yet visible" in p.run["steps"][0]["detail"]


def test_a_run_that_still_sees_the_old_page_is_followed_by_another_run(tmp_path):
    p, fake = proof(tmp_path)
    p.setup(time.time())
    real_run, calls = fake.run_monitor, []

    def stale_first(mid):
        calls.append(mid)
        if len(calls) == 1:     # the first run reads a cached copy of the old page
            fake.runs.append({"id": "run_stale", "status": "completed", "change_detected": False, "change_id": None})
            return "run_stale"
        return real_run(mid)
    fake.run_monitor = stale_first
    p.start("on_sale", "Aino", time.time())
    finish(p)
    monitor = next(s for s in p.run["steps"] if s["key"] == "monitor")
    assert p.run["ok"] is True and len(calls) == 2
    assert monitor["change_id"] == "chg_1" and "1 earlier run still saw the old page" in monitor["detail"]
