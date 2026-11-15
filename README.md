# Recall Watch

Our entry to "The Web Is Your Real-Time Database" by Context.dev. Read `HANDBOOK.md` for the full explanation.

## In brief

Recall Watch finds the public product pages that have not caught up with an official recall. It turns each one into an accountable follow-up.

A recall is a public notice that a food product must leave the market. The Finnish Food Authority lists these notices on a web page, as free text in Finnish. Context.dev finds the notices, reads their fields and watches the public pages that sell the product.

A food recall was published. Eighteen days later, the official shop still presented the exact GTIN as a normal product page. Recall Watch tells the responsible person where public information has not caught up, and keeps the evidence needed to act.

The user is the recall or compliance lead at a retailer, a brand owner or a marketplace.

## What the screen shows

The first screen states one sentence, for example "1 public page still needs review after this official recall". Under it is the strongest row: the official recall, the product match, what the public page shows now, and two buttons.

- Evidence receipt: the official notice and the product identity, and the Context.dev request id and time. It also shows a verbatim excerpt from the page, the screenshot, and why the page needs review.
- Check page now: one fresh Context.dev read of the page.

The review queue, the other cases, "add a page" and the Afterimage timeline are under "Review queue" on the same screen. The Afterimage timeline shows what each page still showed after the notice.

The "Live proof" tab changes a public test page that the team owns. Context.dev detects the change, Recall Watch reads the page again, a new receipt appears and the queue updates. The test page is a public GitHub gist. It is stored apart from the real cases and is never mixed with retailer evidence.

## The earlier kitchen screen

![The kitchen screen at the level quarantine for one product. It shows the reason, the lot and the recent notices.](docs/screenshots/screen.png)

The picture shows the earlier kitchen shelf prototype, in the demo replay of real notices from 2026. The shelf is our example of a school kitchen with 12 products. Its code is still in the folder (`recall/engine.py`, `recall/replay.py`, the `shelf` list in `watch.yaml`). Its routes stay for its regression tests. They are hidden from the API schema, and its poller runs only with `KITCHEN=1`.

It has three levels:

| Level | The screen says | When |
|---|---|---|
| 0 | Clear | No open notice names a product on the shelf |
| 1 | Check | A notice names the same brand and name, or a similar name |
| 2 | Quarantine and verify | An official notice has the same EAN code as a product on the shelf |

## Run

From `recall-watch/`, with `CONTEXT_DEV_API_KEY` in `.env`. You need `uv`.

```sh
uv run uvicorn recall.app:app --host 0.0.0.0 --port 8030
```

Open http://localhost:8030/. Without a key, the screen shows the stored evidence, read-only.

To set up the live proof once on the demo machine:

```sh
uv run python -m recall.proof setup
```

This command publishes the test page as a public gist under the GitHub account of `gh auth login`, and creates its Context.dev monitor.

## Tests

```sh
uv run pytest
```

All 97 tests pass. They use a fake Context.dev client and a fake gist. They spend no credits.

## What is true today

1. A code review on 14 November found faults in the kitchen prototype. Three of them let an order through for a product under an open recall. All are fixed, and a test covers each one.
2. `GET /blocked` and `POST /orders/check` agree: both hold a product that the kitchen screen quarantines.
3. The guard rails are in `HANDBOOK.md`, under "Guard rails".

## More

- [HANDBOOK.md](HANDBOOK.md): how the loop, the review process and the guard rails work.
- [PITCH.md](PITCH.md): the spoken pitch and the demo script.
- [WINNING_LOOP.md](WINNING_LOOP.md): the Context.dev loop and the current evidence.
