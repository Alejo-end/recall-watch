import hashlib
import hmac
import json
import os
import subprocess
import sys
import time
from pathlib import Path

os.environ["RECALL_NO_POLL"] = "1"
os.environ["SOURCE"] = "replay"
os.environ.pop("CONTEXT_DEV_API_KEY", None)
os.environ.pop("SHELF", None)
os.environ["AFTERIMAGE_DB"] = ":memory:"
os.environ["KITCHEN"] = "1"   # the first prototype's routes and health check are still under test
os.environ["PROOF_DB"] = ":memory:"
os.environ["PROOF_STATE"] = os.path.join(os.path.dirname(__file__), ".no-proof-state.json")   # never the demo machine's page
os.environ.pop("AFTERIMAGE_WRITE_KEY", None)

from fastapi.testclient import TestClient  # noqa: E402

from recall import app as appmod  # noqa: E402
from recall.engine import Notice  # noqa: E402
from recall.replay import ts  # noqa: E402

client = TestClient(appmod.app, client=("127.0.0.1", 50000))   # requests from this machine, as the operator


def _no_key():
    raise RuntimeError("no Context.dev key in tests")


# load_env() reads recall-watch/.env, so a developer key could be present: never spend credits in tests.
appmod.live.client_factory, appmod.live._client = _no_key, None
appmod.proof.live.client_factory, appmod.proof.live._client = _no_key, None
appmod.WRITE_KEY = ""   # a developer .env can hold an operator key; the tests set it themselves when needed


def test_the_page_and_the_state_load():
    assert client.get("/").status_code == 200
    demo = client.get("/afterimage/demo").json()
    assert demo["mode"] == "demo_replay" and demo["simulated"] is True
    assert demo["case"]["publisher"] == "Finnish Food Authority"
    live = client.get("/afterimage/live").json()
    assert live["mode"] == "live" and live["pilot"]["configured"] is False
    fixture = client.get("/fixtures/stock-demo.csv")
    assert fixture.status_code == 200 and "stock_location" in fixture.text
    s = client.get("/state").json()
    assert s["shelf_size"] == 12 and s["level"] == 0
    assert s["status"]["source"] == "replay"
    assert s["status"]["stale"] is True    # no poll ran in this test, and the page says so
    assert client.get("/health").status_code == 503


def test_demo_review_requires_a_named_reviewer_and_a_known_action():
    url = "/afterimage/demo/targets/demo-storefront-a/actions"
    assert client.post(url, json={"who": "", "action": "closed"}).status_code == 422
    assert client.post(url, json={"who": "Aino", "action": "deleted"}).status_code == 422
    recorded = client.post(url, json={"who": "Aino", "action": "escalated", "note": "Opened source"})
    assert recorded.status_code == 200 and recorded.json()["action"]["who"] == "Aino"
    assert client.post("/afterimage/demo/targets/nope/actions", json={"who": "Aino", "action": "closed"}).status_code == 404


def test_live_writes_refuse_before_spending_a_credit():
    case = appmod.live.store.open_case({"notice_url": "https://www.ruokavirasto.fi/x/", "title_fi": "Testi", "notice_date": "2026-10-26",
                                        "notice_at": 0, "notice_at_basis": "test", "sla_hours": 4})
    url = f"/afterimage/live/cases/{case['id']}/targets"
    assert client.post(url, json={"url": "http://www.k-ruoka.fi/x"}).status_code == 422          # not https
    r = client.post(url, json={"url": "https://evil.example/x"})
    assert r.status_code == 403 and "approved list" in r.json()["detail"]
    r = client.post(url, json={"url": "https://www.k-ruoka.fi/kauppa/tuote/x"})
    assert r.status_code == 503 and "not configured" in r.json()["detail"]                   # no key in tests
    assert client.post("/afterimage/live/cases", json={"notice_url": "https://example.com/n"}).status_code == 422


def test_an_operator_key_guards_live_writes(monkeypatch):
    monkeypatch.setattr(appmod, "WRITE_KEY", "s3cret")
    r = client.post("/afterimage/live/cases", json={"notice_url": "https://www.ruokavirasto.fi/x/"})
    assert r.status_code == 401
    assert client.get("/afterimage/live").json()["write_requires_key"] is True


def test_default_replay_starts_just_before_the_checked_demo_match(monkeypatch):
    monkeypatch.delenv("REPLAY_FROM", raising=False)
    src = appmod.ReplaySource(time.time())
    assert src.replay.t0 == ts("2026-02-09", 0)


def test_the_replay_shows_a_hit_and_a_person_can_record_an_action(monkeypatch):
    src = appmod.ReplaySource(time.time())
    monkeypatch.setattr(appmod, "source", src)
    monkeypatch.setattr(src, "now", lambda real: ts("2026-02-12"))
    monkeypatch.setattr(src.replay, "now", lambda real: (ts("2026-02-12"), 0))
    src.poll(time.time())
    s = client.get("/state").json()
    assert s["level_name"] == "quarantine" and s["hits"][0]["product"] == "coleslaw"
    assert s["hits"][0]["confidence"] == "high" and s["hits"][0]["match_basis"] == "EAN code"
    assert s["hits"][0]["stock_location"] == "Cold store · rack 2"
    assert s["hits"][0]["next_service"] == "Next lunch · salad counter"
    blocked = client.get("/blocked").json()["products"]
    assert [p["ean"] for p in blocked] == ["6407860065295"] and "ruokavirasto.fi" in blocked[0]["notice"]
    assert blocked[0]["action"] == "hold_and_verify_lot"
    assert blocked[0]["stock_location"] == "Cold store · rack 2"
    assert blocked[0]["next_service"] == "Next lunch · salad counter"
    body = {"notice": s["hits"][0]["notice"], "product": "coleslaw", "action": "held", "who": "Aino"}
    assert client.post("/ack", json={**body, "who": ""}).status_code == 422
    assert client.post("/ack", json={**body, "product": "butter"}).status_code == 404
    assert client.post("/ack", json=body).json() == {"ok": True}
    held = client.get("/state").json()
    assert held["level_name"] == "quarantine" and held["hits"][0]["case_state"] == "held_pending_verification"
    assert client.get("/blocked").json()["products"]
    assert client.post("/ack", json={**body, "action": "released_not_this_lot", "lot": "LOT-OTHER"}).json() == {"ok": True}
    assert client.get("/state").json()["level_name"] == "clear" and client.get("/blocked").json()["products"] == []
    audit = client.get(f"/audit/{body['notice']}/coleslaw").json()["events"]
    assert [e["action"] for e in audit] == ["held", "released_not_this_lot"] and audit[-1]["lot"] == "LOT-OTHER"
    appmod.board.clear()


def test_a_csv_stock_import_recomputes_current_candidates(monkeypatch):
    now = ts("2026-02-12")
    appmod.board.add(Notice(id="import-notice", url="https://www.ruokavirasto.fi/import", title="Coleslaw", date="2026-02-10",
                            published_at=ts("2026-02-10"), detected_at=now, eans=["6407860065295"]))
    monkeypatch.setattr(appmod, "clock", lambda: now)
    csv = "id,name,ean,stock_location,lot,next_service\nimported,Atria Coleslaw 300 g,6407860065295,Freezer 1,LOT-21,Tuesday lunch\n"
    r = client.post("/imports/stock", json={"csv": csv})
    assert r.status_code == 200 and r.json()["products"] == 1
    s = client.get("/state").json()
    assert s["shelf_size"] == 1 and s["hits"][0]["stock_location"] == "Freezer 1" and s["hits"][0]["imported_lot"] == "LOT-21"
    appmod.board.clear()
    appmod.board.replace_shelf(appmod.SHELF)


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
    assert s["level_name"] == "check" and len(s["notices"]) == 2
    assert {h["level_name"] for h in s["hits"]} == {"check"}  # text and unconfirmed-web matches are review only
    assert s["notices"][0]["monitor_detected_at"] is not None
    assert appmod.status["news"][0]["source"] == "n.fi"
    live.poll(now + 120)
    assert len(fake.read) == 2                                                  # nothing is read a second time
    appmod.board.clear()


def test_a_change_that_fails_is_read_again_on_the_next_poll(monkeypatch):
    monkeypatch.setattr(appmod, "KINDS", {})
    fake, now = FakeClient(), 1_794_650_000.0
    read, failed = fake.read_notice, []

    def fail_once(item, now, official=True):
        if not official and not failed:
            failed.append(item["url"])
            raise RuntimeError("no answer")
        return read(item, now, official)

    fake.read_notice = fail_once
    live = appmod.LiveSource(now, client=fake)
    live.kinds = appmod.KINDS
    try:
        live.poll(now)
        assert False
    except RuntimeError:
        pass
    assert "chg_1" in live.seen and "chg_2" not in live.seen and live.since < now
    live.poll(now + 120)
    assert fake.read == ["https://www.ruokavirasto.fi/elintarvikkeet/a/", "https://www.example.fi/recall"]
    assert "chg_2" in live.seen
    appmod.board.clear()


def test_a_notice_that_fails_to_read_does_not_stop_the_others(monkeypatch):
    monkeypatch.setattr(appmod, "KINDS", {})
    fake, now = FakeClient(), 1_794_650_000.0
    lists, read = [], fake.read_notice
    items = [{"date": "2026-11-13", "title": "Broken", "url": "https://www.ruokavirasto.fi/elintarvikkeet/broken/"},
             {"date": "2026-11-12", "title": "Coleslaw", "url": "https://www.ruokavirasto.fi/elintarvikkeet/a/"}]

    def fail_broken(item, now, official=True):
        if item["title"] == "Broken":
            raise RuntimeError("no answer")
        return read(item, now, official)

    fake.read_notice = fail_broken
    fake.changes = lambda since, kinds: []
    fake.recall_list = lambda url: lists.append(url) or items
    live = appmod.LiveSource(now, client=fake)
    live.kinds = appmod.KINDS
    live.poll(now)
    assert fake.read == ["https://www.ruokavirasto.fi/elintarvikkeet/a/"]
    assert [u["url"] for u in appmod.status["unread"]] == ["https://www.ruokavirasto.fi/elintarvikkeet/broken/"]
    live.poll(now + 120)
    assert len(lists) == 1                                                      # the next list check is an hour away
    appmod.board.clear()


def test_a_december_notice_is_read_in_early_january(monkeypatch):
    monkeypatch.setattr(appmod, "KINDS", {})
    fake, now = FakeClient(), ts("2027-01-04")
    fake.changes = lambda since, kinds: []
    fake.recall_list = lambda url: [
        {"date": "2026-12-28", "title": "Coleslaw", "url": "https://www.ruokavirasto.fi/elintarvikkeet/december/"},
        {"date": "2026-11-13", "title": "Too old", "url": "https://www.ruokavirasto.fi/elintarvikkeet/november/"}]
    live = appmod.LiveSource(now, client=fake)
    live.kinds = appmod.KINDS
    live.poll(now)
    assert fake.read == ["https://www.ruokavirasto.fi/elintarvikkeet/december/"]
    appmod.board.clear()


def test_a_database_with_a_bare_file_name_starts(tmp_path):
    root = Path(appmod.__file__).resolve().parent.parent
    env = {**os.environ, "RECALL_DB": "recall-test.sqlite", "PYTHONPATH": str(root)}
    done = subprocess.run([sys.executable, "-c", "import recall.app"], cwd=tmp_path, env=env, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    assert (tmp_path / "recall-test.sqlite").exists()


def test_a_product_that_the_screen_quarantines_is_held_in_an_order(monkeypatch):
    now = ts("2026-02-12")
    appmod.board.add(Notice(id="name-notice", url="https://www.ruokavirasto.fi/name", title="Coleslaw", date="2026-02-10",
                            published_at=ts("2026-02-10"), detected_at=now, brand="Atria", products=["Atria Coleslaw 300 g"]))
    appmod.board.add(Notice(id="web-notice", url="https://www.example.fi/web", title="Butter", date="2026-02-10",
                            published_at=ts("2026-02-10"), detected_at=now, official=False, eans=["6408430000135"]))
    appmod.board.add(Notice(id="ean-notice", url="https://www.ruokavirasto.fi/ean", title="Coleslaw", date="2026-02-10",
                            published_at=ts("2026-02-10"), detected_at=now, eans=["6407860065295"]))
    monkeypatch.setattr(appmod, "clock", lambda: now)
    assert [p["id"] for p in client.get("/blocked").json()["products"]] == ["coleslaw"]
    r = client.post("/orders/check", json={"items": [
        {"id": "coleslaw", "name": "Atria Coleslaw 300 g", "brand": "Atria", "ean": "6407860065295"},
        {"id": "name", "name": "Atria Coleslaw 300 g", "brand": "Atria"},
        {"id": "web", "name": "Butter", "ean": "6408430000135"},
    ]}).json()["decisions"]
    assert [d["decision"] for d in r] == ["hold_and_verify_lot", "review", "review"]
    assert any(e["match_basis"] == "EAN code" and e["level_name"] == "quarantine" for e in r[0]["evidence"])
    assert r[1]["evidence"][0]["match_basis"] == "brand and product name"   # a text match is review only, on the screen too
    assert r[2]["evidence"][0]["match_basis"] == "EAN code"    # the web notice gives check at most, and the basis stays
    appmod.board.clear()


def test_the_blocked_list_holds_more_than_two_hundred_hits(monkeypatch):
    now = ts("2026-02-12")
    for i in range(201):
        appmod.board.add(Notice(id=f"n{i}", url=f"https://www.ruokavirasto.fi/n{i}", title="Coleslaw", date="2026-02-10",
                                published_at=ts("2026-02-10"), detected_at=now, eans=["6407860065295"]))
    monkeypatch.setattr(appmod, "clock", lambda: now)
    assert len(client.get("/blocked").json()["products"]) == 201
    appmod.board.clear()


def test_the_order_check_reads_the_notices_under_the_lock(monkeypatch):
    class CountingLock:
        def __init__(self, lock):
            self.lock, self.entered = lock, 0

        def __enter__(self):
            self.entered += 1
            return self.lock.__enter__()

        def __exit__(self, *exc):
            return self.lock.__exit__(*exc)

    lock = CountingLock(appmod.board.lock)
    monkeypatch.setattr(appmod.board, "lock", lock)
    assert client.post("/orders/check", json={"items": [{"name": "Atria Coleslaw 300 g"}]}).status_code == 200
    assert lock.entered > 0


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


def test_the_demo_drafts_an_owner_message_and_records_the_contact(monkeypatch):
    monkeypatch.setattr(appmod.afterimage, "started_at", time.time() - 30)   # past the first simulated reads
    m = client.get("/afterimage/demo/targets/demo-marketplace-d/owner-message").json()
    assert m["subject"].startswith("Recall check") and "Atria Coleslaw" in m["body"]
    r = client.post("/afterimage/demo/targets/demo-marketplace-d/actions",
                    json={"who": "Aino", "action": "owner_contacted", "recipient": "seller@example.fi", "due_hours": 24})
    assert r.status_code == 200 and r.json()["action"]["recipient"] == "seller@example.fi"
    assert "page_updated" in client.get("/afterimage/demo").json()["outcomes"]


def test_a_case_report_is_a_csv_audit_trail():
    case = appmod.live.store.open_case({"notice_url": "https://www.ruokavirasto.fi/report/", "title_fi": "R", "notice_date": "2026-10-26",
                                        "notice_at": 0, "notice_at_basis": "t", "sla_hours": 4})
    t, _ = appmod.live.store.add_target(case["id"], "https://www.k-ruoka.fi/r", "K-Ruoka", "P", "A", 1)
    appmod.live.store.observe(t["id"], 2, "active_listing", "Price visible", "added")
    r = client.get(f"/afterimage/live/cases/{case['id']}/report.csv")
    assert r.status_code == 200 and r.text.splitlines()[0].startswith("time_utc") and "Price visible" in r.text


def test_the_share_card_gives_the_address_and_only_the_configured_wifi(monkeypatch):
    monkeypatch.delenv("SHARE_WIFI_SSID", raising=False)
    monkeypatch.setenv("SHARE_URL", "http://192.168.1.20:8030/")
    j = client.get("/share").json()
    assert j["url"] == "http://192.168.1.20:8030/" and j["ssid"] == ""
    monkeypatch.setenv("SHARE_WIFI_SSID", "Junction team")
    monkeypatch.setenv("SHARE_WIFI_PASSWORD", "pw")
    assert client.get("/share").json()["ssid"] == "Junction team"


def test_without_an_operator_key_only_this_machine_can_write(monkeypatch):
    monkeypatch.setattr(appmod, "WRITE_KEY", "")
    remote = TestClient(appmod.app, client=("172.20.10.5", 50000))
    for url, body in (("/afterimage/live/cases", {"notice_url": "https://www.ruokavirasto.fi/x/"}),
                      ("/afterimage/live/cases/c-x/targets", {"url": "https://www.k-ruoka.fi/x"}),
                      ("/afterimage/live/targets/t-x/rescrape", {}),
                      ("/afterimage/live/targets/t-x/actions", {"who": "A", "action": "note", "note": "x"})):
        r = remote.post(url, json=body)
        assert r.status_code == 403 and "AFTERIMAGE_WRITE_KEY" in r.json()["detail"], url
    assert remote.get("/afterimage/live").status_code == 200        # reading stays open
    assert client.post("/afterimage/live/targets/t-x/rescrape").status_code == 404   # this machine passes the guard


def test_the_screen_loads_no_third_party_fonts_or_scripts():
    page = client.get("/").text
    assert "googleapis" not in page and "gstatic" not in page and "cdnjs" not in page
    assert client.get("/fonts/fonts.css").status_code == 200 and "gstatic" not in client.get("/fonts/fonts.css").text
    assert client.get("/vendor/qrcode-1.4.4.min.js").status_code == 200


def test_kitchen_writes_need_the_operator_and_the_audit_reads_a_real_notice_id(monkeypatch):
    monkeypatch.setattr(appmod, "WRITE_KEY", "")
    remote = TestClient(appmod.app, client=("172.20.10.5", 50000))
    assert remote.post("/imports/stock", json={"csv": "id,name\\nx,X"}).status_code == 403
    assert remote.post("/ack", json={"notice": "n", "product": "p", "action": "held", "who": "A"}).status_code == 403
    nid = "ruokavirasto.fi/elintarvikkeet/a/b"
    appmod.board.db.execute("INSERT INTO events (notice_id, product_id, action, who, at) VALUES (?, 'coleslaw', 'held', 'Aino', 1)", (nid,))
    assert client.get(f"/audit/{nid}/coleslaw").json()["events"][0]["who"] == "Aino"
    assert client.get("/audit", params={"notice": nid, "product": "coleslaw"}).json()["events"][0]["who"] == "Aino"
