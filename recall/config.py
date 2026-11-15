"""Settings: watch.yaml for what to watch and what is on the shelf, .env for keys. Git ignores .env."""

from __future__ import annotations

import json
import os
from pathlib import Path

import yaml

from .engine import Product
from .importer import products_from_csv

ROOT = Path(__file__).resolve().parent.parent


def load_env(path: Path = ROOT / ".env") -> None:
    """Read KEY=value lines into the environment. A value already set wins."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def load_watch(path: Path | None = None) -> dict:
    cfg = yaml.safe_load((path or ROOT / "watch.yaml").read_text())
    ids = [p["id"] for p in cfg["shelf"]]
    if len(ids) != len(set(ids)):
        raise ValueError("two shelf products have the same id")
    return cfg


def load_shelf(cfg: dict) -> tuple[list[Product], str]:
    """The products to compare with the notices, and a name for the list.

    SHELF=valio reads the product file of the Valio Aimo challenge instead of the list in watch.yaml.
    That file is synthetic data of the challenge. Git ignores it.
    """
    if os.environ.get("STOCK_CSV"):
        path = Path(os.environ["STOCK_CSV"])
        return products_from_csv(path.read_text()), f"Stock import: {path.name}"
    if os.environ.get("SHELF") == "valio":
        folder = Path(os.environ.get("VALIO_DATA_DIR") or ROOT.parent / "valio-challenge" / "data" / "valio")
        raw = json.loads((folder / "valio_aimo_junction26_product_data.json").read_text())
        return [valio_product(r) for r in raw if not r.get("deleted")], "Valio Aimo challenge catalog (synthetic)"
    return [Product(**p) for p in cfg["shelf"]], cfg.get("kitchen", "Kitchen")


def valio_product(raw: dict) -> Product:
    s = raw.get("synkkaData") or {}
    names = {n.get("language"): n.get("value") for n in s.get("names") or []}
    return Product(id=str(raw["id"]), name=names.get("fi") or names.get("en") or next(iter(names.values()), ""),
                   ean=s.get("gtin") or raw.get("salesUnitGtin") or "", supplier=raw.get("vendorName") or "")
