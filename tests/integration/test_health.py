"""PRD 25 reliability: early detection, step-wise self-recovery and incident records, scenario by scenario.

The HealthMonitor runs against a FakeDevice with injected faults. Its clock is real monotonic time plus an offset
the tests move forward, so "2 seconds later" does not take 2 seconds.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from phonepilot.core.health import HealthConfig, HealthMonitor, IncidentLog
from phonepilot.testing.fake_device import FakeDevice, demo_screens

from tests.conftest import CANARY_PASSWORD


class Clock:
    def __init__(self) -> None:
        self.offset = 0.0

    def __call__(self) -> float:
        return time.monotonic() + self.offset

    def advance(self, s: float) -> None:
        self.offset += s


class World:
    """One FakeDevice plus the runtime callbacks the monitor uses; `reconnect` swaps in a fresh device."""

    def __init__(self, tmp_path, reconnect_ok: list[bool] | None = None, adb_ok: bool = True) -> None:  # noqa: ANN001
        self.dev = FakeDevice(demo_screens(), "home")
        self.clock = Clock()
        self.last_input = -1e9
        self.reconnects: list[str] = []
        self.adb_restarts = 0
        self.reconnect_ok = list(reconnect_ok) if reconnect_ok is not None else [True]
        self.adb_ok = adb_ok
        self.incidents = IncidentLog(tmp_path / "incidents.jsonl")
        self.mon = HealthMonitor(
            get_device=lambda: self.dev,
            get_last_input=lambda: self.last_input,
            reconnect=self._reconnect,
            restart_adb=self._restart_adb,
            incidents=self.incidents,
            cfg=HealthConfig(),
            clock=self.clock,
        )

    async def _reconnect(self, reason: str) -> bool:
        self.reconnects.append(reason)
        ok = self.reconnect_ok.pop(0) if self.reconnect_ok else False
        if ok:
            self.dev = FakeDevice(demo_screens(), "home")
        return ok

    async def _restart_adb(self) -> bool:
        self.adb_restarts += 1
        if self.adb_ok:
            self.dev = FakeDevice(demo_screens(), "home")
        return self.adb_ok

    def input_now(self) -> None:
        self.last_input = self.clock()


# ------------------------------------------------------------------ no false alarms
async def test_h1_idle_static_screen_only_heartbeats(tmp_path):
    w = World(tmp_path)
    for _ in range(5):
        w.clock.advance(2)
        assert await w.mon.check(deep=False) == []
    assert w.dev.keyframe_requests == 0
    w.clock.advance(30)  # nothing changed on screen for a while: prove the video path still works
    assert await w.mon.check(deep=False) == []
    assert w.dev.keyframe_requests == 1
    w.clock.advance(2)
    assert await w.mon.check(deep=False) == []  # the heartbeat was answered
    assert w.reconnects == [] and list(w.incidents.items) == []


async def test_h2_tap_on_nothing_is_not_an_incident(tmp_path):
    w = World(tmp_path)
    w.input_now()  # a tap that changes nothing on screen: no new frame
    w.clock.advance(2.5)
    assert await w.mon.check(deep=False) == ["VIDEO_STALLED"]
    assert w.dev.keyframe_requests == 1
    w.clock.advance(1)
    assert await w.mon.check(deep=False) == []  # the key frame arrived: the stream is fine
    assert w.reconnects == [] and list(w.incidents.items) == []


# ------------------------------------------------------------------ step 1: key frame
async def test_h3_frozen_video_after_input_recovers_with_a_key_frame(tmp_path):
    w = World(tmp_path)
    w.dev.video_frozen = True
    w.dev.goto("settings")  # the phone moved on; the PC sees nothing
    w.input_now()
    seq = w.dev.framebus.latest().seq
    w.clock.advance(2.5)
    await w.mon.check(deep=False)
    assert w.dev.framebus.latest().seq > seq and not w.dev.video_frozen
    w.clock.advance(1)
    assert await w.mon.check(deep=False) == []
    assert w.reconnects == []


async def test_h4_decoder_waiting_for_key_frame_is_recorded_and_fixed(tmp_path):
    w = World(tmp_path)
    w.dev.waiting_key_since = w.clock() - 2  # packets were dropped; frozen until a key frame
    assert await w.mon.check(deep=False) == ["WAITING_KEYFRAME"]
    w.clock.advance(1)
    await w.mon.check(deep=False)
    inc = w.incidents.recent(1)[0]
    assert inc.problem == "WAITING_KEYFRAME" and inc.actions == ["keyframe"] and inc.recovered is True
    assert w.reconnects == []


# ------------------------------------------------------------------ step 2: reconnect
async def test_h5_dead_video_escalates_from_key_frame_to_reconnect(tmp_path):
    w = World(tmp_path)
    w.dev.video_dead = True
    w.input_now()
    w.clock.advance(2.5)
    await w.mon.check(deep=False)  # asks for a key frame
    w.clock.advance(3.5)
    assert await w.mon.check(deep=False) == ["NO_FRAME_AFTER_RESET"]
    assert w.reconnects == ["NO_FRAME_AFTER_RESET"]
    inc = w.incidents.recent(1)[0]
    assert inc.actions == ["keyframe", "reconnect"] and inc.recovered is True


@pytest.mark.parametrize(
    ("fault", "code"),
    [("decoder_dead", "DECODER_DEAD"), ("control_dead", "CONTROL_DEAD"), ("link_up", "DISCONNECTED")],
)
async def test_h6_broken_transport_reconnects_directly(tmp_path, fault, code):
    w = World(tmp_path)
    setattr(w.dev, fault, fault != "link_up")  # link_up=False means disconnected
    assert await w.mon.check(deep=False) == [code]
    assert w.reconnects == [code] and w.dev.keyframe_requests == 0  # no point asking a broken transport
    assert w.incidents.recent(1)[0].actions == ["reconnect"]


@pytest.mark.parametrize("code", ["SERVER_PROCESS_GONE", "FORWARD_MISSING", "ADB_UNREACHABLE"])
async def test_h7_deep_check_finds_problems_the_stream_cannot_see(tmp_path, code):
    w = World(tmp_path)
    w.dev.probe_problems = [code]
    assert await w.mon.check(deep=False) == []  # cheap checks see nothing
    w.clock.advance(16)
    assert await w.mon.check() == [code]  # the 15 s deep check does
    assert w.reconnects == [code]


async def test_h8_key_frame_request_failing_means_control_is_dead(tmp_path):
    w = World(tmp_path)
    w.input_now()
    w.clock.advance(2.5)
    w.dev.control_dead = True
    w.dev.health = lambda: type(w.dev).health(w.dev).__class__(connected=True)  # transport has not noticed yet
    await w.mon.check(deep=False)
    assert w.reconnects == ["CONTROL_DEAD"]


# ------------------------------------------------------------------ steps 3-4: adb restart, give up
async def test_h9_adb_restart_after_reconnects_fail(tmp_path):
    w = World(tmp_path, reconnect_ok=[False, False], adb_ok=True)
    w.dev.decoder_dead = True
    await w.mon.check(deep=False)
    inc = w.incidents.recent(1)[0]
    assert inc.actions == ["reconnect", "reconnect", "adb_restart"] and inc.recovered is True
    assert w.adb_restarts == 1


async def test_h10_gives_up_and_leaves_an_open_record(tmp_path):
    w = World(tmp_path, reconnect_ok=[False, False], adb_ok=False)
    w.dev.decoder_dead = True
    await w.mon.check(deep=False)
    inc = w.incidents.recent(1)[0]
    assert inc.actions[-1] == "gave_up" and inc.recovered is False
    assert "복구 실패" in inc.summary()
    rep = w.mon.report()
    assert rep["recent_incidents"] and "영상 디코더가 멈춤" in rep["recent_incidents"][0]


async def test_h11_one_recovery_at_a_time(tmp_path):
    w = World(tmp_path)
    gate = asyncio.Event()

    async def slow(reason: str) -> bool:
        w.reconnects.append(reason)
        await gate.wait()
        w.dev = FakeDevice(demo_screens(), "home")
        return True

    w.mon.reconnect = slow
    w.dev.decoder_dead = True
    first = asyncio.ensure_future(w.mon.check(deep=False))
    await asyncio.sleep(0.05)
    assert w.mon.recovering
    assert await w.mon.check(deep=False) == []  # a second round while recovering does nothing
    gate.set()
    await first
    assert w.reconnects == ["DECODER_DEAD"]


# ------------------------------------------------------------------ after the fact
async def test_h12_incident_file_holds_codes_only_and_stays_small(tmp_path):
    w = World(tmp_path)
    w.dev.info.serial_tail = "SERIAL1234"
    for _ in range(3):
        w.dev.decoder_dead = True
        await w.mon.check(deep=False)
    lines = (tmp_path / "incidents.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    rec = json.loads(lines[0])
    assert set(rec) == {"started", "problem", "actions", "recovered", "duration_ms"}
    text = "\n".join(lines)
    assert "SERIAL1234" not in text and CANARY_PASSWORD not in text
    log = IncidentLog(tmp_path / "big.jsonl")
    for _ in range(250):
        log.close(log.open("DECODER_DEAD"), True)
    assert len((tmp_path / "big.jsonl").read_text(encoding="utf-8").splitlines()) == 200
