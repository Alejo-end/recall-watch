"""Is this public page about the recalled product?  A deterministic answer, with its basis.

The page state comes from a language model; the identity must not.  It is computed here from what
Context.dev returned (the structured product block, the page text and the address) and the
official notice, in this order of strength:

    gtin           the official GTIN is the page's product code, is in its address, or follows "EAN"/"GTIN"
    exact_name     the page's product name has the same words as a recalled product name (pack sizes aside)
    partial_name   at least two words in common, including the brand: a person must confirm
    none           nothing supports the identity: the observation counts as "could not classify"
"""

from __future__ import annotations

import re

from .engine import name_tokens, norm_ean

BASIS_LABEL = {
    "gtin": "Official GTIN found on the page",
    "exact_name": "Product name matches the notice",
    "partial_name": "Product name matches only partly",
    "none": "No supported product match",
    "confirmed": "Confirmed by a reviewer",
}


def _codes(text: str) -> set[str]:
    return {norm_ean(c) for c in re.findall(r"(?<!\d)\d{8,14}(?!\d)", text or "")}


def identity_basis(products: list[str], gtins: list[str], url: str, product: dict, markdown: str,
                   page_title: str) -> tuple[str, str]:
    official = {norm_ean(g) for g in gtins if g}
    if official:
        if norm_ean(product.get("sku") or "") in official:
            return "gtin", f"Product code {product['sku']} on the page"
        if official & _codes(url):
            return "gtin", "The official GTIN is in the page address"
        labelled = re.findall(r"(?:EAN|GTIN)[^\d]{0,20}(\d{8,14})", markdown or "", flags=re.I)
        if official & {norm_ean(c) for c in labelled}:
            return "gtin", "The official GTIN is printed on the page as its EAN"
    page_name = product.get("name") or re.split(r"\s[|–-]\s", page_title or "")[0]
    page_words = name_tokens(page_name.replace("®", ""))
    best, best_name = 0, ""
    for p in products:
        words = name_tokens(p.replace("®", ""))
        if words and words == page_words:
            return "exact_name", f"“{page_name}” = “{p}”"
        shared = len(words & page_words)
        if shared > best:
            best, best_name = shared, p
    if best >= 2:
        return "partial_name", f"“{page_name}” shares {best} words with “{best_name}”"
    return "none", f"“{page_name or 'no product name'}” does not match the notice"
