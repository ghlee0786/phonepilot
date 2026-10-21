"""ApprovalCenter and human hand-off requests (PRD 13.3, 10.6).

The GUI installs handlers; without a GUI (headless/tests) the default handlers deny.
Approvals are bound to the screen hash and the target element: if either changes
before execution, the approval is invalid (FR-HIL-001).
"""

from __future__ import annotations

import asyncio
import itertools
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from phonepilot.errors import PhonePilotError
from phonepilot.security.policy import Level, PolicyDecision

_ids = itertools.count(1)


@dataclass
class ApprovalRequest:
    title: str
    summary: str
    decision: PolicyDecision
    client: str = "agent"
    reason: str = ""
    details: dict[str, Any] = field(default_factory=dict)
    preview_png: bytes | None = None
    screen_hash: str = ""
    target_key: str = ""
    expected_amount: str | None = None  # CONFIRM_HIGH: user must retype the digits
    id: int = field(default_factory=lambda: next(_ids))
    created: float = field(default_factory=time.monotonic)

    @property
    def high(self) -> bool:
        return self.decision.level >= Level.CONFIRM_HIGH


@dataclass
class ApprovalResponse:
    approved: bool
    note: str = ""
    typed_confirmation: str = ""
    stop_all: bool = False


@dataclass
class HumanRequest:
    kind: str
    instructions: str
    timeout_s: float = 600
    id: int = field(default_factory=lambda: next(_ids))


@dataclass
class HumanResponse:
    outcome: str  # continued | aborted | timeout
    note: str = ""


ApprovalHandler = Callable[[ApprovalRequest], Awaitable[ApprovalResponse]]
HumanHandler = Callable[[HumanRequest], Awaitable[HumanResponse]]


async def deny_all(_req: ApprovalRequest) -> ApprovalResponse:
    return ApprovalResponse(False, note="no approver available (headless)")


async def abort_all(_req: HumanRequest) -> HumanResponse:
    return HumanResponse("aborted", "no human available (headless)")


def _digits(s: str) -> str:
    return "".join(ch for ch in s if ch.isdigit())


class ApprovalCenter:
    def __init__(self, timeout_s: float = 120, per_minute: int = 6) -> None:
        self.timeout_s = timeout_s
        self.per_minute = per_minute
        self.approval_handler: ApprovalHandler = deny_all
        self.human_handler: HumanHandler = abort_all
        self.history: deque[tuple[ApprovalRequest, ApprovalResponse]] = deque(maxlen=200)
        self.pending: dict[int, ApprovalRequest] = {}
        self._recent: deque[float] = deque()
        self.on_flood: Callable[[], None] | None = None
        self.on_stop_all: Callable[[], Awaitable[None]] | None = None
        self._waiting: set[asyncio.Task[ApprovalResponse]] = set()

    async def request(self, req: ApprovalRequest) -> ApprovalResponse:
        now = time.monotonic()
        while self._recent and now - self._recent[0] > 60:
            self._recent.popleft()
        self._recent.append(now)
        if len(self._recent) > self.per_minute:
            if self.on_flood:
                self.on_flood()
            raise PhonePilotError("RATE_LIMITED", "too many approval requests; agent paused (PRD 13.8)")
        self.pending[req.id] = req
        task = asyncio.ensure_future(self.approval_handler(req))
        self._waiting.add(task)
        try:
            resp = await asyncio.wait_for(task, timeout=self.timeout_s)
        except asyncio.CancelledError:
            resp = ApprovalResponse(False, note="cancelled by kill switch")
            self.history.append((req, resp))
            raise PhonePilotError("APPROVAL_DENIED", "approval cancelled by the kill switch") from None
        except TimeoutError:
            resp = ApprovalResponse(False, note="timeout")
            self.history.append((req, resp))
            raise PhonePilotError("APPROVAL_TIMEOUT", "no response from the user") from None
        finally:
            self.pending.pop(req.id, None)
            self._waiting.discard(task)
        if resp.approved and req.high and req.expected_amount is not None:
            if _digits(resp.typed_confirmation) != _digits(req.expected_amount):
                resp = ApprovalResponse(False, note="confirmation digits did not match")
        if resp.approved and req.high and req.expected_amount is None and resp.typed_confirmation.strip() not in {"승인", "approve"}:
            resp = ApprovalResponse(False, note="confirmation word missing")
        self.history.append((req, resp))
        if resp.stop_all and self.on_stop_all is not None:
            await self.on_stop_all()
        if not resp.approved:
            raise PhonePilotError("APPROVAL_DENIED", f"user denied: {req.title}", note=resp.note, stop_all=resp.stop_all)
        return resp

    async def human(self, req: HumanRequest) -> HumanResponse:
        try:
            return await asyncio.wait_for(self.human_handler(req), timeout=req.timeout_s)
        except TimeoutError:
            return HumanResponse("timeout")

    def deny_pending(self) -> None:
        """Kill switch: cancel every in-flight approval so its caller gets APPROVAL_DENIED right away."""
        for task in list(self._waiting):
            task.cancel()
        self.pending.clear()
