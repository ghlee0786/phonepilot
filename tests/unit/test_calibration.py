"""FR-INP-035..039: gesture calibration measures the user's own gestures and turns them into deterministic defaults."""

import pytest

from phonepilot.core import gesture as g
from phonepilot.core.calibration import (
    CLAMPS,
    CalibrationRecorder,
    GestureKind,
    GestureProfile,
    ProfileStore,
    build_profile,
    measure,
)


def points(steps: list[g.TouchStep]) -> list[tuple[float, float, float]]:
    return [(s.t_ms, float(s.x), float(s.y)) for s in steps if s.pointer == 0]


def test_measure_tap_and_long_press():
    tap = measure(points(g.tap(500, 500, 85)))
    assert tap.kind is GestureKind.TAP and tap.duration_ms == pytest.approx(85)
    lp = measure(points(g.long_press(500, 500, 900)))
    assert lp.kind is GestureKind.LONG_PRESS and lp.duration_ms == pytest.approx(900)
    assert measure(points(g.tap(1, 1, 400))) is None  # ambiguous press is ignored


@pytest.mark.parametrize("arc", [-0.1, 0.0, 0.08])
def test_measure_swipe_recovers_curvature(arc):
    s = measure(points(g.swipe(540, 1800, 540, 600, 400, arc=arc)))
    assert s.kind is GestureKind.SWIPE
    assert s.arc == pytest.approx(arc, abs=0.01)
    assert s.distance_px == pytest.approx(1200, abs=2)


def test_measure_swipe_speed_settle_and_peak():
    s = measure(points(g.precise_scroll(540, 1800, 540, 600, duration_ms=500, settle_ms=150)))
    assert s.settle_ms == pytest.approx(150, abs=10)
    assert s.speed_px_per_ms == pytest.approx(1200 / 500, rel=0.05)
    early = measure(points(g.swipe(540, 1800, 540, 600, 400, ease=(0.2, 0.36))))
    late = measure(points(g.swipe(540, 1800, 540, 600, 400, ease=(0.64, 0.8))))
    assert early.peak < 0.5 < late.peak


def human_samples():
    """A small, varied set of 'human' gestures (different speeds, slight arcs)."""
    out = []
    for ms in (60, 75, 90, 80, 70):
        out.append(measure(points(g.tap(500, 900, ms))))
    for ms in (700, 900, 800):
        out.append(measure(points(g.long_press(500, 900, ms))))
    for ms, arc in ((350, 0.05), (420, 0.06), (380, 0.04), (300, 0.07), (450, 0.05)):
        out.append(measure(points(g.precise_scroll(540, 1900, 560, 700, duration_ms=ms, settle_ms=110))))
        out[-1] = measure(points(g.swipe(540, 1900, 560, 700, ms, arc=arc)))
    return [s for s in out if s is not None]


def test_build_profile_uses_medians():
    prof = build_profile(human_samples())
    assert prof.calibrated
    assert prof.tap_ms == 75 and prof.long_press_ms == 800
    assert prof.arc == pytest.approx(0.05, abs=0.01)
    assert prof.samples == {"tap": 5, "long_press": 3, "swipe": 5}
    dist = ((560 - 540) ** 2 + (700 - 1900) ** 2) ** 0.5
    assert prof.swipe_ms(dist) == pytest.approx(380, rel=0.1)  # median swipe duration reproduced


def test_profile_is_clamped_to_recognisable_ranges():
    weird = [measure(points(g.tap(1, 1, 5))) for _ in range(3)]  # 5 ms taps are too short to be reliable
    weird += [measure(points(g.swipe(0, 2000, 0, 0, 30, arc=0.4))) for _ in range(3)]
    prof = build_profile([w for w in weird if w])
    assert prof.tap_ms == CLAMPS["tap_ms"][0]
    assert prof.arc == CLAMPS["arc"][1]
    assert prof.swipe_px_per_ms == CLAMPS["swipe_px_per_ms"][1]


def test_too_few_samples_keeps_defaults():
    prof = build_profile([measure(points(g.tap(1, 1, 90)))])
    assert not prof.calibrated and prof.tap_ms == 70


def test_uncalibrated_profile_reproduces_builtin_gestures():
    prof = GestureProfile()
    assert prof.ease == g.DEFAULT_EASE and prof.swipe_ms(1200) == 300 and prof.scroll_ms(1200) == 600
    assert g.swipe(0, 0, 0, 900, prof.swipe_ms(900), arc=prof.arc, ease=prof.ease) == g.swipe(0, 0, 0, 900)


def test_profile_is_deterministic():
    prof = build_profile(human_samples())
    a = g.swipe(540, 1800, 540, 600, prof.swipe_ms(1200), arc=prof.arc, ease=prof.ease)
    b = g.swipe(540, 1800, 540, 600, prof.swipe_ms(1200), arc=prof.arc, ease=prof.ease)
    assert a == b and g.open_pointers(a) == set()


def test_ease_follows_peak():
    early = GestureProfile(calibrated=True, peak=0.35).ease
    late = GestureProfile(calibrated=True, peak=0.65).ease
    assert early[0] < g.DEFAULT_EASE[0] < late[0]


def test_recorder_segments_gestures():
    r = CalibrationRecorder()
    r.feed("down", 0, 0, 0)  # ignored while inactive
    r.start()
    for s in g.tap(500, 500, 80) + g.swipe(500, 1800, 500, 600, 300):
        r.feed({g.TouchAction.DOWN: "down", g.TouchAction.MOVE: "move", g.TouchAction.UP: "up"}[s.action], s.x, s.y, 1000 + s.t_ms)
    r.feed("down", 1, 1, 5000)
    r.feed("cancel", 1, 1, 5010)
    assert r.counts() == {"tap": 1, "long_press": 0, "swipe": 1}
    assert len(r.stop()) == 2 and not r.active


def test_store_roundtrip(tmp_path):
    store = ProfileStore(tmp_path / "gesture_profiles.yaml")
    assert not store.get("dev1").calibrated
    prof = build_profile(human_samples())
    store.save("dev1", prof)
    loaded = store.get("dev1")
    assert loaded.to_dict() == prof.to_dict()
    assert "dev1" in (tmp_path / "gesture_profiles.yaml").read_text()
    store.reset("dev1")
    assert not store.get("dev1").calibrated
