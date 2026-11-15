from recall.config import load_shelf, load_watch
from recall.engine import CHECK, CLEAR, DAY, EAN, PULL, Board, Notice, Product, match, match_evidence, name_tokens, size_tokens
from recall.replay import Replay, load_notices, ts

SHELF, _ = load_shelf(load_watch())
P = {p.id: p for p in SHELF}


def notice(**kw) -> Notice:
    base = dict(id="n1", url="https://www.ruokavirasto.fi/n1", title="t", date="2026-02-10", published_at=ts("2026-02-10"),
                detected_at=ts("2026-02-10"), brand="Atria", products=["Atria Coleslaw 300 g"], summary="Wrong tub.")
    return Notice(**{**base, **kw})


def test_pack_sizes_are_not_name_words():
    assert name_tokens("Atria Coleslaw 300 g") == {"atria", "coleslaw"}
    assert size_tokens("Elovena kaurajuoma kahviin 2,5 dl") == {"2.5dl"} and size_tokens("Japp 160g") == {"160g"}


def test_the_same_ean_code_is_a_pull_and_leading_zeros_do_not_matter():
    n = notice(brand="", products=["something else"], eans=["06407860065295"])
    assert match(n, P["coleslaw"]) == (PULL, "same EAN code", EAN)


def test_text_matches_are_review_only_even_when_brand_and_pack_look_exact():
    assert match(notice(), Product(id="a", name="Atria Coleslaw 300 g", brand="Atria"))[0] == CHECK
    assert match(notice(), Product(id="b", name="Atria Coleslaw Salaatti 3kg", supplier="ATRIA SUOMI OY TUORE"))[0] == CHECK
    assert match(notice(eans=["6407860065295"]), Product(id="c", name="Atria Coleslaw 300 g", brand="Atria", ean="6400000000001"))[0] == CHECK


def test_another_brand_with_the_same_words_is_no_hit():
    n = notice(brand="Via", products=["Via Red Thai Curry 320 g"])
    assert match(n, Product(id="k", name="Kitchen Joy Thai-Cube Red Curry 350 g", brand="Kitchen Joy"))[0] == CLEAR
    assert match(n, P["butter"])[0] == CLEAR


def test_a_notice_that_is_not_from_the_authority_gives_check_at_most():
    level, why, _ = match(notice(official=False), P["coleslaw"])
    assert level == CHECK and "authority" in why


def test_an_ean_match_that_is_not_from_the_authority_keeps_the_ean_code_as_its_basis():
    level, why, basis = match(notice(official=False, eans=["6407860065295"]), P["coleslaw"])
    assert level == CHECK and "authority" in why and basis == EAN
    assert match_evidence(level, basis) == ("medium", "EAN code")
    assert match_evidence(PULL, EAN) == ("high", "EAN code")


def test_a_hold_stays_open_until_a_lot_based_outcome_closes_it():
    b = Board(SHELF, active_days=30)
    n = notice(eans=["6407860065295"], allergens=["egg", "milk", "mustard", "not an allergen"])
    assert b.add(n) and not b.add(n)
    t = n.published_at
    s = b.state(t + DAY)
    assert s["level"] == PULL and s["blocked"] == ["coleslaw"]
    assert "Quarantine Atria Coleslaw 300 g and verify the lot on the pack" in s["answer"]
    assert "egg, milk, mustard" in s["answer"]
    assert b.state(t + 31 * DAY)["level"] == CLEAR and b.state(t - DAY)["level"] == CLEAR
    b.ack("n1", "coleslaw", "quarantined", "Aino", t + 2 * DAY)
    s = b.state(t + 3 * DAY)
    assert s["level"] == PULL and s["hits"][0]["case_state"] == "held_pending_verification" and s["blocked"] == ["coleslaw"]
    b.record_event("n1", "coleslaw", "released_not_this_lot", "Aino", t + 3 * DAY, lot="LOT-OTHER", note="Physical pack differs")
    s = b.state(t + 4 * DAY)
    assert s["level"] == CLEAR and s["hits"][0]["case_state"] == "released" and s["blocked"] == []
    for bad in (("n1", "butter", "quarantined", "Aino"), ("n1", "coleslaw", "eaten", "Aino"), ("n1", "coleslaw", "quarantined", " "),
                ("n1", "coleslaw", "not_this_lot", "Aino")):
        try:
            b.ack(*bad, t)
            assert False, bad
        except (KeyError, ValueError):
            pass


def test_notices_and_actions_come_back_after_a_restart(tmp_path):
    db = str(tmp_path / "recall.sqlite")
    b = Board(SHELF, db=db)
    n = notice(eans=["6407860065295"])
    b.add(n)
    b.ack("n1", "coleslaw", "held", "Aino", n.published_at)
    b.ack("n1", "coleslaw", "not_this_lot", "Aino", n.published_at, lot="LOT-OTHER")
    again = Board(SHELF, db=db)
    s = again.state(n.published_at + DAY)
    assert list(again.notices) == ["n1"] and s["level"] == CLEAR and s["hits"][0]["ack"]["action"] == "released_not_this_lot"


def test_a_revised_notice_recomputes_the_task_and_keeps_a_version_history():
    b = Board(SHELF)
    first = notice(eans=["6407860065295"], lot="use by 21.2.2026")
    revised = notice(eans=["6407860065295"], lot="use by 22.2.2026", summary="Corrected date.")
    assert b.add(first) is True
    assert b.add(revised) is False
    assert b.notices["n1"].lot == "use by 22.2.2026"
    versions = b.db.execute("SELECT version FROM notice_versions WHERE notice_id = 'n1' ORDER BY version").fetchall()
    assert versions == [(1,), (2,)]


def test_a_source_revision_reopens_a_terminal_case_without_erasing_the_history():
    b = Board(SHELF)
    first = notice(eans=["6407860065295"], lot="use by 21.2.2026")
    b.add(first)
    b.record_event("n1", "coleslaw", "held", "Aino", first.published_at)
    b.record_event("n1", "coleslaw", "released_not_this_lot", "Aino", first.published_at + 1, lot="OTHER-LOT")
    assert b.state(first.published_at + DAY)["level"] == CLEAR
    revised = notice(eans=["6407860065295"], lot="use by 22.2.2026")
    b.add(revised)
    state = b.state(first.published_at + DAY)
    assert state["level"] == PULL and state["hits"][0]["case_state"] == "new"
    events = b.db.execute("SELECT action FROM events WHERE notice_id = 'n1' AND product_id = 'coleslaw' ORDER BY event_id").fetchall()
    assert events == [("held",), ("released_not_this_lot",), ("evidence_revised",)]


def test_the_snapshot_of_2026_gives_every_level_on_the_demo_shelf():
    notices, about = load_notices()
    assert len(notices) == 38 and about["read_on"] == "2026-11-14"
    b = Board(SHELF)
    for n in notices:
        b.add(n)
    levels = {max((hit[1] for hit in hits), default=CLEAR) for hits in b.hits.values()}
    assert levels == {CLEAR, CHECK, PULL}
    assert sum(1 for hits in b.hits.values() if hits) == 7     # the seven products we chose for this
    assert b.state(ts("2026-11-14"))["level"] == CHECK         # an active text-only match is review, not a hold


def test_the_replay_runs_through_the_year_and_starts_again():
    r = Replay(real_start=1000.0, day_s=2.0)
    t, cycle = r.now(1000.0 + 2.0 * 40)
    assert cycle == 0 and abs(t - (r.t0 + 40 * DAY)) < 1
    span_s = (r.t1 - r.t0) / DAY * 2.0
    assert r.now(1000.0 + span_s + 4.0)[1] == 1


def test_acknowledgements_of_the_first_version_are_migrated_into_the_event_log_once(tmp_path):
    import json
    import sqlite3
    from dataclasses import asdict
    db = str(tmp_path / "old.sqlite")
    old = sqlite3.connect(db)   # the schema of the first version
    old.executescript("""
        CREATE TABLE notices (id TEXT PRIMARY KEY, body TEXT NOT NULL);
        CREATE TABLE acks (notice_id TEXT, product_id TEXT, action TEXT, who TEXT, at REAL, PRIMARY KEY (notice_id, product_id));
        CREATE TABLE history (t REAL, level INTEGER);
    """)
    n = notice(eans=["6407860065295"])
    old.execute("INSERT INTO notices VALUES (?, ?)", (n.id, json.dumps(asdict(n))))
    old.execute("INSERT INTO acks VALUES ('n1', 'coleslaw', 'not_this_lot', 'Aino', ?)", (ts("2026-02-11"),))
    old.commit()
    old.close()
    board = Board(SHELF, db=db)
    case = board._case_states()[("n1", "coleslaw")]
    assert case["state"] == "released" and case["who"] == "Aino" and "Migrated" in case["note"]
    assert Board(SHELF, db=db).db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1   # only once


def test_a_reworded_extraction_is_not_a_revision_but_a_new_lot_date_is():
    board = Board(SHELF)
    n = notice(eans=["6407860065295"], lot="Best before 12.02.2026.", summary="The tub may hold another salad.")
    board.add(n)
    board.record_event("n1", "coleslaw", "held", "Aino", ts("2026-02-11"))
    board.record_event("n1", "coleslaw", "released_not_this_lot", "Aino", ts("2026-02-11"), lot="L123")
    reworded = notice(eans=["6407860065295"], lot="Use by date 12.02.2026", summary="A different coleslaw may be in the tub.")
    assert board.add(reworded) is False
    assert board._case_states()[("n1", "coleslaw")]["state"] == "released"
    expanded = notice(eans=["6407860065295"], lot="Best before 12.02.2026 and 13.02.2026")
    board.add(expanded)
    assert board._case_states()[("n1", "coleslaw")]["state"] == "new"


def test_a_notice_from_before_versions_keeps_its_original_as_version_1():
    board = Board(SHELF)
    board.add(notice(lot="Best before 12.02.2026"))
    board.db.execute("DELETE FROM notice_versions")         # as stored by the first version
    board.add(notice(lot="Best before 13.02.2026"))
    rows = board.db.execute("SELECT version, body FROM notice_versions ORDER BY version").fetchall()
    assert [r[0] for r in rows] == [1, 2] and "12.02.2026" in rows[0][1] and "13.02.2026" in rows[1][1]


def test_a_held_product_stays_on_the_shelf_when_a_new_import_leaves_it_out():
    board = Board(SHELF)
    board.add(notice(eans=["6407860065295"]))
    board.record_event("n1", "coleslaw", "held", "Aino", ts("2026-02-11"))
    kept = board.replace_shelf([p for p in SHELF if p.id != "coleslaw"])
    assert kept == ["coleslaw"] and any(hit[0] == "coleslaw" for hit in board.hits["n1"])


def test_a_revision_keeps_the_time_of_the_first_read():
    b = Board(SHELF)
    early = ts("2026-02-10", hour=7)
    b.add(notice(eans=["6407860065295"], lot="use by 21.2.2026", published_at=early, detected_at=early))
    b.add(notice(eans=["6407860065295"], lot="use by 22.2.2026", detected_at=early + DAY))
    n = b.notices["n1"]
    assert n.lot == "use by 22.2.2026" and n.published_at == early and n.detected_at == early
    observed = b.db.execute("SELECT observed_at FROM notice_versions WHERE notice_id = 'n1' ORDER BY version").fetchall()
    assert observed == [(early,), (early + DAY,)]


def test_the_blocked_list_and_the_level_agree_also_for_a_held_pack():
    b = Board(SHELF, active_days=30)
    n = notice(eans=["6407860065295"])
    b.add(n)
    b.record_event("n1", "coleslaw", "held", "Aino", n.published_at)
    later = n.published_at + 40 * DAY                        # past the window, but the pack is still on hold
    assert [h["product"] for h in b.blocked(later)] == b.state(later)["blocked"] == ["coleslaw"]
    assert b.blocked(later)[0]["ean"] == "6407860065295"     # the code as printed, not the padded GTIN-14
