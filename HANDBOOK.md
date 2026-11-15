# Recall Watch handbook

## What it is

Recall Watch is a Context.dev product for a recall or compliance lead. It answers one question: after an official recall, which public product pages still need a human check?

It is not an inventory system. A page state is the visible text of the page at the time of the Context.dev read. "Appears purchasable" does not prove that a recalled lot was in stock or sold.

"Afterimage" is the name of one feature: the timeline of what each public page still showed after the notice.

## The three tabs

The live tab shows real cases. Each row is a stored Context.dev observation of a real public page. The data is in `data/afterimage.sqlite`. Page text, screenshots and photos are in `data/artifacts/`. Git ignores both.

The first screen of the live tab shows one sentence and the strongest row of the featured case (`afterimage.featured_notice_url` in `watch.yaml`). Everything else is under "Review queue, other cases, add a page, Afterimage timeline". The "Pilot impact" block under it shows what a pilot measures and where the prototype stands today.

The live proof tab changes a test page that the team owns, and shows each step of the loop as it happens. See "The controlled live proof" below.

The demo tab replays a simulated case on a simulated clock. It has its own banner and endpoint (`GET /afterimage/demo`). It never shares a row or a chart with the live tab.

## What a reviewer does

Each row states its next step. The actions:

| Button | Effect |
|---|---|
| Contact owner… | Drafts a message with the notice, GTIN, lots and the page evidence. The app sends nothing. It stores a draft. |
| I sent it… | Records the sent message, the recipient and the reply deadline. The page then waits on the owner. |
| `Confirm product identity…` | Shown for a partial name match. Compare the stored official photo with the stored page photo. |
| Record the result… / Resolve… | Closes the page with one of five outcomes. Some outcomes need a note. "Page updated" is refused while the last read still shows the product for sale. |
| Check page now | One fresh Context.dev read (about 6 credits), at most once every 5 minutes for each page. |
| Evidence receipt | Five blocks first: the notice and the product identity, the Context.dev call, the verbatim excerpt, the screenshot, and why the page needs review. The full record is under "Full receipt". |

If the reply deadline passes or the page changes, a waiting page needs action again. If the page changes, a resolved page re-opens. Names are typed by the operator and are not verified.

## How a case starts

- The list check reads the Food Authority list every hour. If the Context.dev list monitor reports a change, the list check runs at once. A notice from the last 3 days opens a case.
- An operator pastes a ruokavirasto.fi notice address into "Open a case".
- On first start with a key, the pilot case from `watch.yaml` (`afterimage.seed_case`) opens with its three approved pages.

Each page stores how it was found: the search query and time for a suggestion, the `found` text for a seeded page, or "pasted by a person".

## The controlled live proof

A retailer will not change its page while a judge watches. The live proof uses a page that the team owns: a public GitHub gist with an invented product ("Demo Chili 400 g", EAN 2000000000015). The EAN prefix 200 is for in-store numbering only, so it names no real product. The page says on its face that it is a demo page and not a real shop.

Set it up once on the demo machine. The command needs `CONTEXT_DEV_API_KEY` and a GitHub login (`gh auth login`, or `GITHUB_TOKEN` in `.env`):

```sh
uv run python -m recall.proof setup
```

The command publishes the gist, opens the test case in its own database (`data/proof.sqlite`) and creates the page's Context.dev monitor. It stores the gist address in `data/proof.json`. A second run reuses both.

A click on "Change the page now" does this:

1. The app edits the gist: the product is for sale, or it carries a recall notice.
2. The app asks Context.dev to run the page monitor now (`POST /v1/monitors/{id}/run`) and waits for the run.
3. The normal loop reads Context.dev's change feed and re-reads the page with a fresh structured scrape. The observation keeps the change id.
4. The new observation has its own evidence receipt.
5. The review queue updates. If the page offers the product again, a resolved page re-opens.

On 15 November 2026 the full chain took 42 to 57 seconds, about 28 seconds of it in the monitor run. If the monitor reports no change, the app reads the page directly and labels that step "fallback".

To show a re-open, start from a page that shows the recall notice. Resolve it with "Resolve as page updated". Then change the page back on sale.

The test case never shares a database, a row, a chart or an export with the real cases. Its receipts say "Test page owned by the team". Each run costs about 7 credits: 1 for the monitor run and about 6 for the scrape. The monitor also runs every 10 minutes (1 credit each run).

## Guard rails

Configure these under `afterimage:` in `watch.yaml`:

- `allowed_domains`: only these domains can be watched. The test case allows only `gist.github.com`, and only one page.
- `max_targets_per_case`, `max_cases`, `min_rescrape_seconds`, `scrape_budget_per_hour`.
- `AFTERIMAGE_WRITE_KEY` in `.env`: if it is set, every change needs this operator key. If it is not set, only this computer can make changes, and other devices can only view.

Photo downloads accept only public https addresses and raster image types, up to 5 MB.

A restart does not read a page again for a monitor change that already produced an observation.

## Run

From `recall-watch/`, with `CONTEXT_DEV_API_KEY` in `.env`:

```sh
uv run uvicorn recall.app:app --host 0.0.0.0 --port 8030
```

The live loop runs whenever a Context.dev key is set. Set `AFTERIMAGE_LIVE=0` to stop it. If the live loop stops polling, `GET /health` returns 503.

The folder also holds an earlier kitchen shelf prototype. Its routes stay for its regression tests and are hidden from the API schema. Its poller spends Context.dev credits, so it runs only with `KITCHEN=1`.

Other systems can act on the prototype's answer. `GET /blocked` lists every open "quarantine" hit on the shelf with the EAN code, source notice, and `hold_and_verify_lot` action. A hit is open while it is new or held for its lot check. `POST /orders/check` accepts proposed order items and returns `allow`, `review`, or `hold_and_verify_lot` with source evidence. It holds the same matches: an official notice with the same EAN code. A match on brand and name alone gives `review`. A hold is not a claim that every future batch is unsafe: a person checks the supplier's lot and expiry details before ordering.

## Evidence in the repository

`data/` is ignored by git. A copy of the live evidence is in `evidence/`, and it is safe to publish. It keeps every case, page and observation, with the state, the Context.dev request id and the verbatim excerpt. It also keeps the SHA-256 of each page text and page photo. It keeps the Food Authority's notice photos. It leaves out the retailers' full page text, screenshots and product photos, because they are the retailers' content. It also leaves out reviewer actions and owner addresses. The receipt says which originals are withheld, and the hashes identify them on the demo machine. The test case of the live proof is not in this copy.

If `data/` has no database at start, the app restores this copy by itself.

```sh
uv run python -m recall.evidence export          # refresh the published copy from data/
uv run python -m recall.evidence export --full   # a private copy that also holds the retailer pages
uv run python -m recall.evidence restore         # reset data/ to the copy; the old database is kept as a .bak file
```

Reads made before receipts existed (14 November, 19:38 to 20:35) show "no receipt" in the trail. Each row's "Evidence receipt" button opens the latest read.

## Verification

```sh
uv run pytest
```

97 tests pass. They cover the timeline and SLA rules, the review states, identity matching and the guard rails. They also cover receipts, stored photos, the replay, the live proof and the Context.dev request shapes. They use a fake Context.dev client and a fake gist, and spend no credits.

For the spoken pitch, use [PITCH.md](PITCH.md). For the loop and the current evidence, use [WINNING_LOOP.md](WINNING_LOOP.md).
