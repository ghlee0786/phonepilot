"""Build the PhonePilot app icon (concept 05: AI sparkle inside a phone) into src/phonepilot/gui/resources/.

Two drawings: the detailed one for 40 px and up, a simplified silhouette (filled phone, one dark sparkle,
no corner margin) for 16-32 px where the outline and the light sparkle blur together. Outputs app.svg, app_small.svg, app.ico
(16-256 px, each size rendered from the right drawing) and app_256.png.

usage: python make_app_icon.py <repo_root>
"""

import io
import sys
from pathlib import Path

from PIL import Image
from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QRectF, Qt
from PySide6.QtGui import QGuiApplication, QImage, QPainter
from PySide6.QtSvg import QSvgRenderer

DEFS = """<defs>
<linearGradient id="bg" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#1e3a8a"/><stop offset="1" stop-color="#0b1120"/></linearGradient>
<linearGradient id="spark" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#bfdbfe"/><stop offset="1" stop-color="#3b82f6"/></linearGradient>
</defs>"""


def sparkle(cx: float, cy: float, r: float, waist: float = 0.16) -> str:
    """Four-point star with concave sides; `waist` sets how far the curves pull toward the centre."""
    w = r * waist
    return (
        f"M{cx} {cy - r} C{cx + w} {cy - w} {cx + w} {cy - w} {cx + r} {cy} "
        f"C{cx + w} {cy + w} {cx + w} {cy + w} {cx} {cy + r} "
        f"C{cx - w} {cy + w} {cx - w} {cy + w} {cx - r} {cy} "
        f"C{cx - w} {cy - w} {cx - w} {cy - w} {cx} {cy - r} Z"
    )


LARGE = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">{DEFS}
<rect x="16" y="16" width="480" height="480" rx="112" fill="url(#bg)"/>
<rect x="160" y="96" width="192" height="320" rx="38" fill="#0b1120" fill-opacity="0.45" stroke="#e2e8f0" stroke-width="24"/>
<path d="M232 380h48" stroke="#e2e8f0" stroke-width="14" stroke-linecap="round" opacity="0.55"/>
<path d="{sparkle(250, 252, 82)}" fill="url(#spark)"/>
<path d="{sparkle(310, 172, 26)}" fill="#e2e8f0"/>
</svg>"""

SMALL = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">{DEFS}
<rect x="0" y="0" width="512" height="512" rx="104" fill="url(#bg)"/>
<rect x="124" y="48" width="264" height="416" rx="60" fill="#e2e8f0"/>
<path d="{sparkle(256, 256, 116, 0.3)}" fill="#1d4ed8"/>
</svg>"""

SMALL_MAX = 32  # sizes up to this use the simplified drawing
ICO_SIZES = [16, 20, 24, 32, 40, 48, 64, 96, 128, 256]


def render(svg: str, size: int) -> Image.Image:
    img = QImage(size, size, QImage.Format.Format_ARGB32)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    QSvgRenderer(QByteArray(svg.encode())).render(p, QRectF(0, 0, size, size))
    p.end()
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG")
    return Image.open(io.BytesIO(bytes(buf.data()))).convert("RGBA")


def main() -> None:
    root = Path(sys.argv[1])
    out = root / "src" / "phonepilot" / "gui" / "resources"
    out.mkdir(parents=True, exist_ok=True)
    app = QGuiApplication([])
    (out / "app.svg").write_text(LARGE, encoding="utf-8")
    (out / "app_small.svg").write_text(SMALL, encoding="utf-8")
    frames = [render(SMALL if s <= SMALL_MAX else LARGE, s) for s in ICO_SIZES]
    frames[-1].save(out / "app.ico", format="ICO", sizes=[(s, s) for s in ICO_SIZES], append_images=frames[:-1])
    frames[-1].save(out / "app_256.png")

    # preview: every size on dark and light strips
    preview = Image.new("RGBA", (40 + sum(ICO_SIZES) + 12 * len(ICO_SIZES), 2 * 280), "#0b1120")
    preview.paste(Image.new("RGBA", (preview.width, 280), "#f1f5f9"), (0, 280))
    for band in (0, 280):
        x = 20
        for frame in frames:
            preview.alpha_composite(frame, (x, band + (280 - frame.height) // 2))
            x += frame.width + 12
    preview.save(root / "assets" / "icon-candidates" / "app_icon_final_preview.png")
    print("written:", *sorted(p.name for p in out.iterdir()))
    del app


main()
