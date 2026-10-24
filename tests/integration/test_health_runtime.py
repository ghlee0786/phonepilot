"""PRD 25 end to end: a CoreRuntime (demo device) detects injected faults on its own and replaces the session."""

from __future__ import annotations

import time

import pytest

from phonepilot.config import Config
from phonepilot.issue_report import build_report
from phonepilot.runtime import CoreRuntime, ConnState


@pytest.fixture
def rt(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setenv("TEMP", str(tmp_path / "temp"))
    cfg = Config()
    cfg.privacy.env_path = str(tmp_path / "none.env")
    cfg.health.interval_s = 0.1  # fast watchdog for the test
    cfg.health.deep_check_s = 0.5
    runtime = CoreRuntime(cfg, demo=True)
    runtime.start()
    runtime.call_sync(runtime.connect(), timeout=30)
    yield runtime
    runtime.call_sync(runtime.shutdown(), timeout=30)
    runtime.stop_loop()


def wait_until(cond, timeout: float = 10.0) -> None:  # noqa: ANN001
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return
        time.sleep(0.05)
    raise AssertionError("condition not met in time")


@pytest.mark.parametrize("fault", ["decoder_dead", "control_dead", "video_dead_after_input", "server_gone"])
def test_r1_runtime_replaces_a_broken_session_by_itself(rt, fault):
    old = rt.device
    if fault == "decoder_dead":
        old.decoder_dead = True
    elif fault == "control_dead":
        old.control_dead = True
    elif fault == "server_gone":
        old.probe_problems = ["SERVER_PROCESS_GONE"]
    else:
        old.video_dead = True
        rt.health.cfg.stall_after = 0.2
        rt.health.cfg.keyframe_timeout = 0.3
        rt.stack.arbiter.last_input_ts = time.monotonic()  # a tap whose result never reaches the PC
    wait_until(lambda: rt.device is not old and rt.state == ConnState.STREAMING)
    assert old.closed, "the broken session must be closed, not leaked"
    inc = rt.incidents.recent(1)[0]
    assert inc.recovered is True and "reconnect" in inc.actions
    # the new session works: an AI action goes through
    res = rt.call_sync(rt.stack.tools.tap(actor="claude-code", selector={"text": "설정"}))
    assert res["ok"] and rt.device.current == "settings"


def test_r2_manual_reconnect_replaces_a_session_that_looks_fine(rt):
    old = rt.device
    assert rt.call_sync(rt.reconnect_session("manual"))
    assert rt.device is not old and old.closed and rt.state == ConnState.STREAMING


def test_r3_drop_handler_ignores_sessions_it_already_replaced(rt):
    old = rt.device
    rt.demo = False  # the drop handler only runs for real devices
    try:
        assert rt.call_sync(rt.reconnect_session("manual"))
        rt.call_sync(rt._on_disconnect(old))  # noqa: SLF001  late "disconnected" from the closed session
        assert rt.state == ConnState.STREAMING and not [i for i in rt.incidents.items if i.problem == "DISCONNECTED"]
    finally:
        rt.demo = True


def test_r4_health_is_visible_to_the_ai_and_in_problem_reports(rt):
    rt.device.decoder_dead = True
    wait_until(lambda: rt.incidents.recent(1) and rt.incidents.recent(1)[0].recovered is not None)
    info = rt.call_sync(rt.stack.tools.session_info(actor="claude-code"))
    assert info["health"]["state"] == "STREAMING"
    assert any("영상 디코더가 멈춤" in s for s in info["health"]["recent_incidents"])
    _, body = build_report("x", incidents=[i.summary() for i in rt.incidents.recent(10)])
    assert "## 최근 연결 장애" in body and "영상 디코더가 멈춤" in body


def test_r5_check_now_runs_the_deep_checks(rt):
    rt.device.probe_problems = ["FORWARD_MISSING"]
    rt.health.cfg.deep_every = 9999  # only the button should find it
    old = rt.device
    rep = rt.call_sync(rt.check_health(), timeout=30)
    assert rt.device is not old and rep["state"] == "STREAMING"
    assert "PC-폰 포트 연결이 사라짐" in rep["recent_incidents"][0]


def test_r6_self_heal_can_be_switched_off(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    cfg = Config()
    cfg.privacy.env_path = str(tmp_path / "none.env")
    cfg.health.self_heal = False
    runtime = CoreRuntime(cfg, demo=True)
    runtime.start()
    try:
        runtime.call_sync(runtime.connect(), timeout=30)
        assert runtime.health is None and runtime.health_report()["state"] == "STREAMING"
    finally:
        runtime.call_sync(runtime.shutdown(), timeout=30)
        runtime.stop_loop()
