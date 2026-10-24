"""FR-SKL-014: a human demonstration in the mirror window becomes a replayable skill; gesture fidelity report."""

import pytest

from phonepilot.core import fidelity
from phonepilot.core import gesture as g
from phonepilot.core.calibration import GestureProfile, build_profile, measure
from phonepilot.core.scrcpy import protocol as p
from phonepilot.skills import loader
from phonepilot.skills.recorder import TaskRecorder, dump, safe_skill_name

from tests.integration.test_workflow import wait_done


class Clock:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t


async def human_tap(stack, clock, x, y, hold_s=0.08):
    await stack.arbiter.human_touch(p.MotionAction.DOWN, x, y)
    clock.t += hold_s
    await stack.arbiter.human_touch(p.MotionAction.UP, x, y)


@pytest.fixture
def recording(stack):
    clock = Clock()
    stack.arbiter._clock = clock
    rec = TaskRecorder(observation=lambda: stack.observer.last, display_size=lambda: stack.device.display_size)
    rec.start()
    stack.arbiter.task_recorder = rec
    return rec, clock


async def fresh(stack, rec):
    await stack.observer.observe()
    rec.mark_fresh()


async def test_recorded_tap_text_and_back_become_selector_steps(stack, recording):
    rec, clock = recording
    await fresh(stack, rec)
    await human_tap(stack, clock, 540, 460)  # "설정" on the demo home screen
    assert stack.device.current == "settings"
    clock.t += 2.0  # the human waited two seconds
    await fresh(stack, rec)
    await stack.arbiter.human_raw(p.inject_text("비밀 아님"))
    await stack.arbiter.human_key(p.KeyAction.DOWN, 4)
    await stack.arbiter.human_key(p.KeyAction.UP, 4)
    events = rec.stop()
    assert [e.kind for e in events] == ["tap", "text", "key"]

    doc = rec.to_skill(safe_skill_name("Open Settings!"))
    text = dump(doc)
    assert "비밀 아님" not in text  # typed text is never stored
    skill = loader.parse(text).skill
    assert skill.name == "recorded.open_settings"
    kinds = [(s.id.split("_")[0], s.action) for s in skill.steps]
    assert kinds == [("tap", "tap"), ("wait", "sleep"), ("type", "type_text"), ("key", "system_action")]
    assert skill.steps[0].args["selector"] == {"text": "설정", "resource_id": "com.android.launcher3:id/settings"}
    assert skill.steps[0].expect.package == "com.android.settings"  # replay stops if the tap did not open Settings
    assert skill.steps[1].args["ms"] == 2000
    assert skill.steps[2].args["text"] == "{{inputs.text_1}}" and "text_1" in skill.inputs


async def test_stale_observation_falls_back_to_coordinates(stack, recording):
    rec, clock = recording
    await fresh(stack, rec)
    await human_tap(stack, clock, 540, 460)
    await human_tap(stack, clock, 540, 460)  # no refresh in between: the screen already changed
    doc = rec.to_skill("recorded.t")
    second = doc["steps"][1]["args"]
    assert "selector" not in second and {"x_norm", "y_norm"} <= second.keys()


async def test_recorded_skill_replays_through_engine(stack, recording, approver, tmp_path):
    rec, clock = recording
    await fresh(stack, rec)
    await human_tap(stack, clock, 540, 460)
    rec.stop()
    stack.arbiter.task_recorder = None
    d = tmp_path / "rec_skills"
    d.mkdir()
    (d / "open.yaml").write_text(dump(rec.to_skill("recorded.open")), encoding="utf-8")
    stack.registry.builtin_dir = d
    stack.registry.reload()
    assert not stack.registry.errors, stack.registry.errors

    stack.device.goto("home")
    clock.t += 60  # the human grace period has passed, so the AI may act again
    approver.approve_all()
    started = await stack.engine.start("recorded.open", {}, requested_by="test")
    st = await wait_done(stack.engine, started["run_id"])
    assert st["state"] == "COMPLETED", st
    assert stack.device.current == "settings"


def _pts(steps):
    return [(s.t_ms, float(s.x), float(s.y)) for s in steps if s.pointer == 0]


def test_fidelity_scores_matching_profile_higher_than_default():
    human = [measure(_pts(g.tap(500, 500, d))) for d in (95, 100, 105, 110)]
    human += [measure(_pts(g.precise_scroll(540, 1800, 540, 800, dur, 180, arc=0.05))) for dur in (380, 400, 420)]
    calibrated = build_profile(human)
    good = fidelity.report(human, calibrated)
    default = fidelity.report(human, GestureProfile())
    assert good["overall"] > default["overall"]
    assert good["overall"] >= 80
    tap_row = next(r for r in good["rows"] if r["kind"] == "tap")
    assert tap_row["in_human_range"] and tap_row["ai_stdev"] == 0
    assert "유사도" in fidelity.format_report(good)


def test_fidelity_without_samples():
    rep = fidelity.report([], GestureProfile())
    assert rep["overall"] is None and "기록이 없습니다" in fidelity.format_report(rep)


async def test_recorded_drag_scroll_keeps_hold_end(stack, recording):
    rec, clock = recording
    await fresh(stack, rec)
    for t_ms, y in ((0, 1800), (100, 1500), (200, 1200), (300, 1000), (450, 1000)):
        clock.t = 200 + t_ms / 1000
        action = p.MotionAction.DOWN if t_ms == 0 else p.MotionAction.MOVE
        await stack.arbiter.human_touch(action, 540, y)
    await stack.arbiter.human_touch(p.MotionAction.UP, 540, 1000)
    step = rec.to_skill("recorded.s")["steps"][0]
    assert step["action"] == "swipe" and step["args"]["hold_end"] is True


async def test_swipe_without_duration_uses_profile(stack, monkeypatch):
    sent = []

    async def capture(steps):
        sent.append(steps)

    monkeypatch.setattr(stack.arbiter, "ai_gesture", capture)
    stack.tools.gesture_profile = GestureProfile(calibrated=True, swipe_px_per_ms=2.0, settle_ms=150)
    await stack.tools.swipe(start=[500, 800], end=[500, 300], duration_ms=None, arc=None, hold_end=True)
    steps = sent[-1]
    moving_end = max(s.t_ms for s in steps if s.y == steps[-1].y and s.action is g.TouchAction.MOVE)
    assert steps[-1].t_ms - min(s.t_ms for s in steps if s.y == steps[-1].y) >= 140  # rested ~settle_ms before lifting
    assert moving_end > 300  # 1200 px at 2 px/ms ~ 600 ms, not the fixed 300 ms default
