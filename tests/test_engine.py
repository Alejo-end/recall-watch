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


def test_brand_and_name_is_a_pull_and_another_pack_is_a_check():
    assert match(notice(), Product(id="a", name="Atria Coleslaw 300 g", brand="Atria"))[0] == PULL
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


def test_a_hit_counts_for_thirty_days_and_stops_when_a_person_records_an_action():
    b = Board(SHELF, active_days=30)
    n = notice(eans=["6407860065295"], allergens=["egg", "milk", "mustard", "not an allergen"])
    assert b.add(n) and not b.add(n)
    t = n.published_at
    s = b.state(t + DAY)
    assert s["level"] == PULL and s["blocked"] == ["coleslaw"]
    assert "Quarantine Atria Coleslaw 300 g and verify the lot on the pack" in s["answer"] and "egg, milk, mustard" in s["answer"]
    assert b.state(t + 31 * DAY)["level"] == CLEAR and b.state(t - DAY)["level"] == CLEAR
    b.ack("n1", "coleslaw", "quarantined", "Aino", t + 2 * DAY)
    s = b.state(t + 3 * DAY)
    assert s["level"] == CLEAR and s["hits"][0]["ack"]["who"] == "Aino" and s["blocked"] == []
    for bad in (("n1", "butter", "quarantined", "Aino"), ("n1", "coleslaw", "eaten", "Aino"), ("n1", "coleslaw", "quarantined", " ")):
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
    b.ack("n1", "coleslaw", "not_this_lot", "Aino", n.published_at)
    again = Board(SHELF, db=db)
    s = again.state(n.published_at + DAY)
    assert list(again.notices) == ["n1"] and s["level"] == CLEAR and s["hits"][0]["ack"]["action"] == "not_this_lot"


def test_the_snapshot_of_2026_gives_every_level_on_the_demo_shelf():
    notices, about = load_notices()
    assert len(notices) == 38 and about["read_on"] == "2026-11-14"
    b = Board(SHELF)
    for n in notices:
        b.add(n)
    levels = {max((hit[1] for hit in hits), default=CLEAR) for hits in b.hits.values()}
    assert levels == {CLEAR, CHECK, PULL}
    assert sum(1 for hits in b.hits.values() if hits) == 7     # the seven products we chose for this
    assert b.state(ts("2026-11-14"))["level"] == PULL          # the pudding notice of 26 October is still open


def test_the_replay_runs_through_the_year_and_starts_again():
    r = Replay(real_start=1000.0, day_s=2.0)
    t, cycle = r.now(1000.0 + 2.0 * 40)
    assert cycle == 0 and abs(t - (r.t0 + 40 * DAY)) < 1
    span_s = (r.t1 - r.t0) / DAY * 2.0
    assert r.now(1000.0 + span_s + 4.0)[1] == 1
