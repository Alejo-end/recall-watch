"""Optional source-grounding audit using Verda's Kimi inference endpoint.

This module is deliberately outside the hold decision path.  It can flag an extracted field as
unsupported by the source page for a human reviewer; it never raises, lowers or closes a food
recall action.  Field accuracy still needs an independent human audit sample.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict
from html import unescape

import httpx

from .engine import Notice

FIELDS = ("company", "brand", "products", "eans", "lot", "hazard", "allergens", "summary")
STATUSES = {"supported", "contradicted", "not_stated", "unclear"}


def source_excerpt(source_html: str, limit: int = 18_000) -> str:
    """Send the relevant readable source, not scripts/navigation and a huge token bill, to Kimi."""
    text = re.sub(r"<(script|style|noscript)[^>]*>.*?</\1>", " ", source_html, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", unescape(text)).strip()[:limit]


class VerdaExtractionAuditor:
    def __init__(self, api_key: str | None = None, *, transport: httpx.BaseTransport | None = None):
        key = api_key or os.environ.get("VERDA_INFERENCE_API_KEY")
        if not key:
            raise RuntimeError("Set VERDA_INFERENCE_API_KEY to run the optional Kimi extraction audit")
        base = os.environ.get("VERDA_INFERENCE_BASE_URL", "https://inference.verda.com").rstrip("/")
        self.model = os.environ.get("VERDA_MODEL", "kimi-k3-1-pro")
        self.http = httpx.Client(base_url=f"{base}/v1", timeout=60, transport=transport,
                                 headers={"Authorization": f"Bearer {key}"})

    def audit(self, notice: Notice, source_html: str) -> dict:
        """Return field-grounding labels, not a replacement extraction or safety decision."""
        extraction = {k: v for k, v in asdict(notice).items() if k not in {"detected_at", "published_at", "extract_ms", "monitor_detected_at"}}
        prompt = """You are auditing whether a structured food-recall extraction is grounded in its source page.
This is not a safety decision. Do not infer missing facts and do not decide whether a physical pack is affected.
For each of company, brand, products, eans, lot, hazard, allergens and summary, return one of:
supported, contradicted, not_stated, or unclear. Cite a short source fragment for every non-empty extracted field.
Return only JSON with keys field_status (object), concerns (array of strings), and overall (supported|review).

SOURCE HTML:
""" + source_excerpt(source_html) + "\n\nEXTRACTION:\n" + json.dumps(extraction, ensure_ascii=False)
        body = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
            "max_tokens": 2400,
            "reasoning_effort": "low",
        }
        response = self.http.post("/chat/completions", json=body)
        response.raise_for_status()
        content = ((response.json().get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        try:
            audit = json.loads(content)
        except json.JSONDecodeError as e:
            raise RuntimeError("Kimi did not return a JSON extraction audit") from e
        if not isinstance(audit, dict) or not isinstance(audit.get("field_status"), dict):
            raise RuntimeError("Kimi returned an invalid extraction audit")
        # Kimi may return either the requested status string or a richer object containing
        # status/evidence. Normalize both to a stable shape before any batch metric uses it.
        normalized = {}
        for field in FIELDS:
            value = audit["field_status"].get(field, "unclear")
            item = value if isinstance(value, dict) else {"status": value}
            status = item.get("status")
            if status not in STATUSES:
                raise RuntimeError(f"Kimi returned an invalid status for {field}")
            normalized[field] = {"status": status, "evidence": str(item.get("evidence") or "")[:500]}
        audit["field_status"] = normalized
        if audit.get("overall") not in {"supported", "review"}:
            audit["overall"] = "review"
        return {"model": self.model, "notice": notice.url, "audit": audit}
