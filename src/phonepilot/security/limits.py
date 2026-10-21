"""Rate limits and daily counters (PRD 13.4, 13.8, FR-HIL-011).

Only aggregate numbers and dates are persisted, never action details.
"""

from __future__ import annotations

import json
import time
from collections import deque
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from phonepilot.errors import PhonePilotError

KST = ZoneInfo("Asia/Seoul")


class RateLimiter:
    """Sliding windows: per-second and per-minute caps."""

    def __init__(self, per_second: int, per_minute: int, clock: Callable[[], float] = time.monotonic) -> None:
        self.per_second = per_second
        self.per_minute = per_minute
        self._clock = clock
        self._events: deque[float] = deque()

    def check(self) -> None:
        now = self._clock()
        while self._events and now - self._events[0] >= 60:
            self._events.popleft()
        last_second = sum(1 for t in self._events if now - t < 1)
        if last_second >= self.per_second:
            raise PhonePilotError("RATE_LIMITED", "too many actions per second", retry_after_ms=1000)
        if len(self._events) >= self.per_minute:
            wait = int((60 - (now - self._events[0])) * 1000)
            raise PhonePilotError("RATE_LIMITED", "too many actions per minute", retry_after_ms=wait)
        self._events.append(now)


class DailyCounters:
    """Persisted per-day aggregates: engagement count, in-app purchase KRW, last account creation date."""

    def __init__(self, path: Path | None = None, now: Callable[[], datetime] | None = None) -> None:
        self.path = path
        self._now = now or (lambda: datetime.now(KST))
        self.state: dict[str, object] = {"date": self._today(), "engagement": 0, "purchase_krw": 0, "last_account_creation": None}
        if path and path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                self.state.update({k: loaded[k] for k in self.state if k in loaded})
            except (OSError, ValueError):
                pass
        self._roll()

    def _today(self) -> str:
        return self._now().astimezone(KST).date().isoformat()

    def _roll(self) -> None:
        today = self._today()
        if self.state.get("date") != today:
            self.state.update({"date": today, "engagement": 0, "purchase_krw": 0})
            self._save()

    def _save(self) -> None:
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.state), encoding="utf-8")

    def use_engagement(self, limit: int) -> None:
        self._roll()
        count = int(self.state["engagement"])  # type: ignore[arg-type]
        if count >= limit:
            raise PhonePilotError("LIMIT_EXCEEDED", f"daily engagement limit {limit} reached", limit=limit)
        self.state["engagement"] = count + 1
        self._save()

    def use_purchase(self, amount_krw: int, limit_krw: int) -> None:
        self._roll()
        used = int(self.state["purchase_krw"])  # type: ignore[arg-type]
        if limit_krw <= 0 or used + amount_krw > limit_krw:
            raise PhonePilotError("LIMIT_EXCEEDED", "in-app purchase limit", used=used, limit=limit_krw)
        self.state["purchase_krw"] = used + amount_krw
        self._save()

    def check_account_creation(self) -> None:
        last = self.state.get("last_account_creation")
        if last:
            then = datetime.fromisoformat(str(last))
            if (self._now() - then).total_seconds() < 24 * 3600:
                raise PhonePilotError("POLICY_BLOCKED", "only one account creation per 24 hours (PRD 3.3)")

    def record_account_creation(self) -> None:
        self.state["last_account_creation"] = self._now().isoformat()
        self._save()
