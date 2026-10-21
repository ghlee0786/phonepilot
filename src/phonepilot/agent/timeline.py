"""In-memory, redacted session timeline (PRD 13.7). Never written to disk unless the user exports it."""

from __future__ import annotations

import json
import time
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from phonepilot.security.redactor import Redactor


@dataclass
class TimelineEvent:
    actor: str  # human | <mcp client> | workflow:<name> | builtin-agent | system
    tool: str
    target: str = ""
    level: str = ""
    result: str = "ok"
    reason: str = ""
    elapsed_ms: int = 0
    detail: dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)


class Timeline:
    def __init__(self, redactor: Redactor, maxlen: int = 5000) -> None:
        self.redactor = redactor
        self.events: deque[TimelineEvent] = deque(maxlen=maxlen)
        self.listeners: list[Callable[[TimelineEvent], None]] = []

    def add(self, event: TimelineEvent) -> TimelineEvent:
        event.target = self.redactor.redact(event.target)
        event.reason = self.redactor.redact(event.reason)
        event.result = self.redactor.redact(event.result)
        event.detail = self.redactor.redact_obj(event.detail)
        self.events.append(event)
        for cb in list(self.listeners):
            try:
                cb(event)
            except Exception:
                pass
        return event

    def recent(self, n: int = 100) -> list[dict[str, Any]]:
        return [asdict(e) for e in list(self.events)[-n:]]

    def export_jsonl(self, path: Path) -> int:
        with path.open("w", encoding="utf-8") as f:
            for e in self.events:
                f.write(json.dumps(asdict(e), ensure_ascii=False) + "\n")
        return len(self.events)

    def clear(self) -> None:
        self.events.clear()
