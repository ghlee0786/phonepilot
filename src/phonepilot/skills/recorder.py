"""Demonstration recording -> replayable skill draft (FR-SKL-014).

While recording, the human's own gestures in the mirror window are classified with
`calibration.measure` and resolved against the latest observation into selector-based steps
(text / description / resource id; normalized coordinates only as a fallback). Typed text is
never stored: every text entry becomes a `{{inputs.text_N}}` placeholder. Pauses longer than
`MIN_PAUSE_MS` are kept as deterministic `sleep` steps so the replay waits where the human waited.

Replay goes through the normal workflow engine, so every step still passes
ToolService._act -> policy -> approval -> InputArbiter, and gestures use the gesture profile.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import yaml

from phonepilot.core.calibration import GestureKind, measure
from phonepilot.core.coords import px_to_norm
from phonepilot.observe import uitree

MIN_PAUSE_MS = 800
HOLD_END_MS = 60  # matches the lower clamp of the profile's settle time
MAX_PAUSE_MS = 10000
KEY_ACTIONS = {3: "home", 4: "back", 187: "recents"}  # Android keycodes -> system_action


@dataclass
class RecordedEvent:
    kind: str  # tap | long_press | swipe | key | text
    t_ms: float  # gesture start
    end_ms: float
    points: tuple[tuple[float, float], ...] = ()
    element: dict[str, Any] | None = None  # selector resolved from the observation before the gesture
    label: str = ""
    package: str = ""
    key: str = ""
    hold_end: bool = False  # swipe: the finger rested at the end before lifting (drag-scroll, not a fling)


def selector_for(el: uitree.UIElement) -> dict[str, Any] | None:
    """Most specific stable selector for an element; None when it has nothing to match on."""
    if el.text:
        sel: dict[str, Any] = {"text": el.text}
    elif el.desc:
        sel = {"desc": el.desc}
    elif el.resource_id:
        sel = {"resource_id": el.resource_id}
    else:
        return None
    if el.resource_id and "resource_id" not in sel:
        sel["resource_id"] = el.resource_id
    return sel


@dataclass
class TaskRecorder:
    """Fed by the InputArbiter (human input only). `observation` returns the latest Observation or None."""

    observation: Callable[[], Any]
    display_size: Callable[[], tuple[int, int]]
    clock: Callable[[], float] = time.monotonic
    active: bool = False
    events: list[RecordedEvent] = field(default_factory=list)
    on_event: Callable[[], None] | None = None  # e.g. refresh the observation after each recorded step
    _current: list[tuple[float, float, float]] | None = None
    _down_obs: Any = None
    _fresh: bool = False  # the observation reflects the screen after the last recorded step

    def _add(self, ev: RecordedEvent) -> None:
        self.events.append(ev)
        self._fresh = False
        if self.on_event is not None:
            self.on_event()

    def mark_fresh(self) -> None:
        self._fresh = True

    def _observation(self) -> Any:
        """A stale observation would resolve a tap to an element of the previous screen; use coordinates instead."""
        return self.observation() if self._fresh else None

    def start(self) -> None:
        self.active = True
        self.events = []
        self._current = None
        self._fresh = False

    def stop(self) -> list[RecordedEvent]:
        self.active = False
        self._current = None
        return list(self.events)

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for e in self.events:
            out[e.kind] = out.get(e.kind, 0) + 1
        return out

    # ------------------------------------------------------------- feeding
    def feed_touch(self, action: str, x: float, y: float, t_ms: float) -> None:
        if not self.active:
            return
        if action == "down":
            self._current = [(t_ms, x, y)]
            self._down_obs = self._observation()
        elif self._current is not None and action == "move":
            self._current.append((t_ms, x, y))
        elif self._current is not None and action == "up":
            self._current.append((t_ms, x, y))
            self._finish_gesture(self._current, self._down_obs)
            self._current = None
        elif action == "cancel":
            self._current = None

    def feed_key(self, keycode: int, t_ms: float) -> None:
        """Called on key DOWN. Only navigation keys are recorded; typing arrives via feed_text."""
        if self.active and keycode in KEY_ACTIONS:
            obs = self._observation()
            self._add(RecordedEvent("key", t_ms, t_ms, key=KEY_ACTIONS[keycode], package=obs.package if obs is not None else ""))

    def feed_text(self, t_ms: float) -> None:
        """Text was injected. The content is deliberately not passed in; consecutive chunks merge."""
        if not self.active:
            return
        if self.events and self.events[-1].kind == "text":
            self.events[-1].end_ms = t_ms
            return
        obs = self._observation()
        focused = next((e for e in obs.elements if e.focused), None) if obs is not None else None
        sel = selector_for(focused) if focused is not None else None
        pkg = obs.package if obs is not None else ""
        self._add(RecordedEvent("text", t_ms, t_ms, element=sel, label=focused.label if focused else "", package=pkg))

    def _finish_gesture(self, pts: list[tuple[float, float, float]], obs: Any) -> None:
        sample = measure(pts)
        if sample is None:
            return
        (t0, x0, y0), (t1, x1, y1) = pts[0], pts[-1]
        ev = RecordedEvent(sample.kind.value, t0, t1, points=((x0, y0), (x1, y1)), hold_end=sample.settle_ms >= HOLD_END_MS)
        if obs is not None:
            ev.package = obs.package
            if sample.kind is not GestureKind.SWIPE:
                el = uitree.element_at(obs.elements, int(x0), int(y0))
                if el is not None:
                    ev.element = selector_for(el)
                    ev.label = el.label
        self._add(ev)

    # ------------------------------------------------------------ to skill
    def _norm(self, x: float, y: float) -> list[int]:
        dw, dh = self.display_size()
        return list(px_to_norm(x, y, dw, dh))

    @staticmethod
    def _add_expectations(steps: list[dict[str, Any]]) -> None:
        """A step that led to another app expects that app, so replay stops instead of tapping on the wrong screen."""
        for st in steps:
            cur, nxt = st.pop("_pkg", ""), st.pop("_next_pkg", "")
            if cur and nxt and nxt != cur:
                st["expect"] = {"package": nxt, "timeout_ms": 10000}

    def to_skill(self, name: str, description: str = "") -> dict[str, Any]:
        """Skill document (PRD 11.2 schema) for the recorded events."""
        steps: list[dict[str, Any]] = []
        inputs: dict[str, Any] = {}
        packages: list[str] = []
        prev_end: float | None = None
        n_text = 0
        for i, ev in enumerate(self.events, 1):
            if prev_end is not None:
                gap = ev.t_ms - prev_end
                if gap >= MIN_PAUSE_MS:
                    steps.append({"id": f"wait_{i}", "action": "sleep", "args": {"ms": int(min(gap, MAX_PAUSE_MS))}})
            prev_end = ev.end_ms
            if ev.package and ev.package not in packages:
                packages.append(ev.package)
            what = ev.label or ev.kind
            first = len(steps)
            if ev.kind in ("tap", "long_press"):
                args: dict[str, Any] = {"reason": f"시연 {i}: {what}"}
                if ev.element:
                    args["selector"] = ev.element
                else:
                    x, y = self._norm(*ev.points[0])
                    args |= {"x_norm": x, "y_norm": y}
                steps.append({"id": f"{ev.kind}_{i}", "action": ev.kind, "args": args, "description": f"{what} {ev.kind}"})
            elif ev.kind == "swipe":
                (x0, y0), (x1, y1) = ev.points
                steps.append(
                    {
                        "id": f"swipe_{i}",
                        "action": "swipe",
                        "args": {"start": self._norm(x0, y0), "end": self._norm(x1, y1), "hold_end": ev.hold_end, "reason": f"시연 {i}: 스와이프"},
                        "description": "스와이프",
                    }
                )
            elif ev.kind == "key":
                steps.append({"id": f"key_{i}", "action": "system_action", "args": {"action": ev.key, "reason": f"시연 {i}: {ev.key}"}})
            elif ev.kind == "text":
                n_text += 1
                key = f"text_{n_text}"
                inputs[key] = {"type": "string", "required": True, "description": f"{ev.label or '입력란'}에 넣을 글자"}
                args = {"text": "{{inputs." + key + "}}", "clear": True}
                if ev.element:
                    args["selector"] = ev.element
                steps.append({"id": f"type_{i}", "action": "type_text", "args": args, "description": f"{ev.label or '입력란'} 입력"})
            if len(steps) > first:
                steps[-1]["_pkg"] = ev.package
                steps[-1]["_next_pkg"] = self.events[i].package if i < len(self.events) else ""
        self._add_expectations(steps)
        doc: dict[str, Any] = {
            "name": name,
            "version": "1.0.0",
            "description": description or f"사람 시연 녹화로 만든 스킬 ({len(self.events)}개 동작)",
            "tags": ["recorded"],
        }
        if inputs:
            doc["inputs"] = inputs
        if packages:
            doc["requires"] = {"packages": packages}
        doc["steps"] = steps
        return doc


def safe_skill_name(raw: str) -> str:
    slug = re.sub(r"[^a-z0-9_]+", "_", raw.strip().lower()).strip("_") or "task"
    return f"recorded.{slug}"


def dump(doc: dict[str, Any]) -> str:
    return yaml.safe_dump(doc, allow_unicode=True, sort_keys=False)
