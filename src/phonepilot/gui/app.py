"""GUI entry point: QApplication + MainWindow on top of a CoreRuntime (PRD 5.3)."""

from __future__ import annotations

import logging
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QGuiApplication, QIcon
from PySide6.QtWidgets import QApplication

from phonepilot.gui.hotkeys import AltSpaceFilter, GlobalHotkeys
from phonepilot.gui.main_window import MainWindow
from phonepilot.gui.theme import apply_theme
from phonepilot.issue_report import describe_exception

if TYPE_CHECKING:
    from phonepilot.runtime import CoreRuntime

log = logging.getLogger(__name__)

APP_ICON = Path(__file__).parent / "resources" / "app.ico"  # built by assets/icon-candidates/make_app_icon.py
UPDATE_CHECK_DELAY_MS = 5000
ERROR_ALREADY_EXISTS = 183
SW_RESTORE = 9


def ensure_app() -> QApplication:
    app = QApplication.instance()
    if isinstance(app, QApplication):
        return app
    # FR-MIR-042: crisp rendering at 125 %/150 % scaling
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    if sys.platform == "win32":
        # own taskbar identity; without it Windows groups the window under python.exe and shows its icon
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("PhonePilot.App")
    app = QApplication(sys.argv[:1])
    app.setApplicationName("PhonePilot")
    app.setOrganizationName("PhonePilot")
    app.setWindowIcon(QIcon(str(APP_ICON)))
    apply_theme(app)
    return app


_instance_mutex: int | None = None  # kept open for the life of the process


def acquire_single_instance() -> bool:
    """Windows: False when PhonePilot already runs (its window is brought to the front instead).

    A second copy would fight the first over the phone, the adb server and the MCP port.
    """
    global _instance_mutex
    if sys.platform != "win32":
        return True
    import ctypes

    kernel32, user32 = ctypes.windll.kernel32, ctypes.windll.user32
    _instance_mutex = kernel32.CreateMutexW(None, False, "Local\\PhonePilot.SingleInstance")
    if kernel32.GetLastError() != ERROR_ALREADY_EXISTS:
        return True
    hwnd = user32.FindWindowW(None, "PhonePilot")
    if hwnd:
        user32.ShowWindow(hwnd, SW_RESTORE)
        user32.SetForegroundWindow(hwnd)
    return False


def install_crash_reporter(window: MainWindow) -> Callable[[], None]:
    """Unexpected exceptions on any thread offer a pre-filled GitHub issue instead of vanishing. Returns a restore()."""
    prev_hook, prev_thread_hook = sys.excepthook, threading.excepthook

    def report(exc: BaseException) -> None:
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            return
        summary, tb = describe_exception(exc)
        log.error("unhandled exception: %s", summary)
        window.bridge.post(lambda: window.report_problem(summary, tb, crashed=True))

    def hook(exc_type: type[BaseException], exc: BaseException, tb: TracebackType | None) -> None:
        report(exc)
        prev_hook(exc_type, exc, tb)

    def thread_hook(args: threading.ExceptHookArgs) -> None:
        if args.exc_value is not None:
            report(args.exc_value)
        prev_thread_hook(args)

    sys.excepthook = hook
    threading.excepthook = thread_hook

    def restore() -> None:
        sys.excepthook = prev_hook
        threading.excepthook = prev_thread_hook

    return restore


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
    restore_hooks = install_crash_reporter(window)
    QTimer.singleShot(UPDATE_CHECK_DELAY_MS, window.check_for_update)  # after startup settles; throttled to once a day
    app.aboutToQuit.connect(window.shutdown_ui)
    try:
        rc = app.exec()
    finally:
        restore_hooks()
        hotkeys.unregister()
        alt_space.uninstall()
        try:
            runtime.call_sync(runtime.shutdown(), timeout=15)
        except Exception as e:  # noqa: BLE001
            log.warning("runtime shutdown failed: %s", e)
        runtime.stop_loop()
    return int(rc)
