# Handbook: Recall Watch

This handbook explains our entry to "The Web Is Your Real-Time Database" by Context.dev. The brief is in `CHALLENGE_BRIEFS.md`. The entry replaces the S-Pankki entry, which had no code. Its first version, Station Pulse, was about transport. We changed the subject on 14 November, because the transport operator's own app already answers the rider.

## What it is

A screen in a professional kitchen answers one question: is anything on my shelf recalled right now?

A recall is a public notice that a food product must leave the market. In Finland, the Finnish Food Authority (Ruokavirasto) lists these notices on a web page. Each notice is free text in Finnish. It names the product, the lot and the reason.

The screen answers in three levels, in large type, with the product, the reason, the lot and the source:

| Level | The screen says | When |
|---|---|---|
| 0 | Clear | No open notice names a product on the shelf |
| 1 | Check | A notice names a product of the same brand and name, but another pack or code |
| 2 | Quarantine and verify | An official notice names a product on the shelf: same EAN code, or same brand and name. Check the lot before confirming it is affected |

An EAN code is the number under the barcode of a pack. A notice counts for 30 days after its date. A person first quarantines a hit and checks the lot on the physical pack. They can record "Quarantined", "Confirmed affected", or "Another lot". The hit then stops counting, and the screen keeps the name and the day.

The design rule is the same as in our Valio entry. Context.dev finds the notices and reads their fields. Plain code decides the answer (`recall/engine.py`).

## Why it matters

1. Of the 38 notices of 2026 on the list, 14 name an allergen: milk, egg, fish, gluten, peanut, nuts, soy or mustard. One example is a "milk-free" rice porridge that is suspected to hold milk (3 August 2026). In a school kitchen, a missed notice of this kind reaches a child with an allergy.
2. Recalls also reach professional kitchens. The notice about alfalfa sprouts (25.5.2026) names the 1 kg pack for professional kitchens.
3. We compared the 38 notices with the 17,546 products of the Valio Aimo challenge catalog. 9 notices name a product in it: 7 products at the level "quarantine", 5 of them by the same EAN code, and 14 at the level "check". The brief says that this catalog is synthetic, so this is not a fact about Valio. It shows that the matching works on a catalog of real size. Run it with `SHELF=valio uv run python -m recall.count`.

We do not know how a kitchen learns of a recall today, or how fast. Nobody asked a kitchen yet (`ASSUMPTIONS.md`).

## How it uses live web data

| Source | Context.dev call | How often | What it gives |
|---|---|---|---|
| The authority's recall list | A page monitor with instructions, so a judge reports only a new notice | Every 10 minutes, the shortest schedule | The signal that the list has a new notice |
| The same list | Scrape as HTML | After each change, and once an hour | The date, title and address of each notice |
| Each new notice | Scrape with a JSON schema | Once for each notice | Company, brand, products with EAN codes, lot, hazard, allergens, one English sentence |
| Recalls announced elsewhere | A search monitor, then the same JSON scrape of each result | Every 10 minutes | A recall that a manufacturer or a shop chain announced. It gives "check" at most, because it is not from the authority |
| News | News search | Every 30 minutes | Articles about recalls. The screen lists them. They give no level |

The calls and their shapes come from docs.context.dev, read on 14 November. `recall/contextdev.py` names the page of each one. The backend reads all monitor changes every 2 minutes. Context.dev can also push a change by webhook, and the backend then reads at once. That needs an address that the internet can reach, so the demo polls.

The costs are from docs.context.dev/account/credits. A page monitor run costs 1 credit, and a search monitor run costs 2. A page scrape costs 1 credit, and a scrape with fields costs 5. A news search costs 1 credit for each 10 results. With the settings in `watch.yaml`, that is about 500 credits a day, plus 190 once for the 38 notices of this year. The team code gives 50,000 credits.

Other systems can act on the answer. `GET /blocked` lists every open "quarantine" hit on the shelf with the EAN code, source notice, and `hold_and_verify_lot` action. `POST /orders/check` accepts proposed order items and returns `allow`, `review`, or `hold_and_verify_lot` with source evidence. It holds the same matches: an official notice with the same EAN code, or with the same brand and name. A hold is not a claim that every future batch is unsafe: a person checks the supplier's lot and expiry details before ordering.

## Run it

Run every command from `recall-watch/`. You need `uv`.

With no key, the screen replays the real notices of 2026 from `fixtures/notices-2026.json`. One day passes in 2 seconds, and the year starts again at its end. The screen says "Replay" at the bottom:

```
uv run uvicorn recall.app:app --host 0.0.0.0 --port 8030
```

Open `http://localhost:8030/`. Add `?kiosk` to hide the mouse. To start the replay later in the year, set `REPLAY_FROM=2026-10-22`. To slow it, set `REPLAY_DAY_S=6`.

With live data:

1. Sign up at context.dev/signup and redeem the code JUNCTION26 at context.dev/dashboard/redeem-credits.
2. Copy `.env.example` to `.env` and put the key in it.
3. Create the monitors: `uv run python -m recall.setup_monitors`, then the same with `--create`.
4. Start the backend as above. On start, it reads the list and the notices of this year. It keeps them in `data/recall.sqlite`, so a restart reads nothing twice.

Run the tests: `uv run pytest`. 31 tests pass.

## How the judges score

The judges' questions are from the challenge page, read on 14 November. Each criterion counts 20%.

| Criterion | The judges ask | Where we stand |
|---|---|---|
| Real-world impact | Does this connect to a real business or consumer need? | Fair. 14 of 38 notices this year name an allergen, and one names a pack for professional kitchens. We asked no kitchen how it learns of recalls today |
| Creative use of live web data | One scrape call, or an interesting combination of different sources? | Fair. A page monitor, a page scrape, field extraction with a schema, a search monitor and news combine with the kitchen's own product list. No call ran live yet |
| Completeness of demo | How ready is it for production? | Fair. The state is on disk, a person records each action, and the screen shows stale data. Missing: a live run, sign-in for the person who records an action, and the link to the gateway |
| Technical execution | Latency and accuracy of data | Weak until the live run. The screen shows the delay of each notice and the time that Context.dev took to read it, but only live data fills these. For accuracy, see "What we checked by hand" |
| Real-timeness | Does it react to change over time, or take a one-off snapshot? | Fair. Monitors run every 10 minutes, notices fade after 30 days, and the year line shows each notice. The list gets about one notice a week, so a live demo mostly says "Clear". The replay shows the change over time |

## What we checked by hand

1. On the demo shelf, 7 of the 38 notices give a hit: 6 "quarantine" and 1 "check". We chose these seven products for that, so this result proves only that the code runs.
2. On the Valio challenge catalog, an AI model read all 21 hits. Of the 7 "quarantine" hits, 5 have the same EAN code as the notice. The other 2 have the same brand, name and pack size. Of the 14 "check" hits, about half are the recalled product in another pack. The others are another product of the same line, for example a flavoured oat drink for a recall of the plain one. An earlier rule gave one wrong hit: a red curry of another brand. We removed that rule, and a test covers it.
3. Nobody counted the recalled products that the matching misses. A product with another spelling of its name and no EAN code in the notice is missed.

## What is true today

| Part | State |
|---|---|
| Rule from notices to the answer (`recall/engine.py`) | Built, with tests |
| Context.dev calls (`recall/contextdev.py`) | Built from the documentation. Tested against a fake server. The backend never ran against the live API: it has no key |
| Two live calls by hand, through Context.dev's MCP server, on 14 November | The list scrape returned the list with the markup that our parser reads. The field scrape of the egg notice of 9 October returned the company, the four products and the four EAN codes, the same as in our snapshot. It wrote the lot and the summary in Finnish, so the instructions now ask for English. One notice is not a measure of accuracy |
| List parser | Tested against a part of the real page, saved on 14 November |
| Snapshot of 2026 (`fixtures/notices-2026.json`) | 38 notices. Title, date and address are from the page. An AI model read the other fields. Nobody checked them a second time |
| Replay (`recall/replay.py`) | Works |
| Screen (`web/index.html`) | Works on the replay at 1512 by 778, checked in a browser on 14 November. Not checked at phone width |
| Record of an action (`POST /ack`) | Works. It takes any name, with no sign-in |
| Webhook receiver (`/webhooks/context`) | Built, and the signature check is tested. Not used in the demo: it needs a public address |

## Limits to say out loud

1. The screen cannot see the lot on the pack. It says "quarantine and verify" for an official match, and a person compares the lot before confirming the product is affected.
2. Matching uses the EAN code, or the brand and the words of the name. A notice with no EAN code and another spelling is missed.
3. The shelf list is a file that we wrote. In a real kitchen it must come from the stock or the orders, for example from the Valio gateway.
4. A notice counts for 30 days. That number is our choice. A product with a long shelf life can stay on a shelf longer.
5. The authority's list is in Finnish. Context.dev reads the fields in the live run. It read one notice correctly. We do not know yet how well it reads the others.
6. A recall that a search finds is not checked by anyone. It gives "check" at most, and it can be old or about another country.
7. The monitors run every 10 minutes at most, so the screen can be 12 minutes behind the list.
8. We have no agreement with the Finnish Food Authority, and no kitchen uses the screen.
9. This is not a medical device and not an official warning channel. The notice at the source decides.
