"""What a person does about one watched page, and what the screen asks them to do next.

The page state comes from Context.dev.  The review state comes only from people:

    open ──contact owner──▶ waiting on owner ──resolve (with an outcome)──▶ resolved
      │                         │  deadline passes ▶ overdue (needs action again)
      │                         │  page changes    ▶ "check the change" (needs action again)
      └──────────resolve────────┴──────────────────────────────────────────▶ resolved
    resolved ──page shows a different state later──▶ reopened (needs action again)

Every action is stored with a name and a time.  A resolution needs an outcome, and the outcomes
that rest on someone's word (the owner says the lot is outside the recall) need a note.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

HELSINKI = ZoneInfo("Europe/Helsinki")

# Action kinds.  A prepared draft is not a sent message: only "owner_contacted", which a person
# records after sending, starts the wait for the owner.  "escalated" and "closed" are the names of
# the first prototype, whose button recorded a contact without any message being sent; those rows
# are therefore read as an unconfirmed draft and a resolution.
ACTIONS = ("draft_prepared", "owner_contacted", "resolved", "reopened", "note", "identity_confirmed",
           "escalated", "closed", "assigned", "reviewed")
LEGACY = {"escalated": "draft_prepared", "closed": "resolved"}

OUTCOMES = {
    "page_updated": "The page now shows the recall or no longer sells the product",
    "lot_outside_recall": "The owner confirmed the listed stock is outside the recalled lots",
    "not_this_product": "The page is about a different product",
    "classification_wrong": "The automatic classification was wrong",
    "no_action_needed": "Checked: no action needed",
}
NOTE_REQUIRED = {"lot_outside_recall", "classification_wrong", "no_action_needed"}
RESOLVED_LABEL = {
    "page_updated": "page updated", "lot_outside_recall": "lot outside the recall", "not_this_product": "different product",
    "classification_wrong": "classification corrected", "no_action_needed": "no action needed", "": "resolved",
}


def validate(action: str, who: str, note: str, outcome: str, recipient: str, corrected_state: str, states,
             page_state: str | None = None) -> str:
    """Returns the stored action kind, or raises ValueError with a message for the person."""
    if action not in ACTIONS:
        raise ValueError(f"Unknown action. Use one of: {', '.join(ACTIONS[:4])}")
    if not who.strip():
        raise ValueError("A reviewer name is required")
    kind = LEGACY.get(action, action)
    if kind in {"owner_contacted", "draft_prepared"} and action == kind and not recipient.strip():
        raise ValueError("Name who you contact, for example an e-mail address or a team")
    if kind == "identity_confirmed" and not note.strip():
        raise ValueError("Say how you confirmed it, for example: same pack photo and name as the notice")
    if kind == "resolved" and action == "resolved":
        if outcome not in OUTCOMES:
            raise ValueError("Choose an outcome: " + ", ".join(OUTCOMES))
        if outcome in NOTE_REQUIRED and not note.strip():
            raise ValueError("This outcome needs a note that says what you checked or who confirmed it")
        if outcome == "page_updated" and page_state in {"active_listing", "unverified"}:
            raise ValueError("The last read does not show an updated page. Use “Check page now” first, "
                             "or choose another outcome.")
        if outcome == "classification_wrong" and corrected_state not in states:
            raise ValueError("Choose what the page really shows")
    if kind == "note" and not note.strip():
        raise ValueError("The note is empty")
    return kind


def fmt(ts: float) -> str:
    return datetime.fromtimestamp(ts, HELSINKI).strftime("%-d %b %H:%M")


def review_state(obs: list[dict], acts: list[dict], state_since: float, now: float, publisher: str,
                 page_state: str, purchase_closed: set[str], when=fmt) -> dict:
    """The review state of one page and the next step, in words a person can act on."""
    kinds = [dict(a, kind=LEGACY.get(a["action"], a["action"])) for a in acts]
    last_resolve = next((a for a in reversed(kinds) if a["kind"] == "resolved"), None)
    last_contact = next((a for a in reversed(kinds) if a["kind"] == "owner_contacted"), None)
    last_reopen = next((a for a in reversed(kinds) if a["kind"] == "reopened"), None)
    last_draft = next((a for a in reversed(kinds) if a["kind"] == "draft_prepared"), None)
    state_at = lambda t: next((o["state"] for o in reversed(obs) if o["at"] <= t), None)

    status, needs, step, reason = "open", False, "", ""
    resolved_live = last_resolve and not (last_reopen and last_reopen["at"] > last_resolve["at"])
    contact_live = last_contact and not (last_resolve and last_resolve["at"] > last_contact["at"]) \
        and not (last_reopen and last_reopen["at"] > last_contact["at"])

    later = [a["at"] for a in (last_contact, last_resolve, last_reopen) if a]
    draft_live = last_draft and all(t < last_draft["at"] for t in later)
    if draft_live:
        status, needs = "draft_prepared", True
        step = (f"A message to {last_draft.get('recipient') or publisher} was prepared on {when(last_draft['at'])}"
                + (" with the first prototype's button" if last_draft["action"] == "escalated" else "")
                + ". Nothing confirms it was sent. Confirm once you have sent it.")
    elif resolved_live and (state_since > last_resolve["at"] and state_at(last_resolve["at"]) != page_state):
        status, needs = "reopened", True
        reason = f"The page changed to “{page_state.replace('_', ' ')}” after it was resolved on {when(last_resolve['at'])}."
    elif resolved_live:
        status = "resolved"
        step = f"Resolved: {RESOLVED_LABEL.get(last_resolve.get('outcome') or '', 'resolved')}. It re-opens if the page changes."
    elif contact_live:
        due = last_contact.get("due_at")
        changed = state_since > last_contact["at"] and state_at(last_contact["at"]) != page_state
        status = "waiting_on_owner"
        if changed:
            needs = True
            step = f"The page changed after you contacted {last_contact.get('recipient') or publisher}. Check it and resolve."
        elif due and now > due:
            needs = True
            step = f"No resolution by the deadline ({when(due)}). Contact {publisher} again or resolve."
        else:
            step = (f"Waiting for {last_contact.get('recipient') or publisher}" + (f" until {when(due)}" if due else "") +
                    ". The page stays under watch.")
    if status in {"open", "reopened"}:
        if page_state == "active_listing":
            needs = True
            step = f"Contact {publisher}: ask which lot the page sells, or ask them to show the recall."
        elif page_state == "unverified":
            needs = True
            step = "Open the page and record what it really shows."
        elif page_state in purchase_closed:
            step = "The purchase path is closed. Check the page and resolve to finish."
        if reason:
            step = reason + " " + step
    return {"review": status, "needs_action": needs, "next_step": step,
            "contact": {k: last_contact.get(k) for k in ("recipient", "at", "due_at", "who")} if contact_live else None,
            "draft": {k: last_draft.get(k) for k in ("recipient", "at", "who", "note")} if draft_live else None,
            "resolution": {k: last_resolve.get(k) for k in ("outcome", "at", "who", "note", "corrected_state")}
            if resolved_live else None}


def dur(s: float) -> str:
    s = max(0, s)
    d, h, m = int(s // 86400), int(s % 86400 // 3600), round(s % 3600 / 60)
    return f"{d} d {h} h" if d else f"{h} h {m:02d} min" if h else f"{m} min"


def owner_message(case: dict, row: dict, reply_hours: float, when=None) -> dict:
    """A plain message to the page owner, with the evidence, written for a person to send."""
    product = " / ".join(case["products"][:3]) or case["title_fi"]
    when = (when or fmt)(row["observed_at"]) if row.get("observed_at") else "our last check"
    tz = "" if row.get("url") == "" else " (Helsinki time)"
    lots = f"\nThe recall covers: {case['lot']}" if case.get("lot") else ""
    gtin = f" (GTIN {', '.join(case['gtins'])})" if case.get("gtins") else ""
    ask = ("Please confirm which lot (best-before date) this page sells. If it can be a recalled lot, "
           "please remove the product from sale or show the recall on the page."
           if row.get("state") == "active_listing" else
           "Please check whether the page should show the recall to customers.")
    subject = f"Recall check: {product} on {row['publisher']}"
    body = (f"Hello,\n\nThe Finnish Food Authority published a recall on {case['notice_date']}:\n"
            f"“{case['title_fi']}”" + (f" ({case['title_en']})" if case.get("title_en") else "") + f"\n{case['notice_url']}\n"
            f"Product: {product}{gtin}{lots}\n\n"
            f"Your page {row.get('url') or '(simulated page)'}\n"
            f"showed this when we checked it on {when}{tz}:\n“{row.get('evidence', '')}”\n\n"
            f"{ask}\nWe would be grateful for a reply within {int(reply_hours)} hours.\n\n"
            "This message reports what the public page showed. It does not claim that a recalled pack was sold.\n")
    return {"subject": subject, "body": body, "reply_hours": reply_hours}
