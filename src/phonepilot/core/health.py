"""Connection health: detect trouble early, recover in steps, keep a record (PRD 25 reliability, FR-DEV-003).

Detection (every `interval` seconds, cheap):
  - transport says it is disconnected, the decoder thread died, or the control channel closed
  - the decoder has been waiting for a key frame (it dropped packets and the screen is frozen until one comes)
  - input was sent but no frame arrived afterwards (stalled video)
Deep checks (every `deep_every` seconds, adb round trips): scrcpy server process and our port forward.
Heartbeat: on an idle, static screen a key frame is requested every `heartbeat_every` seconds, which proves the
whole path phone -> encoder -> socket -> decoder still works before the person needs it.

Recovery ladder (each step only if the previous one did not help):
  1. request a key frame (RESET_VIDEO) - a live stream always answers with a frame
  2. reconnect the session (close the old server and forward, start a new one)
  3. restart the adb server, then reconnect
  4. give up: state FAILED, the person is told, the incident stays open for the problem report

Incidents hold only fixed codes and timings - no screen content, serials or text - and are kept in memory and in
%LOCALAPPDATA%\\PhonePilot\\incidents.jsonl (last `MAX_LINES` lines).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

MAX_LINES = 200

# what each problem code means for a person (shown in the device tab and the problem report)
PROBLEM_TEXT = {
    "DISCONNECTED": "영상 연결이 끊김",
    "DECODER_DEAD": "영상 디코더가 멈춤",
    "CONTROL_DEAD": "조작 채널이 끊김(입력이 전달되지 않음)",
    "WAITING_KEYFRAME": "영상이 키 프레임을 기다리며 멈춤",
    "VIDEO_STALLED": "입력 뒤에 화면이 갱신되지 않음",
    "NO_FRAME_AFTER_RESET": "키 프레임을 요청해도 화면이 오지 않음",
    "SERVER_PROCESS_GONE": "폰의 화면 서버가 종료됨",
    "FORWARD_MISSING": "PC-폰 포트 연결이 사라짐",
    "SERVER_NOT_STARTED": "화면 서버가 시작되지 않음",
    "ADB_UNREACHABLE": "adb에 응답이 없음",
}
ACTION_TEXT = {
    "keyframe": "키 프레임 요청",
    "reconnect": "세션 재연결",
    "adb_restart": "adb 재시작 후 재연결",
    "gave_up": "자동 복구 실패",
}


def problem_text(code: str) -> str:
    if code.startswith("ADB_STATE_"):
        return f"adb 기기 상태 {code[10:].lower()}"
    return PROBLEM_TEXT.get(code, code)


@dataclass
class Incident:
    started: float  # wall clock, for people
    problem: str  # code from PROBLEM_TEXT
    actions: list[str] = field(default_factory=list)
    recovered: bool | None = None  # None while still being handled
    duration_ms: int = 0
    _t0: float = field(default=0.0, repr=False)  # monotonic

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("_t0", None)
        return d

    def summary(self) -> str:
        t = time.strftime("%H:%M:%S", time.localtime(self.started))
        steps = " → ".join(ACTION_TEXT.get(a, a) for a in self.actions) or "조치 없음"
        state = {True: "복구됨", False: "복구 실패", None: "처리 중"}[self.recovered]
        return f"{t} {problem_text(self.problem)} · {steps} · {state}" + (f" ({self.duration_ms / 1000:.1f}초)" if self.recovered is not None else "")


class IncidentLog:
    """Recent incidents in memory, appended to a small JSON-lines file for after-the-fact analysis."""

    def __init__(self, path: Path | None, keep: int = 50) -> None:
        self.path = path
        self.items: deque[Incident] = deque(maxlen=keep)
        self._writes = 0

    def open(self, problem: str) -> Incident:
        inc = Incident(time.time(), problem, _t0=time.monotonic())
        self.items.append(inc)
        return inc

    def close(self, inc: Incident, recovered: bool) -> None:
        inc.recovered = recovered
        inc.duration_ms = int((time.monotonic() - inc._t0) * 1000)
        self._persist(inc)

    def recent(self, n: int = 5) -> list[Incident]:
        return list(self.items)[-n:][::-1]

    def _persist(self, inc: Incident) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:  # append; trimming is occasional
                f.write(json.dumps(inc.to_dict(), ensure_ascii=False) + "\n")
            self._writes += 1
            if self._writes % 50 == 0:
                lines = self.path.read_text(encoding="utf-8").splitlines()
                if len(lines) > MAX_LINES:
                    self.path.write_text("\n".join(lines[-MAX_LINES:]) + "\n", encoding="utf-8")
        except OSError as e:
            log.warning("could not write incident log: %s", e)


@dataclass
class HealthConfig:
    interval: float = 2.0
    deep_every: float = 15.0
    heartbeat_every: float = 30.0
    stall_after: float = 2.0  # input with no frame for this long -> ask for a key frame
    keyframe_timeout: float = 3.0  # a live stream answers a reset within this
    waiting_key_after: float = 0.8
    max_reconnects: int = 2  # per incident, before restarting adb


class HealthMonitor:
    """Watches one runtime. `reconnect()` / `restart_adb()` are the runtime's; they return True on success."""

    def __init__(
        self,
        get_device: Callable[[], Any],
        get_last_input: Callable[[], float],
        reconnect: Callable[[str], Awaitable[bool]],
        restart_adb: Callable[[], Awaitable[bool]],
        incidents: IncidentLog,
        cfg: HealthConfig | None = None,
        clock: Callable[[], float] = time.monotonic,
        on_change: Callable[[], None] | None = None,
    ) -> None:
        self.get_device = get_device
        self.get_last_input = get_last_input
        self.reconnect = reconnect
        self.restart_adb = restart_adb
        self.incidents = incidents
        self.cfg = cfg or HealthConfig()
        self.clock = clock
        self.on_change = on_change
        self.enabled = True
        self.recovering = False
        self.last_check = 0.0
        self.last_problems: list[str] = []
        self._last_deep = clock()
        self._last_heartbeat = clock()
        self._keyframe_at: float | None = None  # pending key-frame request, waiting for its frame
        self._keyframe_reason = ""
        self._keyframe_frame_seq = 0
        self._keyframe_incident: Incident | None = None
        self._input_answered = 0.0  # inputs up to this time already got a frame after a key-frame request
        self._task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------- running
    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.ensure_future(self._run())

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
        self._task = None

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self.cfg.interval)
            try:
                await self.check()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the watchdog itself must never die
                log.exception("health check failed")

    # -------------------------------------------------------------- checks
    def _frame(self, dev: Any) -> tuple[int, float]:
        f = dev.framebus.latest()
        return (f.seq, f.ts) if f is not None else (0, 0.0)

    async def check(self, deep: bool | None = None) -> list[str]:
        """One round of detection and, if needed, recovery. Returns the problem codes it saw."""
        dev = self.get_device()
        if dev is None or not self.enabled or self.recovering:
            return []
        now = self.clock()
        self.last_check = now
        h = dev.health()
        seq, frame_ts = self._frame(dev)
        problems: list[str] = []

        if not h.connected:
            problems.append("DISCONNECTED")
        if not h.decoder_alive:
            problems.append("DECODER_DEAD")
        if not h.control_alive:
            problems.append("CONTROL_DEAD")
        if problems:  # the transport itself is broken: no point asking it for a key frame
            await self._recover(problems[0], dev, start_at="reconnect")
            return self._seen(problems)

        # a key frame we asked for: a live stream answers within keyframe_timeout
        if self._keyframe_at is not None:
            if seq != self._keyframe_frame_seq:
                self._keyframe_at = None
                self._input_answered = self.get_last_input()  # that input has had its frame
                if self._keyframe_incident is not None:  # step 1 was enough
                    self.incidents.close(self._keyframe_incident, True)
                    self._keyframe_incident = None
                    self._changed()
            elif now - self._keyframe_at > self.cfg.keyframe_timeout:
                self._keyframe_at = None
                problems.append("NO_FRAME_AFTER_RESET")
                await self._recover("NO_FRAME_AFTER_RESET", dev, start_at="reconnect", parent=self._keyframe_reason)
                return self._seen(problems)

        if h.waiting_key_since is not None and now - h.waiting_key_since > self.cfg.waiting_key_after:
            problems.append("WAITING_KEYFRAME")
        last_input = self.get_last_input()
        # >=: Windows' monotonic clock ticks every ~16 ms, so an input and the frame before it can share a timestamp;
        # a needless key-frame request costs nothing, a missed stall leaves the screen frozen
        if last_input >= frame_ts and last_input > self._input_answered and now - last_input > self.cfg.stall_after:
            problems.append("VIDEO_STALLED")
        if problems and self._keyframe_at is None:
            await self._ask_keyframe(dev, problems[0], seq, now)
            return self._seen(problems)

        if deep or (deep is None and now - self._last_deep >= self.cfg.deep_every):
            self._last_deep = now
            found = await dev.probe()
            if found:
                await self._recover(found[0], dev, start_at="reconnect")
                return self._seen(found)

        if self._keyframe_at is None and now - frame_ts > self.cfg.heartbeat_every and now - self._last_heartbeat > self.cfg.heartbeat_every:
            self._last_heartbeat = now
            await self._ask_keyframe(dev, "HEARTBEAT", seq, now)
        return self._seen([])

    def _seen(self, problems: list[str]) -> list[str]:
        if problems != self.last_problems:
            self.last_problems = problems
            self._changed()
        return problems

    def _changed(self) -> None:
        if self.on_change is not None:
            try:
                self.on_change()
            except Exception:  # noqa: BLE001
                pass

    async def _ask_keyframe(self, dev: Any, reason: str, seq: int, now: float) -> None:
        try:
            await dev.request_keyframe()
        except Exception as e:  # noqa: BLE001 - control channel failure: straight to reconnect
            log.warning("key frame request failed: %s", e)
            await self._recover("CONTROL_DEAD", dev, start_at="reconnect")
            return
        self._keyframe_at = now
        self._keyframe_reason = reason
        self._keyframe_frame_seq = seq
        if reason == "WAITING_KEYFRAME":
            # a real freeze (packets were dropped). A quiet screen after a tap on nothing is not, so VIDEO_STALLED
            # and HEARTBEAT only become incidents if the request goes unanswered.
            self._keyframe_incident = self.incidents.open(reason)
            self._keyframe_incident.actions.append("keyframe")
            self._changed()
        if reason != "HEARTBEAT":
            log.info("health: %s -> key frame requested", reason)

    # ------------------------------------------------------------ recovery
    async def _recover(self, problem: str, dev: Any, start_at: str, parent: str = "") -> bool:
        """Steps 2-4 of the ladder. `parent` is what a failed key-frame request was trying to fix."""
        self.recovering = True
        if self._keyframe_incident is not None:  # the key frame did not help; continue that incident
            inc, self._keyframe_incident = self._keyframe_incident, None
        else:
            inc = self.incidents.open(problem)
            if parent and parent != "HEARTBEAT":
                inc.actions.append("keyframe")
        self._changed()
        log.warning("health: %s -> recovering", problem)
        ok = False
        try:
            for _ in range(self.cfg.max_reconnects):
                inc.actions.append("reconnect")
                if await self.reconnect(problem):
                    ok = True
                    break
            if not ok:
                inc.actions.append("adb_restart")
                ok = await self.restart_adb()
            if not ok:
                inc.actions.append("gave_up")
        finally:
            self.incidents.close(inc, ok)
            self.recovering = False
            self._keyframe_at = None
            self._last_deep = self._last_heartbeat = self.clock()
            self._changed()
        log.warning("health: %s -> %s", problem, "recovered" if ok else "gave up")
        return ok

    def report(self) -> dict[str, Any]:
        return {
            "ok": not self.last_problems and not self.recovering,
            "recovering": self.recovering,
            "problems": [problem_text(p) for p in self.last_problems],
            "codes": list(self.last_problems),
            "seconds_since_check": round(self.clock() - self.last_check, 1) if self.last_check else None,
            "recent_incidents": [i.summary() for i in self.incidents.recent(5)],
        }
