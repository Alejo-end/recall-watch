import hashlib
import hmac
import json
import os
import time

os.environ["RECALL_NO_POLL"] = "1"
os.environ["SOURCE"] = "replay"
os.environ.pop("CONTEXT_DEV_API_KEY", None)
os.environ.pop("SHELF", None)

from fastapi.testclient import TestClient  # noqa: E402

from recall import app as appmod  # noqa: E402
from recall.engine import Notice  # noqa: E402
from recall.replay import ts  # noqa: E402

client = TestClient(appmod.app)


def test_the_page_and_the_state_load():
    assert client.get("/").status_code == 200
    s = client.get("/state").json()
    assert s["shelf_size"] == 12 and s["level"] == 0
    assert s["status"]["source"] == "replay"
    assert s["status"]["stale"] is True    # no poll ran in this test, and the page says so


def test_the_replay_shows_a_hit_and_a_person_can_record_an_action(monkeypatch):
    src = appmod.ReplaySource(time.time())
    monkeypatch.setattr(appmod, "source", src)
    monkeypatch.setattr(src, "now", lambda real: ts("2026-02-12"))
    monkeypatch.setattr(src.replay, "now", lambda real: (ts("2026-02-12"), 0))
    src.poll(time.time())
    s = client.get("/state").json()
    assert s["level_name"] == "quarantine" and s["hits"][0]["product"] == "coleslaw"
    assert s["hits"][0]["confidence"] == "high" and s["hits"][0]["match_basis"] == "EAN code"
    blocked = client.get("/blocked").json()["products"]
    assert [p["ean"] for p in blocked] == ["6407860065295"] and "ruokavirasto.fi" in blocked[0]["notice"]
    assert blocked[0]["action"] == "hold_and_verify_lot"
    body = {"notice": s["hits"][0]["notice"], "product": "coleslaw", "action": "quarantined", "who": "Aino"}
    assert client.post("/ack", json={**body, "who": ""}).status_code == 422
    assert client.post("/ack", json={**body, "product": "butter"}).status_code == 404
    assert client.post("/ack", json=body).json() == {"ok": True}
    assert client.get("/state").json()["level_name"] == "clear" and client.get("/blocked").json()["products"] == []
    appmod.board.clear()


class FakeClient:
    """Stands in for Context.dev: one list item, one web result, and the same change on every poll."""

    def __init__(self):
        self.read = []

    def monitors(self):
        return [{"id": "mon_list", "name": "Recall Watch: Ruokavirasto recall list"},
                {"id": "mon_web", "name": "Recall Watch: recalls announced on the web"}]

    def changes(self, since, kinds):   # `since` is inclusive: the newest changes come back each time
        return [{"id": "chg_1", "kind": "list", "monitor_id": "mon_list", "detected_at": "2026-11-14T10:00:00Z"},
                {"id": "chg_2", "kind": "web", "monitor_id": "mon_web", "detected_at": "2026-11-14T10:01:00Z", "title": "Recall",
                 "matched_urls": ["https://www.example.fi/recall", "https://www.ruokavirasto.fi/elintarvikkeet/x/"]}]

    def recall_list(self, url):
        return [{"date": "2026-11-13", "title": "Coleslaw", "url": "https://www.ruokavirasto.fi/elintarvikkeet/a/"},
                {"date": "2025-12-30", "title": "Last year", "url": "https://www.ruokavirasto.fi/elintarvikkeet/old/"}]

    def read_notice(self, item, now, official=True):
        self.read.append(item["url"])
        return Notice(id=appmod.notice_id(item["url"]), url=item["url"], title=item["title"], date="2026-11-13",
                      published_at=now - 3600, detected_at=now, official=official, brand="Atria", products=["Atria Coleslaw 300 g"])

    def news(self, query, since):
        return [{"id": "news:1", "title": "Recall in the news", "url": "https://n.fi/1", "source": "n.fi", "published_at": since}]


def test_a_live_poll_reads_each_new_notice_once(monkeypatch):
    monkeypatch.setattr(appmod, "KINDS", {})
    fake, now = FakeClient(), 1_794_650_000.0
    live = appmod.LiveSource(now, client=fake)
    live.kinds = appmod.KINDS
    live.poll(now)
    # The list notice of this year, and the web result. Not last year's notice, and not the authority's page again.
    assert fake.read == ["https://www.ruokavirasto.fi/elintarvikkeet/a/", "https://www.example.fi/recall"]
    s = appmod.board.state(now)
    assert s["level_name"] == "quarantine" and len(s["notices"]) == 2
    assert {h["level_name"] for h in s["hits"]} == {"quarantine", "check"}    # the web result gives check at most
    assert s["notices"][0]["monitor_detected_at"] is not None
    assert appmod.status["news"][0]["source"] == "n.fi"
    live.poll(now + 120)
    assert len(fake.read) == 2                                                  # nothing is read a second time
    appmod.board.clear()


def test_an_order_with_an_recalled_ean_is_held_for_lot_verification(monkeypatch):
    now = ts("2026-02-12")
    appmod.board.add(Notice(id="order-notice", url="https://www.ruokavirasto.fi/order", title="Coleslaw", date="2026-02-10",
                            published_at=ts("2026-02-10"), detected_at=now, eans=["6407860065295"]))
    monkeypatch.setattr(appmod, "clock", lambda: now)
    r = client.post("/orders/check", json={"items": [
        {"id": "same", "name": "Atria Coleslaw 300 g", "ean": "6407860065295"},
        {"id": "other", "name": "Myllyn Paras makaroni 400 g", "ean": "6410000000000"},
    ]})
    assert r.status_code == 200
    assert [d["decision"] for d in r.json()["decisions"]] == ["hold_and_verify_lot", "allow"]
    assert r.json()["decisions"][0]["evidence"][0]["confidence"] == "high"
    appmod.board.clear()


def test_a_signed_webhook_of_our_monitor_wakes_the_poller(monkeypatch):
    monkeypatch.setenv("CONTEXT_WEBHOOK_SECRET", "whsec_t")
    monkeypatch.setattr(appmod, "KINDS", {"mon_1": "list"})
    appmod.WAKE.clear()
    for mon, used in (("mon_9", False), ("mon_1", True)):
        body = json.dumps({"id": "evt", "event": "change.detected", "data": {"change": {"id": "c", "monitor_id": mon}}}).encode()
        t = int(time.time())
        sig = f"t={t},v1=" + hmac.new(b"whsec_t", f"{t}.".encode() + body, hashlib.sha256).hexdigest()
        r = client.post("/webhooks/context", content=body, headers={"X-Context-Signature": sig})
        assert r.status_code == 200 and r.json()["used"] is used and appmod.WAKE.is_set() is used
    bad = client.post("/webhooks/context", content=b"{}", headers={"X-Context-Signature": "t=1,v1=" + "0" * 64})
    assert bad.status_code == 401
