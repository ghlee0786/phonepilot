"""GUI entry point: QApplication + MainWindow on top of a CoreRuntime (PRD 5.3)."""

from __future__ import annotations

import logging
import sys
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication

from phonepilot.gui.hotkeys import AltSpaceFilter, GlobalHotkeys
from phonepilot.gui.main_window import MainWindow

if TYPE_CHECKING:
    from phonepilot.runtime import CoreRuntime

log = logging.getLogger(__name__)


def ensure_app() -> QApplication:
    app = QApplication.instance()
    if isinstance(app, QApplication):
        return app
    # FR-MIR-042: crisp rendering at 125 %/150 % scaling
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv[:1])
    app.setApplicationName("PhonePilot")
    app.setOrganizationName("PhonePilot")
    return app


def run_gui(runtime: CoreRuntime, *, connect: bool = True) -> int:
    """Run the desktop GUI until the main window closes. Shuts the runtime down afterwards."""
    app = ensure_app()
    runtime.start()
    window = MainWindow(runtime)

    hotkeys = GlobalHotkeys(window.trigger_kill)
    registered = hotkeys.register()
    window.set_hotkey_status(registered, hotkeys.failed)
    alt_space = AltSpaceFilter(window.toggle_ai_pause, lambda hwnd: hwnd != 0 and hwnd == int(window.winId()))
    alt_space.install()

    window.show()
    if connect:
        window.connect_device(start_mcp=True)
    app.aboutToQuit.connect(window.shutdown_ui)
    try:
        rc = app.exec()
    finally:
        hotkeys.unregister()
        alt_space.uninstall()
        try:
            runtime.call_sync(runtime.shutdown(), timeout=15)
        except Exception as e:  # noqa: BLE001
            log.warning("runtime shutdown failed: %s", e)
        runtime.stop_loop()
    return int(rc)
