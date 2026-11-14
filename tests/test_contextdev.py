import hashlib
import hmac
import json

import httpx

from pathlib import Path

from recall.contextdev import NOTICE_SCHEMA, ContextDev, notice_id, parse_recall_list, verify_webhook
from recall.replay import ts

SECRET = "whsec_test"


def sign(body: bytes, t: int, secret=SECRET) -> str:
    return f"t={t},v1=" + hmac.new(secret.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()


def client(handler):
    return ContextDev(api_key="ctxt_secret_test", transport=httpx.MockTransport(handler))


def test_news_search_sends_the_documented_body_and_reads_articles():
    seen = {}

    def handler(req):
        seen["url"], seen["auth"], seen["body"] = str(req.url), req.headers["authorization"], json.loads(req.content)
        return httpx.Response(200, json={"data": [{"id": "a1", "story_id": "s1", "title": "Recall of coleslaw",
            "description": "x", "url": "https://www.example.fi/x", "published_at": "2026-11-14T10:00:00Z",
            "source": {"name": "Example", "domain": "example.fi"}}], "has_more": False})

    out = client(handler).news("food recall", since=1_700_000_000)
    assert seen["url"] == "https://api.context.dev/v1/news/search"
    assert seen["auth"] == "Bearer ctxt_secret_test"
    assert seen["body"] == {"searchBy": {"type": "natural_language", "query": "food recall"}, "limit": 10,
                            "filterBy": {"date": {"from": 1_700_000_000_000}}}
    assert out[0]["id"] == "news:s1" and out[0]["source"] == "example.fi"


def test_the_list_parser_reads_the_page_as_it_was_on_14_november():
    html_text = (Path(__file__).parent / "fixtures" / "recall-list.html").read_text()
    items = parse_recall_list(html_text, "https://www.ruokavirasto.fi/elintarvikkeet/ohjeita-kuluttajille/takaisinvedot/")
    assert len(items) == 4
    assert items[0] == {"date": "2026-11-11", "title": "Muovisessa ensilusikassa kontaktimateriaalivirhe",
                        "url": "https://www.ruokavirasto.fi/elintarvikkeet/ohjeita-kuluttajille/takaisinvedot-2023-/muovisessa-ensilusikassa-kontaktimateriaalivirhe/"}
    assert items[1]["title"] == "Pakastetussa chilissä kadmiumylitys" and items[1]["date"] == "2026-10-27"


def test_the_list_and_a_notice_are_read_with_the_documented_scrape_calls():
    bodies = []

    def handler(req):
        body = json.loads(req.content)
        bodies.append((str(req.url), body))
        if body["formats"].get("html"):
            page = '<p class="d"> 6. lokakuuta 2026 </p><h3><a class="l" href="/elintarvikkeet/x/patonki/"> Patonki </a></h3>'
            return httpx.Response(200, json={"html": {"requested": True, "success": True, "data": page}})
        return httpx.Response(200, json={"json": {"requested": True, "success": True, "data": {
            "company": "Lunden Food OÜ", "brand": "Chef Lundén", "lot": "use by 07.10.2026", "hazard": "allergen",
            "products": [{"name": "Chef Lundén Kana-caesar patonki 200 g", "ean": None}], "allergens": ["fish", "anchovy"],
            "summary": "Anchovy is not declared."}}})

    c = client(handler)
    items = c.recall_list("https://www.ruokavirasto.fi/elintarvikkeet/list/")
    assert items == [{"date": "2026-10-06", "title": "Patonki", "url": "https://www.ruokavirasto.fi/elintarvikkeet/x/patonki/"}]
    n = c.read_notice(items[0], now=1_794_650_000.0)
    assert bodies[0] == ("https://api.context.dev/v1/web/scrape", {"url": "https://www.ruokavirasto.fi/elintarvikkeet/list/", "formats": {"html": True}})
    assert bodies[1][1]["formats"] == {"json": True} and bodies[1][1]["jsonParams"]["schema"] == NOTICE_SCHEMA
    assert n.id == "ruokavirasto.fi/elintarvikkeet/x/patonki" and n.official and n.source == "ruokavirasto.fi"
    assert n.brand == "Chef Lundén" and n.allergens == ["fish"] and n.eans == [] and n.extracted_by == "context.dev"
    assert n.date == "2026-10-06" and n.detected_at == 1_794_650_000.0 and n.extract_ms is not None


def test_a_notice_read_before_nine_on_its_date_counts_at_once():
    c = client(lambda req: httpx.Response(200, json={"json": {"requested": True, "success": True, "data": {}}}))
    item = {"date": "2026-11-13", "title": "x", "url": "https://www.ruokavirasto.fi/x/"}
    early = ts("2026-11-13", hour=7) + 35 * 60
    assert c.read_notice(item, now=early).published_at == early
    assert c.read_notice(item, now=early + 86400).published_at == ts("2026-11-13")   # a later read keeps 09:00 on the date


def test_two_addresses_that_differ_only_by_the_query_are_two_notices():
    assert notice_id("https://www.example.fi/recall?id=1") != notice_id("https://www.example.fi/recall?id=2")
    assert notice_id("https://www.example.fi/recall/") == "example.fi/recall"


def test_a_failed_read_is_an_error_and_not_an_empty_notice():
    c = client(lambda req: httpx.Response(200, json={"json": {"requested": True, "success": False, "data": None,
                                                              "error_code": "fetch_failed", "message": "no text"}}))
    try:
        c.read_notice({"url": "https://example.fi/x", "title": "x"}, now=1.0)
        assert False
    except RuntimeError as e:
        assert "fetch_failed" in str(e)


def test_a_monitor_runs_every_ten_minutes_with_our_tags():
    seen = {}

    def handler(req):
        seen["body"] = json.loads(req.content)
        return httpx.Response(201, json={"id": "mon_1", "initial_run_id": "run_1"})

    spec = {"name": "Recall Watch: x", "kind": "list", "target": {"type": "page", "url": "https://example.fi"}}
    client(handler).create_monitor(spec)
    assert seen["body"]["schedule"] == {"type": "interval", "frequency": 10, "unit": "minutes"}
    assert seen["body"]["tags"] == ["recall-watch", "kind-list"]
    assert seen["body"]["target"] == spec["target"]


def test_changes_page_through_and_keep_only_our_monitors():
    calls = []

    def handler(req):
        calls.append(dict(req.url.params))
        if "cursor" not in req.url.params:
            return httpx.Response(200, json={"data": [
                {"id": "chg_1", "monitor_id": "mon_1", "target_type": "page", "title": "New notice", "detected_at": "2026-11-14T10:00:00Z"},
                {"id": "chg_2", "monitor_id": "mon_other", "title": "Not ours"}], "has_more": True, "next_cursor": "c2"})
        return httpx.Response(200, json={"data": [
            {"id": "chg_3", "monitor_id": "mon_2", "target_type": "search", "title": "Recall", "url": "https://e.fi/c"}],
            "has_more": False, "next_cursor": None})

    out = client(handler).changes(since=1_763_114_400, kinds={"mon_1": "list", "mon_2": "web"})
    assert calls[0]["since"] == "2025-11-14T10:00:00Z" and calls[1]["cursor"] == "c2"
    assert [(c["id"], c["kind"]) for c in out] == [("chg_1", "list"), ("chg_3", "web")]


def test_a_webhook_signature_is_checked_as_the_docs_say():
    body = b'{"id":"evt_1","event":"change.detected"}'
    assert verify_webhook(body, sign(body, 1000), SECRET, now=1000)
    assert not verify_webhook(body + b" ", sign(body, 1000), SECRET, now=1000)      # changed body
    assert not verify_webhook(body, sign(body, 1000, "other"), SECRET, now=1000)    # other secret
    assert not verify_webhook(body, sign(body, 1000), SECRET, now=1301)             # too old
    assert not verify_webhook(body, "t=1000", SECRET, now=1000)                     # no digest
    assert not verify_webhook(body, sign(body, 1000) + ",t=1", SECRET, now=1000)    # repeated key
    assert not verify_webhook(body, sign(body, 1000), "", now=1000)                 # no secret set
    digest = sign(body, 1000).split("v1=")[1]
    assert not verify_webhook(body, "t=1000,v1=" + "é" * 64, SECRET, now=1000)      # a digest that is not hex
    assert not verify_webhook(body, "t=100²,v1=" + digest, SECRET, now=1000)        # a digit that is not ASCII
