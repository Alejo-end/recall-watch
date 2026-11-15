# Recall Watch: the Context.dev loop

## The user and the decision

The user is the recall or compliance lead at a retailer, a brand owner, a marketplace or a consumer organisation.

After an official recall, this person decides which public product pages need a human check and a follow-up with the page owner. They do not decide from the web whether food is unsafe. They measure whether public pages caught up with the official notice.

## The loop as it runs

```text
Finnish Food Authority list            Context.dev list monitor, and an hourly list read
        ↓ a new notice
Case                                   structured read of the notice: products, GTIN, lots, photos,
                                       Finnish headline and a labelled machine translation
        ↓
Candidate pages                        Context.dev search, limited to the approved domains
        ↓ a person approves a page
First read + page monitor              fresh structured read (never cached) and one Context.dev page monitor
        ↓ the monitor reports a change, or the hourly fallback, or "Check page now"
New observation                        state, product identity, verbatim excerpt, page text hash,
                                       screenshot, product photo, Context.dev request id
        ↓
Review queue                           next step in words: contact the owner, confirm sending,
                                       confirm identity, resolve with an outcome
        ↓
Audit trail (CSV)                      every observation and every action, with times
```

A page that changes after it was resolved re-opens on its own. A page without a supported product identity gets the state `unverified`. The SLA (service level agreement) is 4 hours from the notice. A partial name match stays out of the SLA. A person must first compare the official photo with the page photo.

## What the running app shows

The live tab holds three real cases, opened from the Food Authority list on 14 November 2026:

| Case | Watched pages | What the pages showed |
|---|---|---|
| Arla Protein puddings, possible glass fragments, 26 October | Arla, S-kaupat, K-Ruoka | K-Ruoka still shows the vanilla pudding with a price and "Add to cart". S-kaupat shows "Ei valikoimassa" (not in assortment). Arla's product page shows no recall message. |
| Coop Chopped Chili, cadmium above the limit, 27 October | S-kaupat | SOK recalled the product. SOK's own shop still lists it at about €1.19 under the official GTIN 7340191134195. |
| Nuby first spoons, 11 November | none yet | Opened by the list check. Search found no page that names the product closely. |

All three recalls name specific lots. A page that still lists the product can therefore sell newer stock. The screen asks the owner which lot the page sells. It does not allege a breach.

Every watched page has a Context.dev page monitor that runs every 10 minutes. The evidence receipt of each row shows the monitor's last and next run, read from Context.dev.

The live proof tab runs the same loop on a public test page that the team owns (a GitHub gist with an invented product). The app edits the page and asks Context.dev to run the page monitor now. The screen shows each step: the monitor's change id, the fresh scrape, the new receipt and the review queue. On 15 November 2026 the chain took 42 to 57 seconds. The test case has its own database and never shares a row with the real cases.

The demo tab is a separate, labelled replay on a simulated clock. It shows a complete case, with measured lags and one SLA breach. It never shares a row or a chart with the live tab.

## Proof to show a judge

1. Open the live tab. It opens on the chili case. Click "Evidence receipt". The receipt shows the Context.dev request id, "cache miss" and the monitor status. It shows the identity basis "Official GTIN found on the page". It shows a verbatim excerpt, checked against the stored page text, and the SHA-256 of that text. It shows the screenshot and the stored official photo next to the stored page photo.
2. Click "Check page now". A fresh read takes about 7 seconds (7.2 s on 14 November at 21:10). The row then says whether the state changed, and the new observation has its own receipt.
3. Open the live proof tab and click "Change the page now". The chain shows the monitor run, the change id, the fresh scrape and the new receipt. A resolved test page re-opens.
4. Under "Review queue", click "Download the audit trail (CSV)".

## What is not proven yet

- No live lag is measured. Watching started 18 to 20 days after these notices. The screen therefore reports "still appears purchasable N days after the notice". It never says "took N days to update".
- No retailer page changed yet, so no retailer page has a monitor-triggered re-read. The receipts show that the monitors run. The monitor-triggered path runs live on the team-owned test page.
- The state classifier has no accuracy study. Reviewers can record "classification wrong", and the summary counts these corrections. The chili page shows a price but asks the shopper to choose delivery before it shows availability, so its "appears purchasable" is open to dispute.
- Names in the audit trail are typed by the operator. The pilot has no user accounts.

## Pilot that turns this into a product

1. One retailer or brand owner approves 5 to 10 product pages and names a person who receives follow-ups.
2. The list check opens a case for each new notice. The team watches the approved pages from the first hour.
3. Calibrate the classifier on 20 manual checks. Report precision for each state.
4. Track the median time from notice to a closed purchase path, the share of pages inside the 4 hour SLA, and reviewer corrections.
