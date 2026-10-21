"""InputArbiter: the single path every input takes to the device (PRD 5.1, 7.6, 13.5).

- Human input always wins: it cancels the running AI gesture and pauses AI.
- The kill switch closes open pointers, cancels AI work and blocks AI actions until re-enabled.
- No code path may leave a pointer down (NFR-REL-004).
All coordinates handed to the arbiter are display pixels; conversion to the video frame
space happens here, right before serialization.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any
from enum import Enum

from phonepilot.core.device import DeviceBackend
from phonepilot.core.gesture import TouchAction, TouchStep
from phonepilot.core.scrcpy import protocol as p
from phonepilot.errors import PhonePilotError

HUMAN_GRACE_S = 1.5
LEASE_TTL_S = 60.0
AI_POINTER_BASE = 0  # AI fingers use ids 0..9 (PRD appendix A.3)

_ACTIONS = {
    TouchAction.DOWN: p.MotionAction.DOWN,
    TouchAction.MOVE: p.MotionAction.MOVE,
    TouchAction.UP: p.MotionAction.UP,
    TouchAction.CANCEL: p.MotionAction.CANCEL,
}


class ControlState(str, Enum):
    MANUAL = "manual"
    AI_RUNNING = "ai_running"
    AI_PAUSED = "ai_paused"
    KILLED = "killed"


class InputArbiter:
    def __init__(self, device: DeviceBackend, clock: Callable[[], float] = time.monotonic) -> None:
        self.device = device
        self._clock = clock
        self._lock = asyncio.Lock()
        self._cancel = asyncio.Event()
        self.paused = False
        self.killed = False
        self.last_human_ts = -1e9
        self._open: dict[int, tuple[int, int]] = {}  # wire pointer id -> frame position
        self._ai_busy = False
        self._lease_owner: str | None = None
        self._lease_ts = 0.0
        self.listeners: list[Callable[[str, dict], None]] = []
        self.recorder: Any = None  # CalibrationRecorder, set by the runtime (FR-INP-035)

    # ----------------------------------------------------------------- status
    @property
    def state(self) -> ControlState:
        if self.killed:
            return ControlState.KILLED
        if self.paused:
            return ControlState.AI_PAUSED
        if self._ai_busy:
            return ControlState.AI_RUNNING
        return ControlState.MANUAL

    @property
    def open_pointers(self) -> dict[int, tuple[int, int]]:
        return dict(self._open)

    def _emit(self, event: str, data: dict | None = None) -> None:
        for cb in list(self.listeners):
            try:
                cb(event, data or {})
            except Exception:
                pass

    # ------------------------------------------------------------------ lease
    def acquire_lease(self, owner: str) -> None:
        now = self._clock()
        if self._lease_owner and self._lease_owner != owner and now - self._lease_ts < LEASE_TTL_S:
            raise PhonePilotError("CONTROL_BUSY", f"control lease held by {self._lease_owner}")
        self._lease_owner = owner
        self._lease_ts = now

    def release_lease(self, owner: str | None = None) -> None:
        if owner is None or owner == self._lease_owner:
            self._lease_owner = None

    @property
    def lease_owner(self) -> str | None:
        if self._lease_owner and self._clock() - self._lease_ts >= LEASE_TTL_S:
            return None
        return self._lease_owner

    # ------------------------------------------------------------ ai gating
    def check_ai_allowed(self) -> None:
        if self.killed:
            raise PhonePilotError("KILL_SWITCH_ACTIVE", "kill switch is active")
        if self.paused:
            raise PhonePilotError("HUMAN_ACTIVE", "AI is paused")
        if self._clock() - self.last_human_ts < HUMAN_GRACE_S:
            raise PhonePilotError("HUMAN_ACTIVE", "a human is using the device")

    def pause_ai(self, reason: str = "manual") -> None:
        self.paused = True
        self._cancel.set()
        self._emit("paused", {"reason": reason})

    def resume_ai(self) -> None:
        if self.killed:
            return
        self.paused = False
        self._emit("resumed")

    async def kill(self) -> None:
        """Kill switch (FR-INP-045): close pointers, cancel AI, block until allow_ai()."""
        self.killed = True
        self._cancel.set()
        await self._close_open_pointers()
        self._emit("killed")

    def allow_ai(self) -> None:
        self.killed = False
        self.paused = False
        self._emit("allowed")

    # ------------------------------------------------------------- low level
    async def _send(self, msg: bytes) -> None:
        await self.device.send_control(msg)

    async def _touch(self, action: p.MotionAction, pointer_id: int, x: int, y: int, pressure: float = 1.0) -> None:
        fx, fy = self.device.display_to_frame(x, y)
        fw, fh = self.device.frame_size
        await self._send(p.inject_touch(action, pointer_id, fx, fy, fw, fh, pressure if action != p.MotionAction.UP else 0.0))
        if action == p.MotionAction.DOWN:
            self._open[pointer_id] = (x, y)
        elif action in (p.MotionAction.UP, p.MotionAction.CANCEL):
            self._open.pop(pointer_id, None)
        else:
            if pointer_id in self._open:
                self._open[pointer_id] = (x, y)

    async def _close_open_pointers(self) -> None:
        for pid, (x, y) in list(self._open.items()):
            try:
                await self._touch(p.MotionAction.UP, pid, x, y)
            except Exception:
                self._open.pop(pid, None)

    # --------------------------------------------------------------- human
    async def human_touch(self, action: p.MotionAction, x: int, y: int, pointer_id: int = p.POINTER_ID_GENERIC_FINGER) -> None:
        self._note_human()
        if self.recorder is not None and pointer_id == p.POINTER_ID_GENERIC_FINGER:
            name = {p.MotionAction.DOWN: "down", p.MotionAction.MOVE: "move", p.MotionAction.UP: "up"}.get(action, "cancel")
            self.recorder.feed(name, x, y, self._clock() * 1000)
        await self._touch(action, pointer_id, x, y)

    async def human_key(self, action: p.KeyAction, keycode: int, meta: int = 0, repeat: int = 0) -> None:
        self._note_human()
        await self._send(p.inject_keycode(action, keycode, repeat, meta))

    async def human_raw(self, msg: bytes) -> None:
        self._note_human()
        await self._send(msg)

    async def human_scroll(self, x: int, y: int, h: float, v: float) -> None:
        self._note_human()
        fx, fy = self.device.display_to_frame(x, y)
        fw, fh = self.device.frame_size
        await self._send(p.inject_scroll(fx, fy, fw, fh, h, v))

    def _note_human(self) -> None:
        self.last_human_ts = self._clock()
        if self._ai_busy:
            self._cancel.set()
        if self._ai_busy or self._lease_owner:
            if not self.paused:
                self.pause_ai("human_input")

    # ------------------------------------------------------------------ ai
    async def ai_gesture(self, steps: list[TouchStep]) -> None:
        """Play a synthesized gesture with real timing. Always leaves no pointer down."""
        self.check_ai_allowed()
        async with self._lock:
            self.check_ai_allowed()
            self._cancel.clear()
            self._ai_busy = True
            self._emit("busy")
            used: set[int] = set()
            start = time.monotonic()
            try:
                for step in steps:
                    delay = start + step.t_ms / 1000 - time.monotonic()
                    if delay > 0:
                        try:
                            await asyncio.wait_for(self._cancel.wait(), timeout=delay)
                        except TimeoutError:
                            pass
                    if self._cancel.is_set():
                        raise PhonePilotError(
                            "KILL_SWITCH_ACTIVE" if self.killed else "HUMAN_ACTIVE", "gesture interrupted"
                        )
                    pid = AI_POINTER_BASE + step.pointer
                    used.add(pid)
                    await self._touch(_ACTIONS[step.action], pid, step.x, step.y)
            finally:
                for pid in used:
                    if pid in self._open:
                        x, y = self._open[pid]
                        try:
                            await self._touch(p.MotionAction.UP, pid, x, y)
                        except Exception:
                            self._open.pop(pid, None)
                self._ai_busy = False
                self._emit("idle")

    async def ai_key(self, keycode: int, meta: int = 0, long_press: bool = False) -> None:
        self.check_ai_allowed()
        async with self._lock:
            self.check_ai_allowed()
            await self._send(p.inject_keycode(p.KeyAction.DOWN, keycode, 0, meta))
            if long_press:
                await asyncio.sleep(0.6)
            await self._send(p.inject_keycode(p.KeyAction.UP, keycode, 0, meta))

    async def ai_raw(self, msg: bytes) -> None:
        """Non-touch control message from the AI (panels, clipboard, text)."""
        self.check_ai_allowed()
        async with self._lock:
            self.check_ai_allowed()
            await self._send(msg)

    async def ai_text(self, text: str) -> None:
        """ASCII text through INJECT_TEXT in <=300 byte chunks."""
        self.check_ai_allowed()
        async with self._lock:
            raw = text.encode("utf-8")
            for i in range(0, len(raw), p.INJECT_TEXT_MAX_BYTES):
                self.check_ai_allowed()
                await self._send(p.inject_text_bytes(raw[i : i + p.INJECT_TEXT_MAX_BYTES]))

    async def ai_secret_text(self, buf: bytearray) -> None:
        """Inject a revealed secret buffer. The serialized message buffer is zeroed after sending."""
        self.check_ai_allowed()
        async with self._lock:
            self.check_ai_allowed()
            for i in range(0, len(buf), p.INJECT_TEXT_MAX_BYTES):
                chunk = buf[i : i + p.INJECT_TEXT_MAX_BYTES]
                msg = bytearray(p.inject_text_bytes(chunk))
                try:
                    await self._send(bytes(msg))
                finally:
                    for j in range(len(msg)):
                        msg[j] = 0
                    for j in range(len(chunk)):
                        chunk[j] = 0
