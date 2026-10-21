"""No-trace session cleanup (PRD 12.6)."""

from __future__ import annotations

import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

DEVICE_TMP_DIR = "/sdcard/Download/phonepilot_tmp"


@dataclass
class CleanupReport:
    done: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        total = len(self.done) + len(self.failed)
        return f"정리 완료: {len(self.done)}/{total}" + (f" (실패: {', '.join(self.failed)})" if self.failed else "")


class SessionCleanup:
    """Ordered cleanup steps. Each step is independent: one failure doesn't stop the others."""

    def __init__(self) -> None:
        self.steps: list[tuple[str, Callable[[], Awaitable[None]]]] = []

    def add(self, name: str, fn: Callable[[], Awaitable[None]]) -> None:
        self.steps.append((name, fn))

    async def run(self) -> CleanupReport:
        report = CleanupReport()
        for name, fn in self.steps:
            try:
                await fn()
                report.done.append(name)
            except Exception:
                report.failed.append(name)
        return report


def sweep_temp_root(root: Path) -> int:
    """Remove leftover per-session temp folders from a crashed run (SEC-010)."""
    if not root.exists():
        return 0
    count = 0
    for child in root.iterdir():
        shutil.rmtree(child, ignore_errors=True)
        count += 1
    return count
