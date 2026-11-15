from recall.afterimage import AfterimageReplay


def test_the_demo_replay_is_simulated_and_changes_state_over_time():
    demo = AfterimageReplay(started_at=1_000)
    start = demo.state(1_002)                    # 10 simulated minutes in
    assert start["mode"] == "demo_replay" and start["simulated"] is True
    assert start["summary"]["appears_purchasable"] == 3 and start["summary"]["purchase_closed"] == 0
    later = demo.state(1_000 + 230 / 5)          # 230 simulated minutes in
    assert later["summary"]["recall_visible"] == 2 and later["summary"]["purchase_closed"] == 3
    a = next(t for t in later["targets"] if t["id"] == "demo-storefront-a")
    closed = a["milestones"]["purchase_closed"]
    assert closed["lag_s"] == 209 * 60 and closed["changed_after"] == later["case"]["notice_at"] + 199 * 60
    assert a["sla"] == "met"


def test_a_page_still_purchasable_after_the_sla_is_breached_and_first_in_the_queue():
    demo = AfterimageReplay(started_at=1_000)
    end = demo.state(1_000 + 320 / 5)
    first = end["targets"][0]
    assert first["id"] == "demo-marketplace-d" and first["sla"] == "breached" and first["needs_action"]


def test_the_replay_loops_and_forgets_reviews_of_the_previous_loop():
    demo = AfterimageReplay(started_at=1_000)
    demo.act("demo-marketplace-d", "Aino", "owner_contacted", now=1_000 + 320 / 5, recipient="seller", due_hours=24)
    assert not demo.state(1_000 + 320 / 5)["targets"][0]["needs_action"] or \
        demo.state(1_000 + 320 / 5)["summary"]["needs_action"] == 0
    restarted = demo.state(1_000 + demo.cycle_s + 1)
    assert restarted["sim_elapsed_s"] < 600 and all(not t["actions"] for t in restarted["targets"])
