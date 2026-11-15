"""Small, strict stock-feed importer used by the demo and the pilot API.

The feed is intentionally boring CSV.  A kitchen can export it from a purchasing or stock
system without trusting an AI to infer barcodes or physical lots.  Each row is one stock or
incoming-order candidate; duplicate IDs are rejected rather than silently merged.
"""

from __future__ import annotations

import csv
import io

from .engine import Product

REQUIRED = {"name"}
OPTIONAL = {
    "id", "name", "brand", "ean", "supplier", "stock_location", "candidate_stock", "next_service", "open_order", "lot", "expiry",
}


def products_from_csv(text: str) -> list[Product]:
    if len(text.encode("utf-8")) > 2_000_000:
        raise ValueError("stock import is too large")
    reader = csv.DictReader(io.StringIO(text))
    headers = set(reader.fieldnames or [])
    missing = REQUIRED - headers
    if missing:
        raise ValueError(f"stock import needs column(s): {', '.join(sorted(missing))}")
    products, ids = [], set()
    for line, row in enumerate(reader, start=2):
        clean = {(k or "").strip(): (v or "").strip() for k, v in row.items() if (k or "").strip() in OPTIONAL}
        if not clean.get("name"):
            continue
        pid = clean.get("id") or clean.get("ean") or f"row-{line}"
        if pid in ids:
            raise ValueError(f"duplicate stock id '{pid}' on row {line}")
        ids.add(pid)
        products.append(Product(id=pid, **{k: v for k, v in clean.items() if k != "id"}))
    if not products:
        raise ValueError("stock import contains no products")
    return products
