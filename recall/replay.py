"""A replay of the real recall notices of 2026, for a screen with no API key.

The notices are in fixtures/notices-2026.json. Their titles, dates and addresses are from the
Finnish Food Authority's list. The replay runs through the year fast: one day takes `day_s`
seconds. Then it starts again. The page shows "Replay" while this source runs.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .engine import DAY, Notice

SNAPSHOT = Path(__file__).resolve().parent.parent / "fixtures" / "notices-2026.json"


def ts(date: str, hour: int = 9) -> float:
    return datetime.fromisoformat(f"{date}T{hour:02d}:00:00+02:00").astimezone(timezone.utc).timestamp()


def load_notices(path: Path = SNAPSHOT) -> tuple[list[Notice], dict]:
    data = json.loads(path.read_text())
    notices = []
    for n in data["notices"]:
        t = ts(n["date"])
        notices.append(Notice(**n, published_at=t, detected_at=t, source="ruokavirasto.fi", extracted_by="by hand"))
    return notices, {k: data[k] for k in ("source", "read_on", "note")}


class Replay:
    def __init__(self, real_start: float, day_s: float = 2.0, first_day: str = "2026-01-01"):
        self.notices, self.about = load_notices()
        self.real_start, self.day_s = real_start, day_s
        self.t0 = ts(first_day, 0)
        self.t1 = ts(self.about["read_on"], 23)
        self.cycle = -1

    def now(self, real_now: float) -> tuple[float, int]:
        """The replay's own time, and the number of the run through the year."""
        span_s = (self.t1 - self.t0) / DAY * self.day_s
        elapsed = real_now - self.real_start
        return self.t0 + (elapsed % span_s) / self.day_s * DAY, int(elapsed // span_s)
