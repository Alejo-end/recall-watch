"""A clean, committed copy of the live evidence, so a fresh checkout shows the same proof.

    uv run python -m recall.evidence export          # data/ -> evidence/, safe to publish
    uv run python -m recall.evidence export --full   # the same, with retailer page text, screenshots and page photos
    uv run python -m recall.evidence restore         # evidence/ -> data/ (the current data/ is kept as a dated backup)

`data/` holds the working database and is ignored by git. `evidence/` is in the repository. The
export keeps every case, page, observation, page text, screenshot and photo. It drops reviewer
actions and remembered owner addresses: those are test clicks and contact details, not evidence of
what the web showed. On start, the app restores the snapshot by itself when `data/` has no database.

By default the copy is safe to publish: it leaves out the retailers' full page text, screenshots and
product photos, which belong to the retailers. It keeps every observation with its state, Context.dev
request id, verbatim excerpt, SHA-256 of the page text and SHA-256 of the page photo, so a withheld
original can still be checked against its hash. The Food Authority's own notice photos are kept.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import time

from .config import ROOT

DATA_DB = ROOT / "data" / "afterimage.sqlite"
DATA_ART = ROOT / "data" / "artifacts"
SNAP_DB = ROOT / "evidence" / "afterimage-evidence.sqlite"
SNAP_ART = ROOT / "evidence" / "artifacts"


def _referenced(db: sqlite3.Connection) -> set[str]:
    names = set()
    for oid, has_shot, product in db.execute("SELECT id, has_screenshot, product FROM observations"):
        names.add(f"{oid}.md")
        if has_shot:
            names.add(f"{oid}.jpg")
        local = (json.loads(product or "{}") or {}).get("image_local")
        if local:
            names.add(local)
    for (images,) in db.execute("SELECT images FROM cases"):
        names.update(i["local"] for i in json.loads(images or "[]") if i.get("local"))
    return names


def export(src_db=DATA_DB, src_art=DATA_ART, dst_db=SNAP_DB, dst_art=SNAP_ART, full: bool = False) -> dict:
    os.makedirs(dst_art, exist_ok=True)
    if os.path.exists(dst_db):
        os.remove(dst_db)
    src = sqlite3.connect(src_db)
    src.execute("VACUUM INTO ?", (str(dst_db),))
    src.close()
    dst = sqlite3.connect(dst_db)
    dst.executescript("DELETE FROM actions; DELETE FROM owners;"
                      "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);")
    if not full:
        # The originals stay on the demo machine. The hashes stay here, and the page photo falls back
        # to its original address.
        for oid, product in dst.execute("SELECT id, product FROM observations").fetchall():
            p = json.loads(product or "{}")
            if p.pop("image_local", None):
                p["image_withheld"] = True
                dst.execute("UPDATE observations SET product = ? WHERE id = ?", (json.dumps(p, ensure_ascii=False), oid))
        dst.execute("INSERT OR REPLACE INTO meta VALUES ('retailer_artifacts', 'withheld')")
    dst.commit()
    keep = _referenced(dst)
    if not full:
        keep = {n for n in keep if n.startswith("case-")}   # only the Food Authority's notice photos
    dst.execute("VACUUM")
    dst.close()
    for old in os.listdir(dst_art):
        if old not in keep:
            os.remove(os.path.join(dst_art, old))
    copied = 0
    for name in keep:
        if os.path.exists(os.path.join(src_art, name)):
            shutil.copy2(os.path.join(src_art, name), os.path.join(dst_art, name))
            copied += 1
    return {"database": str(dst_db), "artifacts": copied}


def restore(dst_db=DATA_DB, dst_art=DATA_ART, src_db=SNAP_DB, src_art=SNAP_ART) -> dict:
    if not os.path.exists(src_db):
        raise FileNotFoundError(src_db)
    backup = None
    if os.path.exists(dst_db):
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup = f"{dst_db}.{stamp}.bak"
        shutil.copy2(dst_db, backup)
    os.makedirs(dst_art, exist_ok=True)
    shutil.copy2(src_db, dst_db)
    for name in os.listdir(src_art):
        shutil.copy2(os.path.join(src_art, name), os.path.join(dst_art, name))
    return {"database": str(dst_db), "backup": backup}


def restore_if_missing(db_path: str) -> bool:
    """Called on start: a fresh checkout gets the committed evidence instead of an empty screen."""
    if db_path == ":memory:" or os.path.exists(db_path) or not os.path.exists(SNAP_DB):
        return False
    restore(dst_db=db_path, dst_art=os.path.join(os.path.dirname(db_path), "artifacts"))
    return True


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "export":
        print(export(full="--full" in sys.argv))
    elif cmd == "restore":
        print(restore())
    else:
        print(__doc__)
