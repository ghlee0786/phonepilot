"""Image utilities: change detection, Set-of-Marks overlay, masking, encoding (PRD 9.4, 9.5, 12.4)."""

from __future__ import annotations

import base64
import io
from collections.abc import Iterable

import numpy as np
from PIL import Image, ImageDraw, ImageFont

SOM_COLORS = [(230, 57, 70), (29, 120, 211), (42, 157, 143), (244, 162, 97), (131, 56, 236), (233, 196, 106)]


def dhash(rgb: np.ndarray, size: tuple[int, int] = (64, 128)) -> np.ndarray:
    """Perceptual hash: horizontal + vertical difference bits and an average-hash on a coarse grid.

    Difference bits alone miss changes between flat colour regions; the average bits catch them.
    """
    img = Image.fromarray(rgb).convert("L").resize((size[0] + 1, size[1] + 1), Image.Resampling.BILINEAR)
    a = np.asarray(img, dtype=np.int16)
    horiz = a[:-1, 1:] > a[:-1, :-1]
    vert = a[1:, :-1] > a[:-1, :-1]
    coarse = np.asarray(Image.fromarray(rgb).convert("L").resize((16, 32), Image.Resampling.BOX), dtype=np.int16)
    avg = coarse > 127
    return np.concatenate([horiz.ravel(), vert.ravel(), np.repeat(avg.ravel(), 16)])


def hash_distance(a: np.ndarray, b: np.ndarray) -> float:
    """Fraction of differing bits (0 = identical)."""
    if a.shape != b.shape:
        return 1.0
    return float(np.count_nonzero(a != b)) / a.size


def screen_hash_hex(rgb: np.ndarray) -> str:
    bits = dhash(rgb, (16, 16)).flatten()
    value = 0
    for bit in bits:
        value = (value << 1) | int(bit)
    width = (len(bits) + 3) // 4
    return f"{value:0{width}x}"


def mean_luma(rgb: np.ndarray) -> float:
    small = rgb[:: max(rgb.shape[0] // 64, 1), :: max(rgb.shape[1] // 64, 1)]
    return float(small.mean())


def looks_black(rgb: np.ndarray, threshold: float = 2.0) -> bool:
    return mean_luma(rgb) < threshold


def to_image(rgb: np.ndarray) -> Image.Image:
    return Image.fromarray(rgb)


def downscale(img: Image.Image, max_edge: int) -> Image.Image:
    w, h = img.size
    s = max_edge / max(w, h)
    if s >= 1:
        return img
    return img.resize((max(int(w * s), 1), max(int(h * s), 1)), Image.Resampling.LANCZOS)


def mask_boxes(img: Image.Image, boxes: Iterable[tuple[int, int, int, int]], src_size: tuple[int, int]) -> Image.Image:
    """Cover boxes (given in src_size pixel space) with opaque rectangles."""
    out = img.copy()
    draw = ImageDraw.Draw(out)
    sx = out.size[0] / max(src_size[0], 1)
    sy = out.size[1] / max(src_size[1], 1)
    for x0, y0, x1, y1 in boxes:
        draw.rectangle([x0 * sx - 2, y0 * sy - 2, x1 * sx + 2, y1 * sy + 2], fill=(40, 40, 40))
    return out


def _font(size: int) -> ImageFont.ImageFont | ImageFont.FreeTypeFont:
    for name in ("arialbd.ttf", "arial.ttf", "DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def render_som(img: Image.Image, marks: Iterable[tuple[int, tuple[int, int, int, int]]], src_size: tuple[int, int]) -> Image.Image:
    """Draw numbered boxes. `marks` = (id, bounds in src_size pixel space)."""
    out = img.convert("RGB").copy()
    draw = ImageDraw.Draw(out)
    sx = out.size[0] / max(src_size[0], 1)
    sy = out.size[1] / max(src_size[1], 1)
    font = _font(max(12, int(out.size[0] / 45)))
    placed: list[tuple[float, float, float, float]] = []
    for mid, (x0, y0, x1, y1) in marks:
        color = SOM_COLORS[mid % len(SOM_COLORS)]
        box = (x0 * sx, y0 * sy, x1 * sx, y1 * sy)
        draw.rectangle(box, outline=color, width=2)
        label = str(mid)
        tb = draw.textbbox((0, 0), label, font=font)
        tw, th = tb[2] - tb[0] + 6, tb[3] - tb[1] + 4
        lx, ly = box[0], box[1] - th
        if ly < 0:
            ly = box[1]
        # push label sideways while it collides with an earlier one
        for _ in range(10):
            if not any(lx < px1 and lx + tw > px0 and ly < py1 and ly + th > py0 for px0, py0, px1, py1 in placed):
                break
            lx += tw
        lx = min(lx, out.size[0] - tw)
        placed.append((lx, ly, lx + tw, ly + th))
        draw.rectangle((lx, ly, lx + tw, ly + th), fill=color)
        draw.text((lx + 3, ly + 1), label, fill=(255, 255, 255), font=font)
    return out


def encode(img: Image.Image, fmt: str = "jpeg", quality: int = 80) -> bytes:
    buf = io.BytesIO()
    if fmt.lower() in {"jpg", "jpeg"}:
        img.convert("RGB").save(buf, "JPEG", quality=quality, optimize=True)
    else:
        img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def crop_norm(img: Image.Image, bounds_norm: tuple[int, int, int, int]) -> Image.Image:
    w, h = img.size
    x0, y0, x1, y1 = bounds_norm
    box = (int(x0 / 1000 * w), int(y0 / 1000 * h), max(int(x1 / 1000 * w), 1), max(int(y1 / 1000 * h), 1))
    return img.crop(box)


class StabilityTracker:
    """Decide when the screen stopped changing (PRD 9.5)."""

    def __init__(self, threshold: float = 0.02, needed: int = 3) -> None:
        self.threshold = threshold
        self.needed = needed
        self._last: np.ndarray | None = None
        self._stable = 0

    def feed(self, rgb: np.ndarray) -> bool:
        h = dhash(rgb)
        if self._last is not None and hash_distance(h, self._last) <= self.threshold:
            self._stable += 1
        else:
            self._stable = 0
        self._last = h
        return self._stable >= self.needed - 1
