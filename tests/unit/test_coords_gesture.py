"""TC-MIR-05/06 (aspect fit) and TC-INP-01..03 (gesture synthesis)."""

import pytest

from phonepilot.core import gesture as g
from phonepilot.core.coords import fit_rect, norm_to_px, px_to_norm, window_to_frame


def test_fit_rect_letterbox_tc_mir_05():
    r = fit_rect(1000, 500, 1080, 2400)
    assert r.h == pytest.approx(500)
    assert r.w == pytest.approx(225)
    assert r.x == pytest.approx(387.5)
    assert r.y == pytest.approx(0)


def test_window_to_frame_tc_mir_06():
    # center of the content maps to the center of the frame
    assert window_to_frame(500, 250, 1000, 500, 1080, 2400) == (540, 1200)
    # letterbox click
    assert window_to_frame(10, 250, 1000, 500, 1080, 2400) is None
    # clamped (drag that left the picture)
    assert window_to_frame(10, 250, 1000, 500, 1080, 2400, clamp=True) == (0, 1200)
    # round-trip error <= 1px
    for wx, wy in [(390, 3), (611, 497), (450, 120)]:
        dx, dy = window_to_frame(wx, wy, 1000, 500, 1080, 2400)
        r = fit_rect(1000, 500, 1080, 2400)
        assert abs((dx * r.scale + r.x) - wx) <= r.scale + 1
        assert abs((dy * r.scale + r.y) - wy) <= r.scale + 1


def test_norm_roundtrip():
    x, y = norm_to_px(500, 1000, 1080, 2400)
    assert (x, y) == (540, 2399)
    assert px_to_norm(x, y, 1080, 2400) == (500, 1000)


def test_tap_tc_inp_01():
    steps = g.tap(10, 20)
    assert [s.action for s in steps] == [g.TouchAction.DOWN, g.TouchAction.UP]
    assert steps[1].t_ms - steps[0].t_ms == 70
    assert all((s.x, s.y) == (10, 20) for s in steps)


def test_precise_scroll_settles_before_up_tc_inp_02():
    steps = g.precise_scroll(500, 1600, 500, 800)
    up = steps[-1]
    last_move = [s for s in steps if s.action is g.TouchAction.MOVE and (s.x, s.y) != (500, 800)][-1]
    assert up.action is g.TouchAction.UP
    assert up.t_ms - last_move.t_ms >= 100
    ys = [s.y for s in steps if s.action is g.TouchAction.MOVE]
    assert all(b <= a for a, b in zip(ys, ys[1:]))  # monotonic towards the end point


def test_pinch_tc_inp_03():
    steps = g.pinch(540, 1200, 200, 400)
    downs = [s for s in steps if s.action is g.TouchAction.DOWN]
    assert {s.pointer for s in downs} == {0, 1}
    for s0, s1 in zip([s for s in steps if s.pointer == 0 and s.action is g.TouchAction.MOVE], [s for s in steps if s.pointer == 1 and s.action is g.TouchAction.MOVE]):
        assert abs((s0.x + s1.x) / 2 - 540) <= 1 and abs((s0.y + s1.y) / 2 - 1200) <= 1
    assert g.open_pointers(steps) == set()


@pytest.mark.parametrize("make", [
    lambda: g.swipe(0, 0, 100, 100),
    lambda: g.long_press(5, 5),
    lambda: g.drag(0, 0, 300, 300),
    lambda: g.fling(0, 500, 0, 0),
    lambda: g.double_tap(1, 1),
])
def test_all_gestures_close_pointers_and_are_deterministic(make):
    a, b = make(), make()
    assert a == b  # no randomness (PRD 3.3)
    assert g.open_pointers(a) == set()
    ts = [s.t_ms for s in a]
    assert ts == sorted(ts)


def test_ease_curve_endpoints():
    assert g._cubic_bezier_ease(0) == 0 and g._cubic_bezier_ease(1) == 1
    assert 0.45 < g._cubic_bezier_ease(0.5) < 0.55
