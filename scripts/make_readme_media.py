"""Render the README's screenshots and demo GIF from the real GUI on the demo device (no phone needed).

usage: python scripts/make_readme_media.py [out_dir]   (default docs/images)
  main.png       mirror window + timeline
  approval.png   human approval dialog
  device.png     device tab (MCP, calibration, task recording)
  demo.gif       the AI drives the demo phone through MCP-equivalent tool calls
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from PIL import Image  # noqa: E402
from PySide6.QtCore import QBuffer, QIODevice, QTimer  # noqa: E402
from PySide6.QtGui import QImage  # noqa: E402

from phonepilot.gui.app import ensure_app  # noqa: E402
from phonepilot.gui.dialogs import ApprovalDialog  # noqa: E402
from phonepilot.gui.main_window import MainWindow  # noqa: E402
from phonepilot.runtime import CoreRuntime  # noqa: E402
from phonepilot.security.approvals import ApprovalRequest  # noqa: E402
from phonepilot.security.policy import Level, PolicyDecision  # noqa: E402

GIF_WIDTH = 960
FRAME_MS = 120


def wait(app, ms: int) -> None:  # noqa: ANN001
    t = QTimer()
    t.setSingleShot(True)
    t.start(ms)
    while t.isActive():
        app.processEvents()


def to_pil(img: QImage) -> Image.Image:
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG")
    import io

    return Image.open(io.BytesIO(bytes(buf.data()))).convert("RGB")


def save_png(img: QImage, path: Path) -> None:
    to_pil(img).save(path, optimize=True)
    print("  ", path.name)


def record(app, win, rt, actions) -> list[Image.Image]:  # noqa: ANN001
    """Run tool calls one by one on the core loop while grabbing the window."""
    frames: list[Image.Image] = []

    def grab() -> None:
        im = to_pil(win.grab().toImage())
        frames.append(im.resize((GIF_WIDTH, round(im.height * GIF_WIDTH / im.width)), Image.LANCZOS))

    for _ in range(6):
        grab()
        wait(app, FRAME_MS)
    for make in actions:
        fut = rt.call(make())
        while not fut.done():
            grab()
            wait(app, FRAME_MS)
        fut.result()
        for _ in range(5):  # let the viewer read the result
            grab()
            wait(app, FRAME_MS)
    return frames


def main() -> None:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "docs" / "images"
    out.mkdir(parents=True, exist_ok=True)
    app = ensure_app()
    rt = CoreRuntime(demo=True)
    rt.start()
    rt.call_sync(rt.connect())
    win = MainWindow(rt)
    win.resize(1000, 860)
    win.show()
    win.attach_stack()
    wait(app, 1500)
    win.resize(1000, 860)
    wait(app, 400)
    win.tabs.setCurrentIndex(0)

    tools = rt.stack.tools
    actor = "claude-code"
    actions = [
        lambda: tools.observe(actor=actor),
        lambda: tools.tap(actor=actor, selector={"text": "YouTube"}, reason="YouTube 열기"),
        lambda: tools.tap(actor=actor, selector={"text": "나"}, reason="내 페이지"),
        lambda: tools.tap(actor=actor, selector={"text": "채널 만들기"}, reason="채널 만들기 화면"),
        lambda: tools.type_text(actor=actor, selector={"resource_id": "com.google.android.youtube:id/name"}, text="PhonePilot 데모", reason="채널 이름"),
        lambda: tools.system_action(actor=actor, action="home", reason="홈으로"),
    ]
    t0 = time.monotonic()
    frames = record(app, win, rt, actions)
    frames[0].save(
        out / "demo.gif", save_all=True, append_images=frames[1:], duration=FRAME_MS, loop=0, optimize=True
    )
    print("   demo.gif", len(frames), "frames", f"{time.monotonic() - t0:.1f}s")

    save_png(win.grab().toImage(), out / "main.png")

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
    save_png(dlg.grab().toImage(), out / "approval.png")
    dlg.close_silently()

    win.tabs.setCurrentIndex(3)
    wait(app, 300)
    scroll = win.tabs.widget(3)
    scroll.ensureWidgetVisible(win.device_panel.record_start_button, 0, 40)
    wait(app, 300)
    save_png(win.grab().toImage(), out / "device.png")  # calibration + task recording

    win.shutdown_ui()
    win.close()
    rt.call_sync(rt.shutdown())
    rt.stop_loop()


if __name__ == "__main__":
    main()
