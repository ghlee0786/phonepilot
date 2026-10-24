"""Gesture calibration and task recording on a real phone (FR-INP-035..039/047/048, FR-SKL-014/016).

Run with:  pytest -m device tests/device   (USB debugging authorized, screen unlocked).
Only harmless actions in the Settings app: open, scroll, enter a menu, go back. The person's real
profiles, skills and secrets are not touched (APPDATA/LOCALAPPDATA point to a temp folder).
"""

from __future__ import annotations

import asyncio
import os
import time

import pytest
import yaml

pytestmark = pytest.mark.device

SETTINGS = "com.android.settings"


@pytest.fixture(scope="module")
def rt(tmp_path_factory):
    from phonepilot.config import Config
    from phonepilot.core.adb import DeviceManager
    from phonepilot.runtime import CoreRuntime

    if not [d for d in DeviceManager().devices() if d.state == "device"]:
        pytest.skip("no authorized device connected")
    tmp = tmp_path_factory.mktemp("device_data")
    saved = {k: os.environ.get(k) for k in ("APPDATA", "LOCALAPPDATA", "TEMP")}
    for k, sub in (("APPDATA", "roaming"), ("LOCALAPPDATA", "local"), ("TEMP", "temp")):
        os.environ[k] = str(tmp / sub)
    cfg = Config()
    cfg.privacy.env_path = str(tmp / "none.env")
    runtime = CoreRuntime(cfg)
    runtime.start()
    runtime.call_sync(runtime.connect(), timeout=60)
    yield runtime
    runtime.call_sync(runtime.shutdown(), timeout=30)
    runtime.stop_loop()
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def run(rt, coro, timeout: float = 90):  # noqa: ANN001
    return rt.call_sync(coro, timeout=timeout)


async def open_settings(rt) -> None:  # noqa: ANN001
    await rt.device.shell(f"am start -S -n {SETTINGS}/.Settings")
    await asyncio.sleep(2.0)
    await rt.stack.observer.wait_stable(3)


async def hand_to_ai(rt) -> None:  # noqa: ANN001
    a = rt.stack.arbiter
    a.last_human_ts = 0.0
    a.release_lease()
    a.allow_ai()


# ------------------------------------------------------------ a person's hand, at a person's pace
# Like the GUI, each event carries the time it happened (t). Windows timers are coarse (~15.6 ms), so the
# events are sent close to their planned time but stamped with the plan, as a mouse event would be.
async def _at(t0: float, offset_s: float) -> float:
    delay = t0 + offset_s - time.monotonic()
    if delay > 0:
        await asyncio.sleep(delay)
    return t0 + offset_s


async def human_tap(rt, x: int, y: int, hold_s: float) -> None:  # noqa: ANN001
    from phonepilot.core.scrcpy import protocol as p

    a = rt.stack.arbiter
    t0 = time.monotonic()
    await a.human_touch(p.MotionAction.DOWN, x, y, t=t0)
    await a.human_touch(p.MotionAction.UP, x, y, t=await _at(t0, hold_s))


async def human_drag(rt, x: int, y0: int, y1: int, move_s: float, rest_s: float) -> None:  # noqa: ANN001
    """Ease-in-out finger movement at 60 Hz, then rest before lifting (a deliberate scroll)."""
    from phonepilot.core.scrcpy import protocol as p

    a = rt.stack.arbiter
    t0 = time.monotonic()
    await a.human_touch(p.MotionAction.DOWN, x, y0, t=t0)
    n = max(int(move_s * 60), 2)
    for i in range(1, n + 1):
        t = i / n
        frac = t * t * (3 - 2 * t)
        await a.human_touch(p.MotionAction.MOVE, x, int(y0 + (y1 - y0) * frac), t=await _at(t0, move_s * t))
    k = 1
    while k / 60 < rest_s:  # a resting finger still reports its position
        await a.human_touch(p.MotionAction.MOVE, x, y1, t=await _at(t0, move_s + k / 60))
        k += 1
    await a.human_touch(p.MotionAction.UP, x, y1, t=await _at(t0, move_s + rest_s))


async def human_back(rt) -> None:  # noqa: ANN001
    from phonepilot.core.scrcpy import protocol as p

    a = rt.stack.arbiter
    await a.human_key(p.KeyAction.DOWN, p.Keycode.BACK)
    await a.human_key(p.KeyAction.UP, p.Keycode.BACK)


async def wait_fresh(rt, timeout: float = 10) -> None:  # noqa: ANN001
    rec = rt.task_recorder
    end = time.monotonic() + timeout
    while not rec._fresh and time.monotonic() < end:  # noqa: SLF001
        await asyncio.sleep(0.1)
    assert rec._fresh, "observation was not refreshed after the last recorded step"  # noqa: SLF001


def texts(obs) -> set[str]:  # noqa: ANN001
    return {e.label for e in obs.elements if e.label}


# ------------------------------------------------------------ calibration
def test_d1_calibration_on_real_phone(rt):  # noqa: ANN001
    dw, dh = rt.device.display_size
    cx = dw // 2

    async def demo() -> None:
        await open_settings(rt)
        rt.start_calibration()
        for hold in (0.08, 0.10, 0.12, 0.10, 0.09):  # taps on the empty header area
            await human_tap(rt, int(dw * 0.25), int(dh * 0.12), hold)
            await asyncio.sleep(0.4)
        for hold in (0.9, 1.0):  # long presses on the same harmless spot
            await human_tap(rt, int(dw * 0.25), int(dh * 0.12), hold)
            await asyncio.sleep(0.4)
        for _ in range(2):  # list scrolls down and back up
            await human_drag(rt, cx, int(dh * 0.75), int(dh * 0.35), move_s=0.45, rest_s=0.15)
            await asyncio.sleep(0.6)
            await human_drag(rt, cx, int(dh * 0.35), int(dh * 0.75), move_s=0.45, rest_s=0.15)
            await asyncio.sleep(0.6)

    run(rt, demo())
    counts = rt.calibration_counts()
    assert counts == {"tap": 5, "long_press": 2, "swipe": 4}, counts
    prof = rt.finish_calibration(save=True)
    assert prof["calibrated"]
    assert 80 <= prof["tap_ms"] <= 130, prof
    assert 900 <= prof["long_press_ms"] <= 1100, prof
    # ~960 px in ~0.45 s with ease-in-out: about 2 px/ms
    assert 1.5 <= prof["swipe_px_per_ms"] <= 3.2, prof
    assert 100 <= prof["settle_ms"] <= 220, prof
    assert rt.profiles.get(rt._device_key()).calibrated  # noqa: SLF001  stored per device
    rep = rt.fidelity_report()
    assert rep["overall"] >= 70, rep["text"]


def test_d2_profile_scroll_moves_the_list_without_fling(rt):  # noqa: ANN001
    tools, obs = rt.stack.tools, rt.stack.observer

    async def go() -> tuple[set[str], set[str], float]:
        await open_settings(rt)
        await hand_to_ai(rt)
        before = texts(await obs.observe())
        res = await tools.swipe(actor="device-test", start=[500, 750], end=[500, 350], hold_end=True)
        t0 = time.monotonic()
        await obs.wait_stable(3, check_progress=False)
        settle = time.monotonic() - t0
        after = texts(await obs.observe())
        assert res["ok"]
        return before, after, settle

    before, after, settle = run(rt, go())
    assert before != after, "the list did not move"
    assert settle < 1.5, f"screen kept moving {settle:.2f}s after lifting (fling?)"


# ------------------------------------------------------------ task recording -> replay
def test_d3_record_and_replay_in_settings(rt):  # noqa: ANN001
    from phonepilot.core import gesture as g

    dw, dh = rt.device.display_size
    obs = rt.stack.observer
    state: dict = {}

    async def demo() -> None:
        await open_settings(rt)
        rt.start_task_recording()
        await wait_fresh(rt)
        home = await obs.observe()
        # any read-only page that is on the first screen (entering it changes nothing)
        harmless = ("디스플레이", "Display", "소리 및 진동", "Sounds and vibration", "알림", "Notifications", "배경화면 및 스타일",
                    "모드 및 루틴", "Modes and Routines", "휴대전화 정보", "About phone", "배터리 및 디바이스 케어", "유용한 기능")
        target = next((e for e in home.elements if e.label.split(" / ")[0] in harmless), None)
        assert target is not None, sorted(texts(home))[:40]
        state["target"] = target.label.split(" / ")[0]
        await asyncio.sleep(0.8)
        await human_tap(rt, *target.center, 0.1)
        await asyncio.sleep(1.5)
        await wait_fresh(rt)
        state["inside"] = texts(await obs.observe())
        assert state["target"] in state["inside"] or texts(home) != state["inside"]
        await human_drag(rt, dw // 2, int(dh * 0.75), int(dh * 0.4), move_s=0.45, rest_s=0.15)
        await asyncio.sleep(1.0)
        await wait_fresh(rt)
        state["scrolled"] = texts(await obs.observe())
        await human_back(rt)
        await asyncio.sleep(1.5)
        await wait_fresh(rt)
        state["end"] = texts(await obs.observe())

    run(rt, demo())
    res = rt.finish_task_recording("device settings display")
    assert res["saved"], res
    doc = yaml.safe_load(open(res["path"], encoding="utf-8").read())
    steps = [s for s in doc["steps"] if s["action"] != "sleep"]
    assert [s["action"] for s in steps] == ["tap", "swipe", "system_action"], doc["steps"]
    sel = steps[0]["args"]["selector"]
    assert state["target"] in (sel.get("text"), sel.get("text_contains")), steps[0]  # the row's title, not a shared id
    assert steps[1]["args"]["hold_end"] is True, steps[1]
    assert any(s["action"] == "sleep" for s in doc["steps"]), "the person's pauses were not kept"
    assert res["name"] in rt.stack.registry.skills

    played: list = []
    arb = rt.stack.arbiter
    orig = arb.ai_gesture

    async def spy(steps_):  # noqa: ANN001, ANN202
        played.append(steps_)
        await orig(steps_)

    arb.ai_gesture = spy
    prof = rt.stack.tools.gesture_profile

    async def replay() -> dict:
        await open_settings(rt)
        await hand_to_ai(rt)
        started = await rt.stack.engine.start(res["name"], {}, requested_by="device-test")
        for _ in range(600):
            st = rt.stack.engine.status(started["run_id"])
            if st["state"] in {"COMPLETED", "FAILED", "ABORTED", "NEEDS_ATTENTION", "WAITING_HUMAN"}:
                break
            await asyncio.sleep(0.1)
        await asyncio.sleep(1.0)
        st["end"] = texts(await obs.observe())
        return st

    try:
        st = run(rt, replay(), timeout=120)
    finally:
        arb.ai_gesture = orig
    assert st["state"] == "COMPLETED", st
    # the replay ended where the person ended (Settings main list)
    overlap = len(st["end"] & state["end"]) / max(len(state["end"]), 1)
    assert overlap >= 0.6, (sorted(st["end"]), sorted(state["end"]))
    # gestures came from the calibrated profile
    taps = [s for s in played if len(s) == 2]
    assert taps and taps[0][-1].t_ms == pytest.approx(prof.tap_ms), (prof, taps)
    swipes = [s for s in played if len(s) > 2]
    assert swipes, "swipe step did not play"
    last_move = max(x.t_ms for x in swipes[0] if x.action is g.TouchAction.MOVE and (x.x, x.y) != (swipes[0][-1].x, swipes[0][-1].y))
    assert swipes[0][-1].t_ms - last_move >= prof.settle_ms * 0.8, "replayed swipe did not rest before lifting"
