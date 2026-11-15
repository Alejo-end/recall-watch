"""The demo replay of a recall case.

Simulated pages on a simulated clock, built into the same case view as the live store.  It has
its own endpoint and its own screen mode, so a simulated row is never shown or charted next to a
live observation.  The replay loops, so a judge who opens it late still sees the change happen.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from .cases import STATES, build_case_view
from .workflow import validate

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "afterimage-demo.json"


class AfterimageReplay:
    def __init__(self, started_at: float | None = None, path: Path = FIXTURE):
        self.data = json.loads(path.read_text())
        self.started_at = started_at if started_at is not None else time.time()
        self.speed = float(self.data["minutes_per_second"]) * 60          # simulated seconds per real second
        self.cycle_s = self.data["duration_minutes"] * 60 / self.speed + self.data["hold_seconds"]
        self.reviews: dict[int, dict[str, list[dict]]] = {}               # loop number to target to actions

    def _clock(self, now: float) -> tuple[int, float, float]:
        """The loop number, the simulated notice time and the simulated now."""
        elapsed = max(0.0, now - self.started_at)
        loop = int(elapsed // self.cycle_s)
        loop_start = self.started_at + loop * self.cycle_s
        sim = min(elapsed - loop * self.cycle_s, self.cycle_s - self.data["hold_seconds"]) * self.speed
        return loop, loop_start, loop_start + sim

    def state(self, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        loop, notice_at, sim_now = self._clock(now)
        case = {**self.data["official_notice"], "id": "demo", "notice_at": notice_at, "opened_at": notice_at,
                "opened_by": "Demo replay"}
        targets, obs = [], {}
        for raw in self.data["targets"]:
            targets.append({"id": raw["id"], "case_id": "demo", "url": raw["url"], "publisher": raw["publisher"],
                            "page_type": raw["page_type"], "added_at": notice_at + raw["observations"][0]["min"] * 60,
                            "added_by": "Demo replay", "monitor_id": raw["monitor_id"]})
            obs[raw["id"]] = [
                {"at": notice_at + o["min"] * 60, "state": o["state"], "trigger": o["trigger"], "evidence": o["evidence"],
                 "page_title": "", "window_start": notice_at + o["window_min"] * 60 if "window_min" in o else None}
                for o in raw["observations"] if notice_at + o["min"] * 60 <= sim_now]
        targets = [t for t in targets if obs[t["id"]]]
        acts = self.reviews.get(loop, {})
        view = build_case_view(case, targets, obs, acts, sim_now, simulated=True)
        total = self.data["duration_minutes"] * 60
        return {**view, "mode": "demo_replay", "about": self.data["about"], "simulated": True,
                "sim_elapsed_s": sim_now - notice_at, "sim_total_s": total,
                "restarts_in_s": max(0, round(self.cycle_s - (now - self.started_at - loop * self.cycle_s)))}

    def target(self, tid: str) -> dict | None:
        return next((t for t in self.state()["targets"] if t["id"] == tid), None)

    def act(self, tid: str, who: str, action: str, note: str = "", now: float | None = None, outcome: str = "",
            recipient: str = "", due_hours: float | None = None, corrected_state: str = "") -> dict:
        if tid not in {t["id"] for t in self.data["targets"]}:
            raise KeyError(tid)
        loop, _, sim_now = self._clock(time.time() if now is None else now)
        current = next((r for r in self.state(time.time() if now is None else now)["targets"] if r["id"] == tid), None)
        kind = validate(action, who, note, outcome, recipient, corrected_state, STATES, current["state"] if current else None)
        entry = {"at": sim_now, "who": who.strip()[:120], "action": kind, "note": note.strip()[:1000], "outcome": outcome,
                 "recipient": recipient.strip()[:200], "due_at": sim_now + due_hours * 3600 if due_hours else None,
                 "corrected_state": corrected_state if outcome == "classification_wrong" else ""}
        self.reviews = {loop: self.reviews.get(loop, {})}      # a new loop starts with no reviews
        self.reviews[loop].setdefault(tid, []).append(entry)
        return entry
