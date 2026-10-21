"""Personal gesture calibration (PRD 7.5, FR-INP-035..039).

The user performs taps, long presses and swipes in the mirror window. Each human gesture is
measured (press duration, swipe speed, curvature, where the speed peaks, how long the finger
rests before lifting) and the per-device profile is the median of those measurements, clamped
to ranges Android reliably recognises. AI gestures then use these values as their defaults.

The profile is deterministic: the same profile always produces the same event timeline.
There is no random jitter (PRD 3.3).
"""

from __future__ import annotations

import math
import statistics
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import yaml

from phonepilot.core.gesture import DEFAULT_EASE, Ease

TOUCH_SLOP_PX = 24  # displacement below this is a tap / long press
LONG_PRESS_MIN_MS = 450
TAP_MAX_MS = 350

# (min, max) clamps keep calibrated gestures inside what Android gesture detectors accept
CLAMPS = {
    "tap_ms": (40.0, 150.0),
    "long_press_ms": (600.0, 1500.0),
    "swipe_px_per_ms": (0.8, 8.0),
    "arc": (-0.15, 0.15),
    "peak": (0.3, 0.7),
    "settle_ms": (60.0, 250.0),
}
MIN_SAMPLES = {"tap": 3, "swipe": 3, "long_press": 2}


def _clamp(name: str, value: float) -> float:
    lo, hi = CLAMPS[name]
    return min(max(value, lo), hi)


class GestureKind(str, Enum):
    TAP = "tap"
    LONG_PRESS = "long_press"
    SWIPE = "swipe"


@dataclass(frozen=True)
class GestureSample:
    kind: GestureKind
    duration_ms: float
    distance_px: float
    path_px: float
    arc: float = 0.0  # signed max deviation / distance, in gesture._path's sign convention
    peak: float = 0.5  # fraction of the moving time where the speed peaks
    settle_ms: float = 0.0  # time between the last movement and lifting the finger

    @property
    def speed_px_per_ms(self) -> float:
        moving = max(self.duration_ms - self.settle_ms, 1.0)
        return self.path_px / moving


def measure(points: list[tuple[float, float, float]]) -> GestureSample | None:
    """Classify and measure one DOWN..UP gesture. points = [(t_ms, x, y), ...] in display pixels."""
    if len(points) < 2:
        return None
    t0, x0, y0 = points[0]
    t1, x1, y1 = points[-1]
    duration = t1 - t0
    if duration <= 0:
        return None
    dx, dy = x1 - x0, y1 - y0
    distance = math.hypot(dx, dy)
    path = sum(math.hypot(b[1] - a[1], b[2] - a[2]) for a, b in zip(points, points[1:]))
    max_from_start = max(math.hypot(x - x0, y - y0) for _, x, y in points)
    if max_from_start < TOUCH_SLOP_PX:
        if duration <= TAP_MAX_MS:
            return GestureSample(GestureKind.TAP, duration, distance, path)
        if duration >= LONG_PRESS_MIN_MS:
            return GestureSample(GestureKind.LONG_PRESS, duration, distance, path)
        return None  # ambiguous press, not used for calibration
    if distance < 2 * TOUCH_SLOP_PX:
        return None

    # curvature: signed perpendicular deviation with the same normal as gesture._path (-dy, dx)/len
    nx, ny = -dy / distance, dx / distance
    deviations = [(x - x0) * nx + (y - y0) * ny for _, x, y in points]
    dev = max(deviations, key=abs)
    arc = dev / distance

    # arrival at the final position (within 1 px) -> settle time before UP.
    # Using "last step > eps" instead would count the slow end of the deceleration as resting.
    last_move_t = t0
    for i in range(len(points) - 1, 0, -1):
        if math.hypot(points[i - 1][1] - x1, points[i - 1][2] - y1) > 1.0:
            last_move_t = points[i][0]
            break
    settle = t1 - last_move_t
    moving_time = max(last_move_t - t0, 1.0)

    # where the speed peaks within the moving part
    best_v, best_t = -1.0, t0
    for a, b in zip(points, points[1:]):
        dt = b[0] - a[0]
        if dt <= 0 or b[0] > last_move_t:
            continue
        v = math.hypot(b[1] - a[1], b[2] - a[2]) / dt
        if v > best_v:
            best_v, best_t = v, (a[0] + b[0]) / 2
    peak = (best_t - t0) / moving_time
    return GestureSample(GestureKind.SWIPE, duration, distance, path, arc, peak, settle)


@dataclass
class GestureProfile:
    """Defaults for AI gestures. `calibrated=False` reproduces the built-in fixed defaults exactly."""

    calibrated: bool = False
    tap_ms: float = 70.0
    long_press_ms: float = 800.0
    swipe_px_per_ms: float = 0.0  # 0 = use the fixed durations below
    arc: float = 0.0
    peak: float = 0.5
    settle_ms: float = 120.0
    samples: dict[str, int] = field(default_factory=dict)

    # fixed defaults used when not calibrated
    SWIPE_MS = 300.0
    SCROLL_MS = 600.0

    @property
    def ease(self) -> Ease:
        """Cubic-bezier control points shifted so the speed peaks where the user's does."""
        if not self.calibrated:
            return DEFAULT_EASE
        shift = (self.peak - 0.5) * 0.8
        p1 = min(max(DEFAULT_EASE[0] + shift, 0.05), 0.95)
        p2 = min(max(DEFAULT_EASE[1] + shift, 0.05), 0.95)
        return (round(p1, 3), round(p2, 3))

    def swipe_ms(self, distance_px: float) -> float:
        if not self.calibrated or self.swipe_px_per_ms <= 0:
            return self.SWIPE_MS
        return min(max(distance_px / self.swipe_px_per_ms, 120.0), 900.0)

    def scroll_ms(self, distance_px: float) -> float:
        """Precise scrolls move a bit slower than free swipes so they land where intended."""
        if not self.calibrated or self.swipe_px_per_ms <= 0:
            return self.SCROLL_MS
        return min(max(distance_px / (self.swipe_px_per_ms * 0.6), 250.0), 1200.0)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["ease"] = list(self.ease)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> GestureProfile:
        keys = {"calibrated", "tap_ms", "long_press_ms", "swipe_px_per_ms", "arc", "peak", "settle_ms", "samples"}
        return cls(**{k: v for k, v in d.items() if k in keys})


def build_profile(samples: list[GestureSample], base: GestureProfile | None = None) -> GestureProfile:
    """Median of the measurements, clamped. Kinds with too few samples keep the base values."""
    base = base or GestureProfile()
    taps = [s for s in samples if s.kind is GestureKind.TAP]
    longs = [s for s in samples if s.kind is GestureKind.LONG_PRESS]
    swipes = [s for s in samples if s.kind is GestureKind.SWIPE]
    prof = GestureProfile(**{k: v for k, v in asdict(base).items()})
    prof.samples = {"tap": len(taps), "long_press": len(longs), "swipe": len(swipes)}
    if len(taps) >= MIN_SAMPLES["tap"]:
        prof.tap_ms = round(_clamp("tap_ms", statistics.median(s.duration_ms for s in taps)), 1)
    if len(longs) >= MIN_SAMPLES["long_press"]:
        prof.long_press_ms = round(_clamp("long_press_ms", statistics.median(s.duration_ms for s in longs)), 1)
    if len(swipes) >= MIN_SAMPLES["swipe"]:
        prof.swipe_px_per_ms = round(_clamp("swipe_px_per_ms", statistics.median(s.speed_px_per_ms for s in swipes)), 3)
        prof.arc = round(_clamp("arc", statistics.median(s.arc for s in swipes)), 3)
        prof.peak = round(_clamp("peak", statistics.median(s.peak for s in swipes)), 3)
        prof.settle_ms = round(_clamp("settle_ms", statistics.median(s.settle_ms for s in swipes)), 1)
    prof.calibrated = len(taps) >= MIN_SAMPLES["tap"] or len(swipes) >= MIN_SAMPLES["swipe"]
    return prof


class CalibrationRecorder:
    """Collects human single-finger gestures from the InputArbiter while calibration is on."""

    def __init__(self) -> None:
        self.active = False
        self.samples: list[GestureSample] = []
        self._current: list[tuple[float, float, float]] | None = None

    def start(self) -> None:
        self.active = True
        self.samples = []
        self._current = None

    def stop(self) -> list[GestureSample]:
        self.active = False
        self._current = None
        return list(self.samples)

    def feed(self, action: str, x: float, y: float, t_ms: float) -> None:
        if not self.active:
            return
        if action == "down":
            self._current = [(t_ms, x, y)]
        elif self._current is not None and action == "move":
            self._current.append((t_ms, x, y))
        elif self._current is not None and action == "up":
            self._current.append((t_ms, x, y))
            sample = measure(self._current)
            if sample is not None:
                self.samples.append(sample)
            self._current = None
        elif action == "cancel":
            self._current = None

    def counts(self) -> dict[str, int]:
        out = {k.value: 0 for k in GestureKind}
        for s in self.samples:
            out[s.kind.value] += 1
        return out


class ProfileStore:
    """Per-device profiles in a YAML file, keyed by the serial hash (no raw serials on disk)."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def _load(self) -> dict[str, Any]:
        if self.path.exists():
            return yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        return {}

    def get(self, device_key: str) -> GestureProfile:
        data = self._load().get(device_key)
        return GestureProfile.from_dict(data) if data else GestureProfile()

    def save(self, device_key: str, profile: GestureProfile) -> None:
        data = self._load()
        d = profile.to_dict()
        d.pop("ease", None)
        data[device_key] = d
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=True), encoding="utf-8")

    def reset(self, device_key: str) -> None:
        data = self._load()
        data.pop(device_key, None)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=True), encoding="utf-8")
