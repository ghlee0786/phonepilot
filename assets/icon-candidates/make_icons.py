"""Write 10 PhonePilot app-icon concepts as SVG and render a comparison sheet (256 / 32 / 16 px).

usage: python make_icons.py <out_dir>
"""

import sys
from pathlib import Path

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QGuiApplication, QImage, QPainter
from PySide6.QtSvg import QSvgRenderer

BG = """<defs><linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">
<stop offset="0" stop-color="{a}"/><stop offset="1" stop-color="{b}"/></linearGradient></defs>
<rect x="16" y="16" width="480" height="480" rx="112" fill="url(#bg)"/>"""
NAVY = ("#1e3a8a", "#0b1120")
BLUE = ("#3b82f6", "#1d4ed8")
DARK = ("#1e293b", "#0b1120")

# phone body centred at (256,256): 180x300
PHONE = '<rect x="166" y="106" width="180" height="300" rx="34" fill="none" stroke="{s}" stroke-width="22"/>'
PHONE_FILLED = '<rect x="166" y="106" width="180" height="300" rx="34" fill="{f}"/>'

CONCEPTS = [
    ("01 포인터", "폰 위의 마우스 포인터: PC에서 폰을 조종", NAVY,
     PHONE.format(s="#e2e8f0") + '<path d="M262 214 L262 352 L296 318 L322 372 L346 360 L320 308 L368 304 Z" fill="#3b82f6" stroke="#0b1120" stroke-width="10" stroke-linejoin="round"/>'),
    ("02 조종간", "비행기 조종간(요크): 'Pilot'을 직접 표현", BLUE,
     PHONE_FILLED.format(f="#0b1120") + '<path d="M196 236 h40 a20 20 0 0 1 20 -20 a20 20 0 0 1 20 20 h40 v34 a30 30 0 0 1 -30 30 h-60 a30 30 0 0 1 -30 -30 z" fill="none" stroke="#e2e8f0" stroke-width="18" stroke-linejoin="round"/><path d="M256 300 v56" stroke="#e2e8f0" stroke-width="18" stroke-linecap="round"/>'),
    ("03 종이비행기", "폰에서 날아오르는 종이비행기: 자동 조종", NAVY,
     PHONE.format(s="#64748b") + '<path d="M150 300 L396 170 L330 380 L270 316 Z" fill="#e2e8f0"/><path d="M270 316 L396 170 L248 300 Z" fill="#93c5fd"/><path d="M270 316 L282 372 L304 340 Z" fill="#3b82f6"/>'),
    ("04 미러링", "겹친 두 화면: 폰 화면을 PC로 미러링", DARK,
     '<rect x="126" y="126" width="170" height="270" rx="30" fill="none" stroke="#64748b" stroke-width="20"/><rect x="216" y="116" width="170" height="280" rx="30" fill="#3b82f6"/><rect x="246" y="156" width="110" height="170" rx="10" fill="#0b1120" opacity="0.35"/>'),
    ("05 AI 스파클", "폰 화면 속 반짝임: AI 에이전트", NAVY,
     PHONE.format(s="#e2e8f0") + '<path d="M256 186 C264 236 276 248 326 256 C276 264 264 276 256 326 C248 276 236 264 186 256 C236 248 248 236 256 186 Z" fill="#60a5fa"/><path d="M314 176 C317 192 320 195 336 198 C320 201 317 204 314 220 C311 204 308 201 292 198 C308 195 311 192 314 176 Z" fill="#e2e8f0"/>'),
    ("06 방패", "방패 안의 폰: 비밀값 격리·사람 승인 강조", DARK,
     '<path d="M256 86 L396 136 V256 C396 346 336 404 256 430 C176 404 116 346 116 256 V136 Z" fill="#3b82f6"/><rect x="211" y="176" width="90" height="150" rx="18" fill="none" stroke="#e2e8f0" stroke-width="16"/><path d="M244 300 h24" stroke="#e2e8f0" stroke-width="12" stroke-linecap="round"/>'),
    ("07 P 모노그램", "폰 모양으로 만든 P + 신호 호", BLUE,
     '<path d="M186 410 V126 h96 a76 76 0 0 1 0 152 h-96" fill="none" stroke="#ffffff" stroke-width="40" stroke-linejoin="round" stroke-linecap="round"/><path d="M350 330 a60 60 0 0 1 0 80" fill="none" stroke="#bfdbfe" stroke-width="18" stroke-linecap="round"/><path d="M386 300 a100 100 0 0 1 0 140" fill="none" stroke="#bfdbfe" stroke-width="18" stroke-linecap="round" opacity="0.6"/>'),
    ("08 터치 파동", "손가락 터치의 동심원: 사람 같은 제스처", NAVY,
     PHONE.format(s="#e2e8f0") + '<circle cx="256" cy="270" r="22" fill="#3b82f6"/><circle cx="256" cy="270" r="50" fill="none" stroke="#60a5fa" stroke-width="12"/><circle cx="256" cy="270" r="78" fill="none" stroke="#60a5fa" stroke-width="8" opacity="0.5"/>'),
    ("09 나침반", "폰 속 나침반 바늘: 길을 찾아가는 조종사", DARK,
     PHONE.format(s="#e2e8f0") + '<path d="M256 170 L286 256 L256 342 L226 256 Z" fill="#334155"/><path d="M256 170 L286 256 L226 256 Z" fill="#ef4444"/><circle cx="256" cy="256" r="12" fill="#e2e8f0"/>'),
    ("10 봇 + 정지", "봇 얼굴과 정지 버튼: AI는 일하고 사람이 멈춘다", NAVY,
     '<rect x="136" y="166" width="240" height="190" rx="52" fill="#3b82f6"/><path d="M256 166 v-46" stroke="#3b82f6" stroke-width="18" stroke-linecap="round"/><circle cx="256" cy="110" r="16" fill="#60a5fa"/><circle cx="210" cy="250" r="20" fill="#0b1120"/><circle cx="302" cy="250" r="20" fill="#0b1120"/><rect x="316" y="316" width="96" height="96" rx="24" fill="#ef4444" stroke="#0b1120" stroke-width="12"/><rect x="346" y="346" width="36" height="36" rx="6" fill="#ffffff"/>'),
]


def svg(colors: tuple[str, str], body: str) -> str:
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">{BG.format(a=colors[0], b=colors[1])}{body}</svg>'


def render(data: str, size: int) -> QImage:
    img = QImage(size, size, QImage.Format.Format_ARGB32)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    QSvgRenderer(QByteArray(data.encode())).render(p, QRectF(0, 0, size, size))
    p.end()
    return img


def main() -> None:
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    app = QGuiApplication([])
    cols, cell_w, cell_h = 5, 300, 400
    sheet = QImage(cols * cell_w, 2 * cell_h, QImage.Format.Format_ARGB32)
    sheet.fill(QColor("#e5e7eb"))
    p = QPainter(sheet)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    title = QFont(["Segoe UI", "Malgun Gothic"], 13, QFont.Weight.Bold)
    small = QFont(["Segoe UI", "Malgun Gothic"], 9)
    for i, (name, desc, colors, body) in enumerate(CONCEPTS):
        data = svg(colors, body)
        slug = f"icon_{i + 1:02d}"
        (out / f"{slug}.svg").write_text(data, encoding="utf-8")
        x, y = (i % cols) * cell_w, (i // cols) * cell_h
        p.drawImage(x + 22, y + 16, render(data, 256))
        # taskbar sizes on light and dark strips
        p.fillRect(x + 22, y + 284, 256, 44, QColor("#0b1120"))
        p.drawImage(x + 40, y + 290, render(data, 32))
        p.drawImage(x + 86, y + 298, render(data, 16))
        p.drawImage(x + 160, y + 290, render(data, 32))
        p.setPen(QColor("#111827"))
        p.setFont(title)
        p.drawText(QRectF(x + 22, y + 334, 256, 26), Qt.AlignmentFlag.AlignLeft, name)
        p.setFont(small)
        p.setPen(QColor("#374151"))
        p.drawText(QRectF(x + 22, y + 360, 256, 36), Qt.AlignmentFlag.AlignLeft | Qt.TextFlag.TextWordWrap, desc)
    p.end()
    sheet.save(str(out / "icon_candidates.png"))
    print(out / "icon_candidates.png")
    del app


main()
