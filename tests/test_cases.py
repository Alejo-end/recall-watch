from recall.afterimage_live import LiveCases, Refused, notice_clock
from recall.cases import CaseStore, target_timeline

H = 3600


def obs(at, state, trigger="scheduled", window_start=None):
    return {"at": at, "state": state, "trigger": trigger, "evidence": state, "window_start": window_start}


def test_a_measured_response_is_an_upper_bound_with_its_change_window():
    tl = target_timeline([obs(0.1 * H, "active_listing"), obs(1 * H, "active_listing"),
                          obs(3.5 * H, "recall_message", "monitor_change", window_start=3.3 * H)], [], 0, 4 * H, 5 * H)
    closed = tl["milestones"]["purchase_closed"]
    assert closed["lag_s"] == 3.5 * H and closed["changed_after"] == 3.3 * H and not closed["seen_before_watch"]
    assert tl["sla"] == "met" and not tl["needs_action"]


def test_a_page_closed_before_we_watched_is_not_counted_as_a_measured_lag():
    tl = target_timeline([obs(19 * 24 * H, "not_purchasable", "added")], [], 0, 4 * H, 19 * 24 * H)
    assert tl["milestones"]["purchase_closed"]["seen_before_watch"] and tl["sla"] == "not_measurable"


def test_still_purchasable_after_the_deadline_is_a_breach_and_needs_action_until_escalated():
    rows = [obs(5 * H, "active_listing", "added")]
    assert target_timeline(rows, [], 0, 4 * H, 5 * H)["sla"] == "breached"
    assert target_timeline(rows, [], 0, 4 * H, 5 * H)["needs_action"]
    handled = target_timeline(rows, [{"at": 5.1 * H, "action": "owner_contacted", "who": "A", "note": "", "recipient": "web",
                                      "due_at": 30 * H}], 0, 4 * H, 6 * H)
    assert not handled["needs_action"]
    # The first prototype's button recorded a contact without a message being sent: it is only a draft.
    legacy = target_timeline(rows, [{"at": 5.1 * H, "action": "escalated", "who": "A", "note": ""}], 0, 4 * H, 6 * H)
    assert legacy["review"] == "draft_prepared" and legacy["needs_action"] and "Nothing confirms" in legacy["next_step"]


def test_a_listing_that_returns_after_closure_reopens_review():
    rows = [obs(1 * H, "active_listing"), obs(2 * H, "removed"), obs(6 * H, "active_listing")]
    tl = target_timeline(rows, [{"at": 2.5 * H, "action": "closed", "who": "A", "note": ""}], 0, 4 * H, 6 * H)
    assert tl["relisted"] and tl["reopened"] and tl["needs_action"] and tl["sla"] == "breached"


def test_the_sla_clock_starts_early_when_the_notice_gives_only_a_date():
    at, basis = notice_clock("2026-10-26", None)
    assert "date only" in basis and at == 1792965600       # 00:00 Helsinki; summer time ended on 25 October, so UTC+2
    assert notice_clock("2026-10-26", 1_793_100_000)[0] == 1_793_100_000


class FakeClient:
    def __init__(self):
        self.scrapes, self.monitors, self.state = [], [], "active_listing"
        self.change = None

    def observe_public_page(self, url, product, gtin):
        self.scrapes.append(url)
        return {"state": self.state, "evidence": f"{self.state} seen", "page_title": "P", "extract_ms": 900,
                "product": {"name": getattr(self, "page_name", "Arla Protein")}, "markdown": "Arla Protein\nHinta 1,39 €",
                "excerpt": "Hinta 1,39 €", "request_id": "req_1", "cache": "miss", "credits": 6}

    def monitor(self, mid):
        return {"id": mid, "status": "active", "last_run_at": "2026-11-14T18:00:00Z", "next_run_at": "2026-11-14T18:10:00Z"}

    def create_page_monitor(self, name, url, product):
        self.monitors.append(url)
        return {"id": f"m{len(self.monitors)}"}

    def changes(self, since, kinds):
        return [self.change] if self.change and self.change["monitor_id"] in kinds else []

    def search(self, q, domains, limit=10):
        return [{"url": "https://www.k-ruoka.fi/p", "title": "K"}, {"url": "https://bad.example/p", "title": "x"}]


def service(**cfg):
    fake = FakeClient()
    base = {"allowed_domains": ["k-ruoka.fi"], "sla_hours": 4, "max_targets_per_case": 2, "scrape_budget_per_hour": 5,
            "min_rescrape_seconds": 300, "recheck_minutes": 60}
    svc = LiveCases(CaseStore(), {**base, **cfg}, client_factory=lambda: fake)
    case = svc.store.open_case({"notice_url": "https://www.ruokavirasto.fi/n/", "title_fi": "T", "notice_date": "2026-10-26",
                                "notice_at": 0, "notice_at_basis": "t", "sla_hours": 4, "products": ["Arla Protein"]})
    return svc, fake, case


def test_a_monitor_change_triggers_a_rescrape_that_closes_the_page_and_persists():
    svc, fake, case = service()
    t = svc.add_target(case["id"], "https://www.k-ruoka.fi/p", "", "", "Aino", now=1 * H)
    assert fake.scrapes == ["https://www.k-ruoka.fi/p"] and t["monitor_id"] == "m1"
    fake.state = "recall_message"
    fake.change = {"id": "ch1", "monitor_id": "m1", "detected_at": "1970-01-01T03:00:00Z"}
    svc.poll(3 * H)
    svc.poll(3.1 * H)                                   # the same change is not read twice
    rows = svc.store.observations(t["id"])
    assert [r["trigger"] for r in rows] == ["added", "monitor_change"]
    view = svc.store.view(case["id"], 3.2 * H)
    assert view["summary"]["recall_visible"] == 1 and view["targets"][0]["sla"] == "met"
    assert view["targets"][0]["milestones"]["purchase_closed"]["changed_after"] == 3 * H - 600


def test_guard_rails_refuse_unapproved_domains_too_many_pages_fast_rereads_and_an_empty_budget():
    svc, fake, case = service()
    for url in ("https://bad.example/p", "http://www.k-ruoka.fi/p"):
        try:
            svc.add_target(case["id"], url, "", "", "A", now=0)
            assert False
        except Refused:
            pass
    t = svc.add_target(case["id"], "https://www.k-ruoka.fi/a", "", "", "A", now=0)
    svc.add_target(case["id"], "https://www.k-ruoka.fi/b", "", "", "A", now=0)
    try:
        svc.add_target(case["id"], "https://www.k-ruoka.fi/c", "", "", "A", now=0)
        assert False
    except Refused as e:
        assert "maximum" in str(e)
    try:
        svc.scrape(t["id"], 10, "manual")
        assert False
    except Refused as e:
        assert e.status == 429
    for i in range(3):
        svc.scrape(t["id"], 400 * (i + 1), "manual")
    try:
        svc.scrape(t["id"], 2000, "manual")
        assert False
    except Refused as e:
        assert "budget" in str(e)


def test_suggestions_keep_only_approved_domains_and_drop_titles_that_share_only_the_brand():
    svc, fake, case = service()
    assert [s["url"] for s in svc.suggest(case["id"], 0)] == ["https://www.k-ruoka.fi/p"]
    svc.store.suggest(case["id"], "https://www.k-ruoka.fi/a", "Arla Protein vanukas 200g vanilja", 0)
    svc.store.suggest(case["id"], "https://www.k-ruoka.fi/b", "Arla Kevyt maito 1l", 0)
    svc.store.db.execute("UPDATE cases SET products = ? WHERE id = ?", ('["Arla Protein Vanilja vanukas 200g"]', case["id"]))
    assert [s["url"] for s in svc.ranked_suggestions(case["id"])] == ["https://www.k-ruoka.fi/a"]


def test_cases_survive_a_restart(tmp_path):
    db = str(tmp_path / "a.sqlite")
    store = CaseStore(db)
    case = store.open_case({"notice_url": "https://www.ruokavirasto.fi/n/", "title_fi": "T", "notice_date": "2026-10-26",
                            "notice_at": 0, "notice_at_basis": "t", "sla_hours": 4})
    t, _ = store.add_target(case["id"], "https://www.k-ruoka.fi/p", "K", "Product page", "A", 1)
    store.observe(t["id"], 2, "active_listing", "Price visible", "added")
    store.act(t["id"], "Aino", "escalated", "Sent to web team", 3)
    again = CaseStore(db)
    view = again.view(case["id"], 4)
    assert view["targets"][0]["observations"] == 1 and view["targets"][0]["actions"][0]["who"] == "Aino"


def test_a_lot_limited_recall_asks_for_a_lot_check_instead_of_declaring_a_breach():
    rows = [obs(19 * 24 * H, "active_listing", "added")]
    tl = target_timeline(rows, [], 0, 4 * H, 19 * 24 * H, lot_limited=True)
    assert tl["sla"] == "verify_lot" and tl["needs_action"]


class ListClient(FakeClient):
    def __init__(self):
        super().__init__()
        self.items = []

    def recall_list(self, url):
        return self.items

    def monitors(self):
        return []

    def read_notice(self, item, now, official=True):
        from recall.engine import Notice
        return Notice(id=item["url"], url=item["url"], title=item["title"], date=item["date"], published_at=0,
                      detected_at=now, products=["Coop Chopped Chili 50 g"], title_en="Cadmium above the limit in frozen chili")


def test_a_new_notice_on_the_list_opens_a_case_with_a_clock_from_our_previous_read():
    fake = ListClient()
    svc = LiveCases(CaseStore(), {"allowed_domains": ["s-kaupat.fi"], "scrape_budget_per_hour": 20, "auto_open_days": 3},
                    client_factory=lambda: fake, list_url="https://www.ruokavirasto.fi/list/")
    t0 = 1_794_650_000                                   # 14 Nov 2026, 19:13 Helsinki
    old = {"date": "2026-10-27", "title": "Vanha", "url": "https://www.ruokavirasto.fi/old/"}
    fake.items = [old]
    assert svc.watch_list(t0) == []                      # older than the window: no case
    new = {"date": "2026-11-14", "title": "Pakastetussa chilissä kadmiumylitys", "url": "https://www.ruokavirasto.fi/new/"}
    fake.items = [new, old]
    opened = svc.watch_list(t0 + 3600)
    assert len(opened) == 1 and opened[0]["notice_at"] == t0 and "earlier read" in opened[0]["notice_at_basis"]
    assert opened[0]["title_en"].startswith("Cadmium") and svc.watch_list(t0 + 7200) == []   # no duplicate case


def test_the_budget_counts_reads_from_before_a_restart(tmp_path):
    import time
    db = str(tmp_path / "b.sqlite")
    store = CaseStore(db)
    case = store.open_case({"notice_url": "https://www.ruokavirasto.fi/n/", "title_fi": "T", "notice_date": "2026-10-26",
                            "notice_at": 0, "notice_at_basis": "t", "sla_hours": 4})
    t, _ = store.add_target(case["id"], "https://www.k-ruoka.fi/p", "K", "P", "A", 1)
    for _ in range(3):
        store.observe(t["id"], time.time(), "active_listing", "x", "scheduled")
    svc = LiveCases(CaseStore(db), {"scrape_budget_per_hour": 5})
    assert svc.budget.left(time.time()) == 2


def test_a_suggestion_whose_address_carries_the_official_gtin_ranks_first():
    svc, fake, case = service()
    svc.store.db.execute("UPDATE cases SET gtins = ? WHERE id = ?", ('["07340191134195"]', case["id"]))
    svc.store.suggest(case["id"], "https://www.k-ruoka.fi/x/7340191134195", "Hienonnettu chili", 0)
    assert svc.ranked_suggestions(case["id"])[0]["url"].endswith("7340191134195")


def test_an_observation_keeps_its_receipt_and_a_page_without_product_match_is_unclassified():
    svc, fake, case = service()
    t = svc.add_target(case["id"], "https://www.k-ruoka.fi/p", "", "", "Aino", now=1 * H)
    o = svc.store.observations(t["id"])[-1]
    assert o["identity"] == "exact_name" and o["excerpt_verified"] == 1 and len(o["content_sha256"]) == 64
    assert o["request_id"] == "req_1" and o["cache"] == "miss" and svc.store.artifact(f"{o['id']}.md").startswith(b"Arla")
    assert svc.store.target(t["id"])["origin"] == "suggestion" or svc.store.target(t["id"])["origin_detail"]
    fake.page_name = "Valio maito 1 l"
    svc.scrape(t["id"], 2 * H, "scheduled")
    o = svc.store.observations(t["id"])[-1]
    assert o["identity"] == "none" and o["raw_state"] == "active_listing" and o["state"] == "unverified"
    svc.poll(2.1 * H)
    assert svc.monitor_status["m1"]["status"] == "active"


def test_photos_are_kept_as_stored_copies_with_their_hash():
    fake = FakeClient()
    svc = LiveCases(CaseStore(), {"allowed_domains": ["k-ruoka.fi"], "scrape_budget_per_hour": 20},
                    client_factory=lambda: fake, image_fetcher=lambda url: (b"\x89PNG photo of " + url.encode(), "png"))
    case = svc.store.open_case({"notice_url": "https://www.ruokavirasto.fi/n/", "title_fi": "T", "notice_date": "2026-10-26",
                                "notice_at": 0, "notice_at_basis": "t", "sla_hours": 4, "products": ["Arla Protein"],
                                "images": [{"url": "https://www.ruokavirasto.fi/globalassets/takaisinvedot/a.png", "alt": "A"}]})
    svc.keep_notice_photos(case)
    img = svc.store.case(case["id"])["images"][0]
    assert img["local"] == f"case-{case['id']}-0.png" and len(img["sha256"]) == 64
    assert svc.store.artifact(img["local"]).startswith(b"\x89PNG")
    fake.observe_public_page = lambda *a: {"state": "active_listing", "evidence": "e", "page_title": "P",
                                           "product": {"name": "Arla Protein", "image": "https://cdn.example/p.webp"}}
    t = svc.add_target(case["id"], "https://www.k-ruoka.fi/p", "", "", "A", now=1)
    o = svc.store.observations(t["id"])[-1]
    import json as _j
    p = _j.loads(o["product"])
    assert p["image_local"] == f"obs-{o['id']}.png" and p["image_sha256"]


def test_an_unsafe_photo_address_is_refused():
    from recall.images import ImageRefused, fetch_image
    for url in ("http://example.com/a.png", "https://127.0.0.1/a.png", "https://localhost/a.png"):
        try:
            fetch_image(url)
            assert False, url
        except ImageRefused:
            pass


def test_a_page_whose_first_read_fails_keeps_its_monitor_and_is_retried():
    svc, fake, case = service()
    good = fake.observe_public_page

    def broken(*a):
        raise RuntimeError("upstream timeout")
    fake.observe_public_page = broken
    try:
        svc.add_target(case["id"], "https://www.k-ruoka.fi/p", "", "", "Aino", now=1 * H)
        assert False
    except Refused as e:
        assert e.status == 502
    t = svc.store.targets(case["id"])[0]
    assert t["monitor_id"] == "m1" and not svc.store.observations(t["id"])   # watched, though not read yet
    fake.observe_public_page = good
    svc.poll(1 * H + 700)                                                     # the poller retries the first read
    assert [o["trigger"] for o in svc.store.observations(t["id"])] == ["added"]
    assert len(fake.monitors) == 1                                            # no second monitor


def test_a_photo_download_connects_to_the_checked_address_with_the_real_host_for_tls(monkeypatch):
    import httpx
    from recall import images
    answers = iter(["93.184.216.34", "127.0.0.1"])           # a rebinding DNS: public first, private after
    monkeypatch.setattr(images, "public_address", lambda host: next(answers))
    seen = {}

    def handler(request):
        seen.update(url=str(request.url), host=request.headers["host"], sni=request.extensions.get("sni_hostname"))
        return httpx.Response(200, headers={"content-type": "image/png"}, content=b"\x89PNG")
    data, ext = images.fetch_image("https://cdn.example.fi/a.png", client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert data == b"\x89PNG" and ext == "png"
    assert seen == {"url": "https://93.184.216.34/a.png", "host": "cdn.example.fi", "sni": "cdn.example.fi"}


def test_the_evidence_snapshot_keeps_observations_and_photos_but_not_test_clicks(tmp_path):
    from recall import evidence
    data_db, data_art = tmp_path / "data" / "a.sqlite", tmp_path / "data" / "artifacts"
    snap_db, snap_art = tmp_path / "evidence" / "e.sqlite", tmp_path / "evidence" / "artifacts"
    (tmp_path / "data").mkdir()
    store = CaseStore(str(data_db))
    case = store.open_case({"notice_url": "https://www.ruokavirasto.fi/n/", "title_fi": "T", "notice_date": "2026-10-26",
                            "notice_at": 0, "notice_at_basis": "t", "sla_hours": 4})
    t, _ = store.add_target(case["id"], "https://www.k-ruoka.fi/p", "K", "P", "A", 1)
    o = store.observe(t["id"], 2, "active_listing", "Price visible", "added", markdown="Hinta 1,39 €", screenshot=b"jpg")
    store.act(t["id"], "Tester", "owner_contacted", "", 3, recipient="someone@example.fi")
    evidence.export(data_db, data_art, snap_db, snap_art)             # the copy that is safe to publish
    snap = CaseStore(str(snap_db), artifact_dir=str(snap_art))
    kept = snap.observations(t["id"])[0]
    assert kept["evidence"] == "Price visible" and not snap.actions(t["id"]) and snap.owner_for("https://www.k-ruoka.fi/x") == ""
    assert snap.artifact(f"{o['id']}.jpg") is None and len(kept["content_sha256"]) == 64 and snap.artifacts_withheld()
    evidence.export(data_db, data_art, snap_db, snap_art, full=True)  # the demo machine's copy
    assert CaseStore(str(snap_db), artifact_dir=str(snap_art)).artifact(f"{o['id']}.jpg") == b"jpg"
    fresh = tmp_path / "fresh" / "a.sqlite"
    evidence.restore(fresh, tmp_path / "fresh" / "artifacts", snap_db, snap_art) if (tmp_path / "fresh").mkdir() is None else None
    assert CaseStore(str(fresh)).observations(t["id"])[0]["state"] == "active_listing"


def test_a_monitor_change_whose_read_is_refused_is_retried_on_the_next_poll():
    svc, fake, case = service(scrape_budget_per_hour=2)
    t = svc.add_target(case["id"], "https://www.k-ruoka.fi/p", "", "", "Aino", now=1 * H)       # uses 1 of 2
    svc._spend(1 * H)                                                                             # budget now empty
    fake.change = {"id": "ch9", "monitor_id": "m1", "detected_at": "1970-01-01T01:30:00Z"}
    svc.poll(1.6 * H)
    assert "ch9" not in svc.seen_changes and len(svc.store.observations(t["id"])) == 1
    svc.poll(2.7 * H)                                                                             # budget free again
    assert "ch9" in svc.seen_changes and svc.store.observations(t["id"])[-1]["change_id"] == "ch9"


def test_every_search_counts_against_the_budget_and_the_count_survives_a_restart(tmp_path):
    import time as _t
    db = str(tmp_path / "s.sqlite")
    fake = FakeClient()
    svc = LiveCases(CaseStore(db), {"allowed_domains": ["k-ruoka.fi"], "scrape_budget_per_hour": 50},
                    client_factory=lambda: fake)
    case = svc.store.open_case({"notice_url": "https://www.ruokavirasto.fi/n/", "title_fi": "T", "notice_date": "2026-10-26",
                                "notice_at": 0, "notice_at_basis": "t", "sla_hours": 4, "products": ["A b", "C d"],
                                "gtins": ["07340191134195"]})
    svc.suggest(case["id"], _t.time())
    assert svc.budget.left(_t.time()) == 47                      # one GTIN and two product searches
    again = LiveCases(CaseStore(db), {"scrape_budget_per_hour": 50})
    assert again.budget.left(_t.time()) == 47


def test_a_list_monitor_change_starts_the_clock_one_interval_before_it_was_seen():
    fake = ListClient()
    svc = LiveCases(CaseStore(), {"scrape_budget_per_hour": 20, "auto_open_days": 3, "monitor_interval_minutes": 10},
                    client_factory=lambda: fake, list_url="https://www.ruokavirasto.fi/list/")
    seen = 1_794_660_000                                         # 14 Nov 2026, about 21:20 Helsinki
    fake.items = [{"date": "2026-11-14", "title": "Uusi", "url": "https://www.ruokavirasto.fi/new/"}]
    opened = svc.watch_list(seen + 60, detected_at=seen)
    assert opened[0]["notice_at"] == seen - 600 and "list monitor saw the change" in opened[0]["notice_at_basis"]


def test_a_restart_does_not_read_a_page_again_for_a_change_it_already_handled():
    svc, fake, case = service()
    t = svc.add_target(case["id"], "https://www.k-ruoka.fi/p", "", "", "Aino", now=1 * H)
    fake.change = {"id": "ch1", "monitor_id": "m1", "detected_at": "1970-01-01T03:00:00Z"}
    svc.poll(3 * H)
    restarted = LiveCases(svc.store, svc.cfg, client_factory=lambda: fake)   # same database, fresh process
    restarted.poll(3.2 * H)
    assert [r["trigger"] for r in svc.store.observations(t["id"])] == ["added", "monitor_change"]
