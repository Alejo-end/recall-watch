# Recall Watch pitch

## The opening, 30 seconds

"A food recall was published. Eighteen days later, the official shop still presented the exact GTIN as a normal product page.

Recall Watch tells the responsible person where public information has not caught up, and keeps the evidence needed to act.

Every case starts from the Finnish Food Authority's notice. Context.dev monitors the approved retailer and manufacturer pages. When a page changes, Recall Watch reads it again and keeps a receipt: the request id, the words on the page and a screenshot. The compliance lead sees only the pages that need a decision."

## Demo script, under two minutes

Before the demo:

1. Start the app with `CONTEXT_DEV_API_KEY` in `.env`. The live tab opens on the chili case.
2. Type a name in the header.
3. On the live proof tab, the test page shows the recall notice and its review is "Resolved". If not, change the page to the recall version, then click "Resolve as page updated".

The script:

1. Live case. Read the headline aloud: "1 public page still needs review after this official recall." Point to the row: the official recall, "Exact GTIN match", and S-kaupat still showing a price, 18 days after the notice. SOK recalled the product, and S-kaupat is SOK's own shop.
2. Click "Evidence receipt". Point to the five blocks in order. The notice and the GTIN match. The Context.dev request id, its time and "fresh read (cache miss)". The verbatim words, "found word for word in the stored page text". The screenshot. The reason for review. Say: "This is not a generated compliance dashboard. Every line traces back to one Context.dev call."
3. Live proof tab. Say: "We own this page. It is labelled as a test and kept apart from the retailer evidence." Click "Change the page now: back on sale". The chain fills in about 45 seconds: the page edit, the Context.dev monitor run and its change id, the fresh scrape, the new receipt, and the resolved page re-opening in the queue. Open the new receipt.
4. Back on the live case, open "Review queue". Show "Contact owner…": the draft carries the notice, the GTIN, the lot and the page evidence. Show the Afterimage timeline and "Download the audit trail (CSV)".
5. Point to "Pilot impact". A pilot runs on 10 approved domains and opens a case within 10 minutes of a notice. It measures a 4-hour propagation SLA, the median time to correction, and the count of human corrections.

## Questions, and the answers

"Does 'appears purchasable' mean that recalled food was sold?" No. It means normal purchase words were on the page. The recall names one best-before date, so the next step asks the owner which lot the page sells. We do not accuse. We show the gap.

"How real-time is it?" Each approved page has a Context.dev page monitor that runs every 10 minutes. The live proof tab shows the full chain on a page we control, in under a minute. The retailer pages have not changed since we started watching them, so they show monitor runs with "no change".

"Did you measure a real propagation lag?" Not yet. We started watching 18 to 20 days after these notices, so the screen reports "still appears purchasable N days after the notice". It never says "took N days to update". A pilot that watches from the first hour measures the lag.

"Can the model invent the product match?" No. The product identity comes from code: the official GTIN on the page, in its address or as its EAN. The model only classifies the visible page state, and a reviewer can correct it. Each correction is counted.

"Who sends the message to the shop?" The person. Recall Watch drafts it. After the person confirms that it was sent, Recall Watch records the time. It sends nothing itself.

## Why this scores for Context.dev

| Criterion | Proof |
|---|---|
| Real-world impact | A named owner gets a short queue of public pages to fix after an official recall, with the evidence attached. |
| Creative use of live web data | It joins an authoritative recall notice with changing shop pages, and keeps their state over time. A one-off search answer cannot do that. |
| Completeness | Three real cases from the Food Authority list, stored in SQLite. Drafts, sent messages, outcomes, automatic re-opening and a CSV audit trail. |
| Technical execution | Fresh reads only (cache miss in every receipt). Product identity computed by code. Verbatim excerpt found word for word in the stored page text, with its SHA-256. Screenshot from the same call. |
| Real-timeness | A page monitor on every approved page. On the team-owned test page, the chain from page edit to new receipt runs live in front of the judges. |
