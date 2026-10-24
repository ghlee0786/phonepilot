"""Calibration end to end: human input through the arbiter -> profile -> AI gestures use it (FR-INP-035..039)."""

from phonepilot.config import Config
from phonepilot.core import gesture as g
from phonepilot.core.calibration import CalibrationRecorder, GestureProfile, ProfileStore
from phonepilot.core.scrcpy import protocol as p

ACTIONS = {g.TouchAction.DOWN: p.MotionAction.DOWN, g.TouchAction.MOVE: p.MotionAction.MOVE, g.TouchAction.UP: p.MotionAction.UP}


async def replay_as_human(arbiter, steps, t0):
    """Feed a gesture through the human input path with a fake clock."""
    for s in steps:
        arbiter._clock = lambda t=t0 + s.t_ms / 1000: t  # noqa: SLF001
        await arbiter.human_touch(ACTIONS[s.action], s.x, s.y)


async def test_human_gestures_are_recorded_through_the_arbiter(stack):
    rec = CalibrationRecorder()
    stack.arbiter.recorder = rec
    rec.start()
    t = 100.0
    for ms in (60, 70, 80):
        await replay_as_human(stack.arbiter, g.tap(500, 1500, ms), t)
        t += 1
    for ms in (300, 350, 400):
        await replay_as_human(stack.arbiter, g.swipe(540, 1900, 540, 700, ms, arc=0.05), t)
        t += 1
    assert rec.counts() == {"tap": 3, "long_press": 0, "swipe": 3}
    assert stack.arbiter.open_pointers == {}


async def test_ai_gestures_use_the_profile(stack, monkeypatch):
    seen = []
    real = stack.arbiter.ai_gesture

    async def capture(steps):
        seen.append(steps)
        await real(steps)

    monkeypatch.setattr(stack.arbiter, "ai_gesture", capture)
    stack.tools.gesture_profile = GestureProfile(calibrated=True, tap_ms=95, long_press_ms=1000, swipe_px_per_ms=2.0, arc=0.06, peak=0.6, settle_ms=90)
    stack.arbiter.last_human_ts = -1e9
    await stack.tools.tap(selector={"text": "설정"})
    tap = seen[-1]
    assert tap[-1].t_ms - tap[0].t_ms == 95
    stack.device.goto("home")
    await stack.tools.observe()
    await stack.tools.swipe(start=[500, 800], end=[500, 300], wait=False)
    sw = seen[-1]
    assert sw[-1].t_ms > 300  # slower than the 300 ms built-in default at 2 px/ms
    xs = [s.x for s in sw if s.action is g.TouchAction.MOVE]
    assert max(abs(x - xs[0]) for x in xs) > 10  # the calibrated arc bends the path
    await stack.tools.scroll(direction="down", wait=False)
    sc = seen[-1]
    up, last_move = sc[-1], [s for s in sc if s.action is g.TouchAction.MOVE][-1]
    assert up.t_ms - last_move.t_ms <= 90
    profile = await stack.tools.get_gesture_profile()
    assert profile["calibrated"] and profile["tap_ms"] == 95


def test_runtime_calibration_saves_per_device(tmp_path, monkeypatch):
    from phonepilot.runtime import CoreRuntime

    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setenv("TEMP", str(tmp_path / "temp"))
    cfg = Config()
    cfg.privacy.env_path = str(tmp_path / "none.env")
    rt = CoreRuntime(cfg, demo=True)
    rt.start()
    try:
        rt.call_sync(rt.connect(), timeout=30)
        assert rt.stack.arbiter.recorder is rt.recorder
        rt.start_calibration()

        async def human():
            t = 10.0
            for ms in (65, 75, 85):
                await replay_as_human(rt.stack.arbiter, g.tap(500, 1500, ms), t)
                t += 1
            for ms in (320, 360, 400):
                await replay_as_human(rt.stack.arbiter, g.swipe(540, 1900, 540, 700, ms), t)
                t += 1

        rt.call_sync(human(), timeout=30)
        prof = rt.finish_calibration(save=True)
        assert prof["calibrated"] and prof["tap_ms"] == 75
        stored = ProfileStore(tmp_path / "local" / "PhonePilot" / "gesture_profiles.yaml").get(rt.device.info.serial_hash)
        assert stored.tap_ms == 75
        assert rt.stack.tools.gesture_profile.tap_ms == 75
        rt.reset_calibration()
        assert not rt.stack.tools.gesture_profile.calibrated
    finally:
        rt.call_sync(rt.shutdown(), timeout=30)
        rt.stop_loop()
