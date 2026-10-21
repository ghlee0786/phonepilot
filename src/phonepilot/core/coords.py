"""Window <-> device coordinate mapping with aspect-ratio letterboxing (PRD FR-MIR-030, FR-MIR-044)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FitRect:
    x: float
    y: float
    w: float
    h: float
    scale: float


def fit_rect(container_w: float, container_h: float, frame_w: int, frame_h: int) -> FitRect:
    """Largest rectangle with the frame's aspect ratio centered in the container."""
    if frame_w <= 0 or frame_h <= 0 or container_w <= 0 or container_h <= 0:
        return FitRect(0, 0, 0, 0, 0)
    s = min(container_w / frame_w, container_h / frame_h)
    w = frame_w * s
    h = frame_h * s
    return FitRect((container_w - w) / 2, (container_h - h) / 2, w, h, s)


def window_to_frame(
    wx: float, wy: float, container_w: float, container_h: float, frame_w: int, frame_h: int, *, clamp: bool = False
) -> tuple[int, int] | None:
    """Map a point in the content area to frame pixels. Letterbox points give None unless clamp=True."""
    r = fit_rect(container_w, container_h, frame_w, frame_h)
    if r.scale == 0:
        return None
    dx = (wx - r.x) / r.scale
    dy = (wy - r.y) / r.scale
    inside = 0 <= dx < frame_w and 0 <= dy < frame_h
    if not inside:
        if not clamp:
            return None
        dx = min(max(dx, 0), frame_w - 1)
        dy = min(max(dy, 0), frame_h - 1)
    return int(round(min(dx, frame_w - 1))), int(round(min(dy, frame_h - 1)))


def norm_to_px(x_norm: float, y_norm: float, width: int, height: int) -> tuple[int, int]:
    """0..1000 normalized coordinates -> pixels (PRD 9.3 bounds_norm)."""
    x = round(min(max(x_norm, 0), 1000) / 1000 * (width - 1))
    y = round(min(max(y_norm, 0), 1000) / 1000 * (height - 1))
    return int(x), int(y)


def px_to_norm(x: float, y: float, width: int, height: int) -> tuple[int, int]:
    return int(round(x / max(width - 1, 1) * 1000)), int(round(y / max(height - 1, 1) * 1000))


def scale_point(x: float, y: float, src_w: int, src_h: int, dst_w: int, dst_h: int) -> tuple[int, int]:
    """Rescale a point between two resolutions of the same screen (e.g. UI dump px -> video frame px)."""
    return int(round(x * dst_w / src_w)), int(round(y * dst_h / src_h))
