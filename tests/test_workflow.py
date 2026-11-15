from recall.cases import CaseStore, target_timeline
from recall.workflow import owner_message

H = 3600


def obs(at, state):
    return {"at": at, "state": state, "trigger": "scheduled", "evidence": state, "window_start": None}


def act(at, action, **kw):
    return {"at": at, "action": action, "who": "Aino", "note": kw.pop("note", ""), **kw}


def tl(observations, actions, now, lot=False):
    return target_timeline(observations, actions, 0, 4 * H, now, lot, "K-Ruoka")


def test_an_open_purchasable_page_asks_to_contact_the_owner():
    t = tl([obs(1 * H, "active_listing")], [], 2 * H)
    assert t["review"] == "open" and t["needs_action"] and t["next_step"].startswith("Contact K-Ruoka")


def test_contacting_the_owner_waits_until_the_deadline_then_needs_action_again():
    contact = act(2 * H, "owner_contacted", recipient="verkkokauppa@example.fi", due_at=26 * H)
    waiting = tl([obs(1 * H, "active_listing")], [contact], 10 * H)
    assert waiting["review"] == "waiting_on_owner" and not waiting["needs_action"]
    assert "verkkokauppa@example.fi" in waiting["next_step"]
    overdue = tl([obs(1 * H, "active_listing")], [contact], 27 * H)
    assert overdue["needs_action"] and "deadline" in overdue["next_step"]


def test_a_page_change_after_contact_asks_for_a_check():
    contact = act(2 * H, "owner_contacted", recipient="x", due_at=26 * H)
    t = tl([obs(1 * H, "active_listing"), obs(5 * H, "recall_message")], [contact], 6 * H)
    assert t["review"] == "waiting_on_owner" and t["needs_action"] and "changed after you contacted" in t["next_step"]


def test_a_lot_confirmation_resolves_and_marks_the_sla_as_confirmed_by_a_person():
    done = act(3 * H, "resolved", outcome="lot_outside_recall", note="Owner: stock is best before 03/2027")
    t = tl([obs(1 * H, "active_listing")], [done], 30 * H, lot=True)
    assert t["review"] == "resolved" and not t["needs_action"] and t["sla"] == "lot_confirmed"


def test_a_resolved_page_reopens_when_its_state_changes():
    done = act(3 * H, "resolved", outcome="page_updated")
    t = tl([obs(1 * H, "removed"), obs(5 * H, "active_listing")], [done], 6 * H)
    assert t["review"] == "reopened" and t["needs_action"] and "after it was resolved" in t["next_step"]


def test_the_store_validates_outcomes_notes_and_remembers_the_owner_contact():
    store = CaseStore()
    case = store.open_case({"notice_url": "https://www.ruokavirasto.fi/n/", "title_fi": "T", "notice_date": "2026-10-26",
                            "notice_at": 0, "notice_at_basis": "t", "sla_hours": 4})
    t, _ = store.add_target(case["id"], "https://www.k-ruoka.fi/p", "K-Ruoka", "P", "A", 1)
    for kwargs in ({"outcome": "maybe"}, {"outcome": "lot_outside_recall"}, {"outcome": "classification_wrong", "note": "x"}):
        try:
            store.act(t["id"], "Aino", "resolved", kwargs.pop("note", ""), 2, **kwargs)
            assert False, kwargs
        except ValueError:
            pass
    try:
        store.act(t["id"], "Aino", "owner_contacted", "", 2)
        assert False
    except ValueError:
        pass
    store.act(t["id"], "Aino", "owner_contacted", "", 2, recipient="web@k.example")
    store.observe(t["id"], 2.5, "active_listing", "Price visible", "added")
    try:   # the last read still shows the product for sale: "page updated" would be false
        store.act(t["id"], "Aino", "resolved", "", 2.6, outcome="page_updated")
        assert False
    except ValueError as e:
        assert "Check page now" in str(e)
    assert store.owner_for("https://www.k-ruoka.fi/other") == "web@k.example"
    rows = store.report_rows(case["id"])
    assert [r["kind"] for r in rows] == ["action", "observation"] and rows[0]["recipient"] == "web@k.example"


def test_the_owner_message_carries_the_evidence_and_does_not_allege_a_sale():
    case = {"products": ["Coop Chopped Chili 50 g"], "title_fi": "Pakastetussa chilissä kadmiumylitys", "title_en": "Cadmium",
            "notice_date": "2026-10-27", "notice_url": "https://www.ruokavirasto.fi/x/", "gtins": ["07340191134195"],
            "lot": "Best before 31 July 2027"}
    row = {"publisher": "S-kaupat", "url": "https://www.s-kaupat.fi/p", "state": "active_listing", "observed_at": 1_794_678_000,
           "evidence": "Listed at €1.19"}
    m = owner_message(case, row, 24)
    assert "Listed at €1.19" in m["body"] and "Best before 31 July 2027" in m["body"] and "07340191134195" in m["body"]
    assert "does not claim that a recalled pack was sold" in m["body"] and "24 hours" in m["body"]


def test_a_prepared_draft_is_not_a_contact_until_a_person_confirms_sending_it():
    draft = act(2 * H, "draft_prepared", recipient="web@k.example")
    t = tl([obs(1 * H, "active_listing")], [draft], 3 * H)
    assert t["review"] == "draft_prepared" and t["needs_action"] and "Confirm once you have sent it" in t["next_step"]
    sent = act(2.5 * H, "owner_contacted", recipient="web@k.example", due_at=26 * H)
    t = tl([obs(1 * H, "active_listing")], [draft, sent], 3 * H)
    assert t["review"] == "waiting_on_owner" and not t["needs_action"]


def test_a_partial_name_match_needs_a_person_before_it_counts():
    o = dict(obs(1 * H, "active_listing"), identity="partial_name", identity_detail="“Coop chili punainen” shares 2 words")
    t = tl([o], [], 2 * H)
    assert t["identity_pending"] and t["needs_action"] and t["sla"] == "identity_check" and "official photo" in t["next_step"]
    ok = tl([o], [act(1.5 * H, "identity_confirmed", note="Same pack as the notice photo")], 2 * H)
    assert not ok["identity_pending"] and ok["identity"] == "confirmed" and ok["sla"] == "open"
