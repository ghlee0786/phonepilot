"""Render the installer's images from the real GUI on the demo device (no phone, no MCP server).

usage: python installer/make_installer_assets.py <out_dir>
Writes 24-bit BMPs (Inno Setup's native format):
  feature_1..4.bmp  feature tour pages (960x540)
  wizard.bmp        left banner of the welcome/finish pages
  wizard_small.bmp  top-right icon of the inner pages
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# The offscreen platform (CI) has an empty font database on Windows unless pointed at the system fonts:
# every Korean string then renders as tofu boxes in the wizard images.
if os.environ.get("QT_QPA_PLATFORM") == "offscreen" and not os.environ.get("QT_QPA_FONTDIR"):
    os.environ["QT_QPA_FONTDIR"] = str(Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts")

from PySide6.QtCore import QRectF, Qt, QTimer  # noqa: E402
from PySide6.QtGui import QColor, QFont, QFontDatabase, QImage, QLinearGradient, QPainter  # noqa: E402
from PySide6.QtSvg import QSvgRenderer  # noqa: E402

from phonepilot.agent.timeline import TimelineEvent  # noqa: E402
from phonepilot.gui.app import ensure_app  # noqa: E402
from phonepilot.gui.dialogs import ApprovalDialog  # noqa: E402
from phonepilot.gui.main_window import MainWindow  # noqa: E402
from phonepilot.gui.theme import COLORS, FONT_FAMILIES  # noqa: E402
from phonepilot.runtime import CoreRuntime  # noqa: E402
from phonepilot.security.approvals import ApprovalRequest  # noqa: E402
from phonepilot.security.policy import Level, PolicyDecision  # noqa: E402

FEATURE = (960, 540)
ICON_SVG = ROOT / "src" / "phonepilot" / "gui" / "resources" / "app.svg"


def wait(app, ms: int) -> None:  # noqa: ANN001
    t = QTimer()
    t.setSingleShot(True)
    t.start(ms)
    while t.isActive():
        app.processEvents()


def compose(shot: QImage, size: tuple[int, int] = FEATURE) -> QImage:
    """Center a screenshot on the dark brand background with a margin and rounded frame."""
    w, h = size
    out = QImage(w, h, QImage.Format.Format_RGB32)
    p = QPainter(out)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    g = QLinearGradient(0, 0, w, h)
    g.setColorAt(0, QColor("#1e3a8a"))
    g.setColorAt(1, QColor(COLORS["bg"]))
    p.fillRect(out.rect(), g)
    margin = 28
    scale = min((w - 2 * margin) / shot.width(), (h - 2 * margin) / shot.height())
    sw, sh = shot.width() * scale, shot.height() * scale
    target = QRectF((w - sw) / 2, (h - sh) / 2, sw, sh)
    p.fillRect(target.adjusted(-1, -1, 1, 1), QColor(COLORS["border_strong"]))
    p.drawImage(target, shot)
    p.end()
    return out


def banner(w: int, h: int, small: bool = False) -> QImage:
    out = QImage(w, h, QImage.Format.Format_RGB32)
    p = QPainter(out)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    if small:
        out.fill(QColor("#ffffff"))  # Inno draws the small image on the light header
        QSvgRenderer(str(ICON_SVG)).render(p, QRectF(0, 0, w, h))
        p.end()
        return out
    g = QLinearGradient(0, 0, 0, h)
    g.setColorAt(0, QColor("#1e3a8a"))
    g.setColorAt(1, QColor(COLORS["bg"]))
    p.fillRect(out.rect(), g)
    icon = w * 0.62
    QSvgRenderer(str(ICON_SVG)).render(p, QRectF((w - icon) / 2, h * 0.22, icon, icon))
    p.setPen(QColor("#ffffff"))
    f = QFont(FONT_FAMILIES)
    f.setPixelSize(int(w * 0.13))
    f.setBold(True)
    p.setFont(f)
    p.drawText(QRectF(0, h * 0.22 + icon + 16, w, 40), Qt.AlignmentFlag.AlignHCenter, "PhonePilot")
    f.setPixelSize(int(w * 0.065))
    f.setBold(False)
    p.setFont(f)
    p.setPen(QColor("#bfdbfe"))
    p.drawText(QRectF(10, h * 0.22 + icon + 60, w - 20, 60), Qt.AlignmentFlag.AlignHCenter | Qt.TextFlag.TextWordWrap, "Android 미러링 ·\nAI 제어")
    p.end()
    return out


def save(img: QImage, path: Path) -> None:
    img.convertToFormat(QImage.Format.Format_RGB888).save(str(path), "BMP")
    print("  ", path.name)


def main() -> None:
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    app = ensure_app()
    if "Malgun Gothic" not in QFontDatabase.families():
        sys.exit("Korean font 'Malgun Gothic' not found: the wizard images would show tofu boxes (set QT_QPA_FONTDIR)")
    rt = CoreRuntime(demo=True)
    rt.start()
    rt.call_sync(rt.connect())
    win = MainWindow(rt)
    win.resize(1180, 900)
    win.show()
    win.attach_stack()
    for actor, tool, target, level, result in [
        ("claude-code", "observe", "", "", "ok"),
        ("claude-code", "tap", "YouTube", "ALLOW", "ok"),
        ("claude-code", "scroll", "", "ALLOW", "ok"),
        ("human", "tap", "(312,880)", "", "ok"),
        ("claude-code", "tap", "업로드", "CONFIRM", "approved"),
        ("workflow:youtube.channel_launch_checklist", "observe", "", "", "ok"),
    ]:
        win.timeline_panel.add_event(TimelineEvent(actor, tool, target, level, result, reason="데모"))
    wait(app, 1500)
    win.resize(1180, 900)  # the first frame resizes the window to the phone aspect; keep a wide layout for the tour
    wait(app, 400)

    win.tabs.setCurrentIndex(0)
    wait(app, 300)
    save(compose(win.grab().toImage()), out / "feature_1.bmp")  # mirroring + timeline

    win.tabs.setCurrentIndex(1)
    wait(app, 300)
    save(compose(win.grab().toImage()), out / "feature_2.bmp")  # workflows / skills

    req = ApprovalRequest(
        title="AI가 다음 행동을 요청합니다",
        summary="YouTube에서 '업로드' 버튼 탭",
        decision=PolicyDecision(Level.CONFIRM, "publish", ["업로드"]),
        client="claude-code",
        reason="영상 게시",
    )
    dlg = ApprovalDialog(req, win)
    dlg.show()
    wait(app, 500)
    save(compose(dlg.grab().toImage()), out / "feature_3.bmp")  # human approval
    dlg.close_silently()

    win.tabs.setCurrentIndex(3)
    wait(app, 300)
    save(compose(win.grab().toImage()), out / "feature_4.bmp")  # device, MCP, wireless, calibration

    save(banner(240, 459), out / "wizard.bmp")
    save(banner(58, 58, small=True), out / "wizard_small.bmp")

    win.shutdown_ui()
    win.close()
    rt.call_sync(rt.shutdown())
    rt.stop_loop()


if __name__ == "__main__":
    main()
