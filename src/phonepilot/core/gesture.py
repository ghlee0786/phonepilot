"""Deterministic gesture synthesis (PRD 7.5).

Gestures are physically plausible so that Android's gesture detectors recognise them
(tap vs long press, scroll without fling, pinch). There is intentionally no randomness:
the same input always produces the same event timeline (PRD 3.3 / FR-INP-030).
All coordinates are display pixels.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class TouchAction(Enum):
    DOWN = "down"
    MOVE = "move"
    UP = "up"
    CANCEL = "cancel"


@dataclass(frozen=True)
class TouchStep:
    t_ms: float  # offset from gesture start
    action: TouchAction
    pointer: int  # logical finger index within the gesture (0, 1)
    x: int
    y: int


MOVE_INTERVAL_MS = 1000 / 120  # 120 Hz


def _cubic_bezier_ease(t: float, p1: float = 0.42, p2: float = 0.58) -> float:
    """ease-in-out timing curve with control points (p1, 0), (p2, 1); solved for x(t)=t by bisection."""
    if t <= 0:
        return 0.0
    if t >= 1:
        return 1.0

    def bx(s: float) -> float:
        return 3 * (1 - s) ** 2 * s * p1 + 3 * (1 - s) * s**2 * p2 + s**3

    def by(s: float) -> float:
        return 3 * (1 - s) * s**2 + s**3  # y control points 0 and 1

    lo, hi = 0.0, 1.0
    for _ in range(30):
        mid = (lo + hi) / 2
        if bx(mid) < t:
            lo = mid
        else:
            hi = mid
    return by((lo + hi) / 2)


def ease_out(t: float) -> float:
    return 1 - (1 - t) ** 3


def _path(x0: float, y0: float, x1: float, y1: float, frac: float, arc: float) -> tuple[int, int]:
    x = x0 + (x1 - x0) * frac
    y = y0 + (y1 - y0) * frac
    if arc:
        # perpendicular offset, peaking at the middle of the path
        dx, dy = x1 - x0, y1 - y0
        length = max((dx * dx + dy * dy) ** 0.5, 1e-6)
        bump = arc * length * 4 * frac * (1 - frac)
        x += -dy / length * bump
        y += dx / length * bump
    return int(round(x)), int(round(y))


def tap(x: int, y: int, duration_ms: float = 70) -> list[TouchStep]:
    return [TouchStep(0, TouchAction.DOWN, 0, x, y), TouchStep(duration_ms, TouchAction.UP, 0, x, y)]


def double_tap(x: int, y: int, interval_ms: float = 120) -> list[TouchStep]:
    first = tap(x, y)
    offset = first[-1].t_ms + interval_ms
    return first + [TouchStep(s.t_ms + offset, s.action, s.pointer, s.x, s.y) for s in tap(x, y)]


def long_press(x: int, y: int, duration_ms: float = 800, hold_interval_ms: float = 16) -> list[TouchStep]:
    steps = [TouchStep(0, TouchAction.DOWN, 0, x, y)]
    t = hold_interval_ms
    while t < duration_ms:
        steps.append(TouchStep(t, TouchAction.MOVE, 0, x, y))
        t += hold_interval_ms
    steps.append(TouchStep(duration_ms, TouchAction.UP, 0, x, y))
    return steps


Ease = tuple[float, float]
DEFAULT_EASE: Ease = (0.42, 0.58)


def _moves(
    x0: int, y0: int, x1: int, y1: int, start_ms: float, duration_ms: float, curve: str, arc: float, pointer: int = 0,
    ease: Ease = DEFAULT_EASE,
) -> list[TouchStep]:
    n = max(int(duration_ms / MOVE_INTERVAL_MS), 1)
    steps = []
    for i in range(1, n + 1):
        t = i / n
        frac = {"linear": t, "ease_out": ease_out(t)}.get(curve, None)
        if frac is None:
            frac = _cubic_bezier_ease(t, *ease)
        px, py = _path(x0, y0, x1, y1, frac, arc)
        steps.append(TouchStep(start_ms + t * duration_ms, TouchAction.MOVE, pointer, px, py))
    return steps


def swipe(
    x0: int, y0: int, x1: int, y1: int, duration_ms: float = 300, curve: str = "ease", arc: float = 0.0, ease: Ease = DEFAULT_EASE
) -> list[TouchStep]:
    steps = [TouchStep(0, TouchAction.DOWN, 0, x0, y0)]
    steps += _moves(x0, y0, x1, y1, 0, duration_ms, curve, arc, ease=ease)
    steps.append(TouchStep(duration_ms, TouchAction.UP, 0, x1, y1))
    return steps


def precise_scroll(
    x0: int, y0: int, x1: int, y1: int, duration_ms: float = 600, settle_ms: float = 120, arc: float = 0.0, ease: Ease = DEFAULT_EASE
) -> list[TouchStep]:
    """Drag then hold still before lifting so the release velocity is ~0 and no fling happens."""
    steps = [TouchStep(0, TouchAction.DOWN, 0, x0, y0)]
    steps += _moves(x0, y0, x1, y1, 0, duration_ms, "ease", arc, ease=ease)
    t = duration_ms + MOVE_INTERVAL_MS
    while t < duration_ms + settle_ms:
        steps.append(TouchStep(t, TouchAction.MOVE, 0, x1, y1))
        t += MOVE_INTERVAL_MS
    steps.append(TouchStep(duration_ms + settle_ms, TouchAction.UP, 0, x1, y1))
    return steps


def fling(x0: int, y0: int, x1: int, y1: int, duration_ms: float = 120) -> list[TouchStep]:
    steps = [TouchStep(0, TouchAction.DOWN, 0, x0, y0)]
    steps += _moves(x0, y0, x1, y1, 0, duration_ms, "linear", 0.0)
    steps.append(TouchStep(duration_ms, TouchAction.UP, 0, x1, y1))
    return steps


def drag(
    x0: int, y0: int, x1: int, y1: int, hold_ms: float = 600, move_ms: float = 500, end_hold_ms: float = 200, ease: Ease = DEFAULT_EASE
) -> list[TouchStep]:
    steps = long_press(x0, y0, hold_ms)[:-1]  # without the UP
    steps += _moves(x0, y0, x1, y1, hold_ms, move_ms, "ease", 0.0, ease=ease)
    t = hold_ms + move_ms + MOVE_INTERVAL_MS
    while t < hold_ms + move_ms + end_hold_ms:
        steps.append(TouchStep(t, TouchAction.MOVE, 0, x1, y1))
        t += MOVE_INTERVAL_MS
    steps.append(TouchStep(hold_ms + move_ms + end_hold_ms, TouchAction.UP, 0, x1, y1))
    return steps


def pinch(cx: int, cy: int, start_span: float, end_span: float, duration_ms: float = 400, angle_deg: float = 45) -> list[TouchStep]:
    """Two fingers symmetric around (cx, cy). span = distance between fingers."""
    import math

    ux, uy = math.cos(math.radians(angle_deg)), math.sin(math.radians(angle_deg))

    def pts(span: float) -> tuple[tuple[int, int], tuple[int, int]]:
        h = span / 2
        return (int(round(cx - ux * h)), int(round(cy - uy * h))), (int(round(cx + ux * h)), int(round(cy + uy * h)))

    (a0, b0), (a1, b1) = pts(start_span), pts(end_span)
    steps = [TouchStep(0, TouchAction.DOWN, 0, *a0), TouchStep(0, TouchAction.DOWN, 1, *b0)]
    n = max(int(duration_ms / MOVE_INTERVAL_MS), 1)
    for i in range(1, n + 1):
        t = i / n
        f = _cubic_bezier_ease(t)
        a, b = pts(start_span + (end_span - start_span) * f)
        steps.append(TouchStep(t * duration_ms, TouchAction.MOVE, 0, *a))
        steps.append(TouchStep(t * duration_ms, TouchAction.MOVE, 1, *b))
    steps.append(TouchStep(duration_ms, TouchAction.UP, 1, *b1))
    steps.append(TouchStep(duration_ms, TouchAction.UP, 0, *a1))
    return steps


def open_pointers(steps: list[TouchStep]) -> set[int]:
    """Pointers left down at the end of a step list (should be empty for complete gestures)."""
    down: set[int] = set()
    for s in steps:
        if s.action is TouchAction.DOWN:
            down.add(s.pointer)
        elif s.action in (TouchAction.UP, TouchAction.CANCEL):
            down.discard(s.pointer)
    return down
