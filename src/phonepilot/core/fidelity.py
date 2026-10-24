"""Gesture fidelity: how closely the AI's profile gestures match the user's own (FR-INP-035..039, FR-INP-047).

For every recorded human gesture the AI counterpart is synthesised with the same geometry
from the profile, and both are run through the same `calibration.measure`. Per metric the
report compares the human distribution (median, spread) with the AI values and scores how far
the AI median sits from the human median, in units of the human spread.

This is an offline measurement. It never changes the gestures that are sent to the device
(those stay deterministic, PRD 3.3).
"""

from __future__ import annotations

import statistics
from dataclasses import asdict, dataclass

from phonepilot.core import gesture as g
from phonepilot.core.calibration import GestureKind, GestureProfile, GestureSample, measure

HOLD_END_MS = 60  # same threshold as skills.recorder: below this the human lifted while moving

# minimum spread per metric so a very consistent human does not make every small gap a failure
MIN_SPREAD = {"duration_ms": 10.0, "speed_px_per_ms": 0.15, "arc": 0.01, "peak": 0.05, "settle_ms": 15.0}
METRICS = {
    GestureKind.TAP: ("duration_ms",),
    GestureKind.LONG_PRESS: ("duration_ms",),
    GestureKind.SWIPE: ("speed_px_per_ms", "arc", "peak", "settle_ms"),
}


@dataclass(frozen=True)
class MetricFidelity:
    kind: str
    metric: str
    n: int
    human_median: float
    human_p10: float
    human_p90: float
    human_stdev: float
    ai_median: float
    ai_stdev: float
    z: float  # |ai_median - human_median| / spread
    in_human_range: bool  # ai_median within [p10, p90] of the human samples
    score: float  # 0..100, 100 = AI median equals the human median


def _ai_counterpart(s: GestureSample, prof: GestureProfile) -> GestureSample | None:
    if s.kind is GestureKind.TAP:
        steps = g.tap(540, 1200, prof.tap_ms)
    elif s.kind is GestureKind.LONG_PRESS:
        steps = g.long_press(540, 1200, prof.long_press_ms)
    else:
        # same path as a replayed recorded swipe (ToolService.swipe, hold_end when the human rested before lifting)
        dist = max(int(round(s.distance_px)), 1)
        y0 = 400 + dist
        if s.settle_ms >= HOLD_END_MS:
            steps = g.precise_scroll(540, y0, 540, 400, prof.swipe_ms(dist), prof.settle_ms, prof.arc, ease=prof.ease)
        else:
            steps = g.swipe(540, y0, 540, 400, prof.swipe_ms(dist), "ease", prof.arc, ease=prof.ease)
    return measure([(st.t_ms, float(st.x), float(st.y)) for st in steps if st.pointer == 0])


def _quantile(values: list[float], q: float) -> float:
    vs = sorted(values)
    if len(vs) == 1:
        return vs[0]
    pos = q * (len(vs) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(vs) - 1)
    return vs[lo] + (vs[hi] - vs[lo]) * (pos - lo)


def _value(s: GestureSample, metric: str) -> float:
    return s.speed_px_per_ms if metric == "speed_px_per_ms" else float(getattr(s, metric))


def compare(samples: list[GestureSample], profile: GestureProfile) -> list[MetricFidelity]:
    """One row per (gesture kind, metric) that has human samples."""
    rows: list[MetricFidelity] = []
    for kind, metrics in METRICS.items():
        humans = [s for s in samples if s.kind is kind]
        pairs = [(h, a) for h in humans if (a := _ai_counterpart(h, profile)) is not None]
        if not pairs:
            continue
        for metric in metrics:
            hv = [_value(h, metric) for h, _ in pairs]
            av = [_value(a, metric) for _, a in pairs]
            h_med, a_med = statistics.median(hv), statistics.median(av)
            h_sd = statistics.stdev(hv) if len(hv) > 1 else 0.0
            p10, p90 = _quantile(hv, 0.1), _quantile(hv, 0.9)
            spread = max(h_sd, MIN_SPREAD[metric])
            z = abs(a_med - h_med) / spread
            rows.append(
                MetricFidelity(
                    kind=kind.value,
                    metric=metric,
                    n=len(pairs),
                    human_median=round(h_med, 3),
                    human_p10=round(p10, 3),
                    human_p90=round(p90, 3),
                    human_stdev=round(h_sd, 3),
                    ai_median=round(a_med, 3),
                    ai_stdev=round(statistics.stdev(av) if len(av) > 1 else 0.0, 3),
                    z=round(z, 2),
                    in_human_range=p10 - 1e-9 <= a_med <= p90 + 1e-9,
                    score=round(max(0.0, 100.0 * (1 - z / 3)), 1),
                )
            )
    return rows


def report(samples: list[GestureSample], profile: GestureProfile) -> dict:
    rows = compare(samples, profile)
    overall = round(statistics.mean(r.score for r in rows), 1) if rows else None
    return {"overall": overall, "rows": [asdict(r) for r in rows], "samples": len(samples)}


METRIC_LABELS = {
    "duration_ms": "누름 시간(ms)",
    "speed_px_per_ms": "속도(px/ms)",
    "arc": "휘어짐",
    "peak": "최고 속도 위치",
    "settle_ms": "멈춤 후 떼기(ms)",
}
KIND_LABELS = {"tap": "탭", "long_press": "길게 누르기", "swipe": "스와이프"}


def format_report(rep: dict) -> str:
    """Plain-text table for the GUI and CLI."""
    if not rep["rows"]:
        return "비교할 기록이 없습니다. 보정 중에 탭·길게 누르기·스와이프를 해 주세요."
    lines = [f"사람-AI 제스처 유사도: {rep['overall']}/100 (기록 {rep['samples']}개)", ""]
    for r in rep["rows"]:
        mark = "범위 안" if r["in_human_range"] else "범위 밖"
        lines.append(
            f"{KIND_LABELS[r['kind']]} · {METRIC_LABELS[r['metric']]}: 사람 {r['human_median']}"
            f" (p10~p90 {r['human_p10']}~{r['human_p90']}, 표준편차 {r['human_stdev']}) / AI {r['ai_median']}"
            f" (표준편차 {r['ai_stdev']}) → {r['score']}점, {mark}"
        )
    lines += ["", "AI 제스처는 결정적이라 같은 거리에서는 편차가 0입니다(사람 편차는 참고값)."]
    return "\n".join(lines)
