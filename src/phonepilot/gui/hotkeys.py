"""Global kill-switch hotkeys (PRD 13.5, FR-INP-045): Pause and Ctrl+Alt+End via RegisterHotKey.

The hotkeys are registered for the GUI thread (hWnd = NULL), so WM_HOTKEY arrives as a thread
message that Qt's Windows event dispatcher hands to native event filters. This avoids losing the
registration when Qt recreates the native window (e.g. toggling window flags).
Everything degrades to a no-op when not on Windows or when another app owns the key.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QAbstractNativeEventFilter, QCoreApplication

log = logging.getLogger(__name__)

WM_HOTKEY = 0x0312
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_NOREPEAT = 0x4000
VK_PAUSE = 0x13
VK_END = 0x23

HOTKEYS: list[tuple[int, int, int, str]] = [
    (0x5050, 0, VK_PAUSE, "Pause"),
    (0x5051, MOD_CONTROL | MOD_ALT, VK_END, "Ctrl+Alt+End"),
]


class GlobalHotkeys(QAbstractNativeEventFilter):
    def __init__(self, callback: Callable[[], None]) -> None:
        super().__init__()
        self.callback = callback
        self.registered: list[str] = []
        self.failed: list[str] = []
        self._ids: set[int] = set()
        self._installed = False

    @staticmethod
    def supported() -> bool:
        return sys.platform == "win32"

    def register(self) -> list[str]:
        """Register the hotkeys. Returns the names that succeeded; failures are in `self.failed`."""
        if not self.supported():
            self.failed = [name for *_, name in HOTKEYS]
            return []
        try:
            import ctypes

            user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        except Exception as e:  # noqa: BLE001
            log.warning("global hotkeys unavailable: %s", e)
            self.failed = [name for *_, name in HOTKEYS]
            return []
        for hk_id, mods, vk, name in HOTKEYS:
            try:
                ok = bool(user32.RegisterHotKey(None, hk_id, mods | MOD_NOREPEAT, vk))
            except Exception:  # noqa: BLE001
                ok = False
            if ok:
                self._ids.add(hk_id)
                self.registered.append(name)
            else:
                self.failed.append(name)
                log.warning("could not register global hotkey %s (in use by another app?)", name)
        app = QCoreApplication.instance()
        if self._ids and app is not None and not self._installed:
            app.installNativeEventFilter(self)
            self._installed = True
        return list(self.registered)

    def unregister(self) -> None:
        if self.supported() and self._ids:
            try:
                import ctypes

                for hk_id in self._ids:
                    ctypes.windll.user32.UnregisterHotKey(None, hk_id)  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                pass
        self._ids.clear()
        app = QCoreApplication.instance()
        if self._installed and app is not None:
            app.removeNativeEventFilter(self)
        self._installed = False

    def nativeEventFilter(self, eventType: Any, message: Any) -> tuple[bool, int]:  # noqa: N802
        try:
            et = eventType.data() if hasattr(eventType, "data") else bytes(eventType)
            if et != b"windows_generic_MSG":
                return False, 0
            from ctypes import wintypes

            msg = wintypes.MSG.from_address(int(message))
            if msg.message == WM_HOTKEY and int(msg.wParam) in self._ids:
                self.callback()
                return True, 0
        except Exception:  # noqa: BLE001 - never break the event loop
            log.debug("hotkey filter error", exc_info=True)
        return False, 0


WM_SYSKEYDOWN = 0x0104
VK_SPACE = 0x20


class AltSpaceFilter(QAbstractNativeEventFilter):
    """Windows opens the window's system menu on Alt+Space, which would swallow Mod+Space
    (AI pause/resume, PRD 18.7). Intercept it for our own top-level window only."""

    def __init__(self, callback: Callable[[], None], is_ours: Callable[[int], bool]) -> None:
        super().__init__()
        self.callback = callback
        self.is_ours = is_ours
        self._installed = False

    def install(self) -> bool:
        app = QCoreApplication.instance()
        if not GlobalHotkeys.supported() or app is None or self._installed:
            return False
        app.installNativeEventFilter(self)
        self._installed = True
        return True

    def uninstall(self) -> None:
        app = QCoreApplication.instance()
        if self._installed and app is not None:
            app.removeNativeEventFilter(self)
        self._installed = False

    def nativeEventFilter(self, eventType: Any, message: Any) -> tuple[bool, int]:  # noqa: N802
        try:
            et = eventType.data() if hasattr(eventType, "data") else bytes(eventType)
            if et != b"windows_generic_MSG":
                return False, 0
            from ctypes import wintypes

            msg = wintypes.MSG.from_address(int(message))
            if msg.message == WM_SYSKEYDOWN and int(msg.wParam) == VK_SPACE and self.is_ours(int(msg.hWnd or 0)):
                if not (int(msg.lParam) >> 30) & 1:  # ignore auto-repeat
                    self.callback()
                return True, 0
        except Exception:  # noqa: BLE001
            log.debug("alt+space filter error", exc_info=True)
        return False, 0
