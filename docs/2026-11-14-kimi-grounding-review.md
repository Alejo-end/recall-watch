# Kimi source-grounding review — 14 November 2026

This is a **second-model source-grounding review**, not a human extraction-accuracy study.

## Method

1. Context.dev read the current Finnish Food Authority recall list.
2. For each of the first three notices, Context.dev returned source HTML and the structured Recall Watch extraction.
3. Kimi K3.1 Pro, through Verda, received the readable source text and extraction. It labelled each field `supported`, `contradicted`, `not_stated`, or `unclear`.
4. The result could not create, release, upgrade, or close a Recall Watch case.

Command:

```bash
uv run python -m recall.audit_batch --limit 3 --out /private/tmp/recall-grounding-review.json
```

## Result

All three notices were labelled `supported` overall. Every non-empty extracted company, brand, product, hazard, and summary field was labelled `supported`; the sample's non-empty EAN and lot fields were also labelled `supported`. Empty EAN, lot, and allergen values were labelled `not_stated`. There were no `contradicted` or `unclear` labels and no concerns in this sample.

| Notice | Overall | Notes |
|---|---|---|
| Muovisessa ensilusikassa kontaktimateriaalivirhe | Supported | No EAN, lot, or allergens stated |
| Pakastetussa chilissä kadmiumylitys | Supported | EAN and lot stated and supported |
| Proteiinivanukkaissa mahdollisesti lasinsiruja | Supported | Lot stated and supported; no EAN or allergens stated |

## Limit

Kimi is reading the same source after Context.dev. Agreement is useful for prioritising manual review, but it is not independent ground truth and does **not** establish field accuracy, recall, or safety. A human ten-notice audit remains required before any accuracy claim.
