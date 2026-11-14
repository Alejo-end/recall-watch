"""Count how the 2026 notices of the snapshot match a shelf, for a check by hand.

    uv run python -m recall.count               # the shelf of watch.yaml
    SHELF=valio uv run python -m recall.count   # the Valio challenge catalog, when its file is on this machine
"""

from __future__ import annotations

from .config import load_env, load_shelf, load_watch
from .engine import LEVEL_NAMES, match
from .replay import load_notices


def main() -> None:
    load_env()
    shelf, name = load_shelf(load_watch())
    notices, about = load_notices()
    print(f"{len(notices)} notices of 2026 (read on {about['read_on']}) against {len(shelf)} products: {name}\n")
    totals = {1: 0, 2: 0}
    touched = 0
    for n in notices:
        hits = [(lv, why, p) for p in shelf for lv, why, _ in [match(n, p)] if lv]
        touched += bool(hits)
        for lv, why, p in sorted(hits, key=lambda h: -h[0]):
            totals[lv] += 1
            print(f"{n.date}  {LEVEL_NAMES[lv]:5}  {p.name[:50]:50}  <- {'; '.join(n.products)[:60]}  ({why})")
    print(f"\n{touched} of {len(notices)} notices name a product on the shelf. Hits: {totals[2]} quarantine, {totals[1]} check.")


if __name__ == "__main__":
    main()
