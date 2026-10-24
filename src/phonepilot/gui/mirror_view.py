"""MirrorView: aspect-fit phone screen + mouse/keyboard -> InputArbiter (PRD 6.3, 7.1, 7.3, 18.3, 18.4).

Frames arrive on the decoder thread; the callback only stores the newest frame and pokes the
GUI thread once (latest frame wins). Conversion to QImage happens on the GUI thread, for the
newest frame only. All coordinates handed to the arbiter are display pixels.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

import numpy as np
from PySide6.QtCore import QPointF, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QDragEnterEvent,
    QDragLeaveEvent,
    QDropEvent,
    QFocusEvent,
    QFont,
    QGuiApplication,
    QImage,
    QInputMethodEvent,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPen,
    QWheelEvent,
)
from PySide6.QtWidgets import QWidget

from phonepilot.core.coords import FitRect, fit_rect
from phonepilot.core.framebus import Frame
from phonepilot.core.scrcpy import protocol as p
from phonepilot.gui.bridge import InputPump

log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from phonepilot.core.device import DeviceBackend
    from phonepilot.runtime import CoreRuntime

AI_COLOR = QColor("#2563eb")
PAUSED_COLOR = QColor("#eab308")
KILLED_COLOR = QColor("#dc2626")
FLASH_MS = 400
REVEAL_S = 30.0

# Non-text keys sent as keycodes in text mode (FR-INP-020)
KEYMAP: dict[int, int] = {
    Qt.Key.Key_Return: p.Keycode.ENTER,
    Qt.Key.Key_Enter: p.Keycode.ENTER,
    Qt.Key.Key_Backspace: p.Keycode.DEL,
    Qt.Key.Key_Delete: p.Keycode.FORWARD_DEL,
    Qt.Key.Key_Tab: p.Keycode.TAB,
    Qt.Key.Key_Left: p.Keycode.DPAD_LEFT,
    Qt.Key.Key_Right: p.Keycode.DPAD_RIGHT,
    Qt.Key.Key_Up: p.Keycode.DPAD_UP,
    Qt.Key.Key_Down: p.Keycode.DPAD_DOWN,
    Qt.Key.Key_Home: p.Keycode.MOVE_HOME,
    Qt.Key.Key_End: p.Keycode.MOVE_END,
    Qt.Key.Key_PageUp: p.Keycode.PAGE_UP,
    Qt.Key.Key_PageDown: p.Keycode.PAGE_DOWN,
    Qt.Key.Key_Escape: p.Keycode.ESCAPE,
}
_MODIFIER_KEYS = {Qt.Key.Key_Alt, Qt.Key.Key_Control, Qt.Key.Key_Shift, Qt.Key.Key_Meta, Qt.Key.Key_AltGr, Qt.Key.Key_CapsLock}
KEYCODE_0 = 7
KEYCODE_A = 29


def ctrl_keycode(key: int) -> int | None:
    """Android keycode for letters/digits so Ctrl combos (Ctrl+A, Ctrl+C, ...) pass through."""
    if Qt.Key.Key_A <= key <= Qt.Key.Key_Z:
        return KEYCODE_A + (key - Qt.Key.Key_A)
    if Qt.Key.Key_0 <= key <= Qt.Key.Key_9:
        return KEYCODE_0 + (key - Qt.Key.Key_0)
    return None


def is_printable_ascii(text: str) -> bool:
    return bool(text) and all(0x20 <= ord(ch) <= 0x7E for ch in text)


def frame_to_qimage(rgb: np.ndarray) -> QImage:
    arr = np.ascontiguousarray(rgb)
    h, w = arr.shape[:2]
    return QImage(arr.data, w, h, arr.strides[0], QImage.Format.Format_RGB888).copy()


class MirrorView(QWidget):
    frameReady = Signal()  # internal: decoder thread -> GUI thread
    frameSizeChanged = Signal(int, int)
    escapeInFullscreen = Signal()
    fullscreenToggleKey = Signal()
    filesDropped = Signal(list)  # local file/folder paths dropped on the phone screen (FR-INP-053)

    def __init__(self, runtime: CoreRuntime, pump: InputPump, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.runtime = runtime
        self.pump = pump
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_InputMethodEnabled, True)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.setMouseTracking(False)
        self.setMinimumSize(120, 160)
        self.setAcceptDrops(True)
        self.drop_hint = ""  # shown while files are dragged over the screen
        self.drop_target = "/sdcard/Download"

        self._device: DeviceBackend | None = None
        self._unsubscribe: Callable[[], None] | None = None
        self._lock = threading.Lock()
        self._pending: Frame | None = None
        self._signal_pending = False
        self.image: QImage | None = None
        self.frame_seq = 0
        self._seq_at_last_check = 0  # resync() watchdog
        self._blur_cache: tuple[int, QImage] | None = None
        self.frameReady.connect(self._consume_frame)

        # input state
        self._touching = False
        self._pinch = False
        self._last_touch: tuple[int, int] = (0, 0)
        self._pressed: dict[int, tuple[int, int]] = {}  # Qt key -> (android keycode, meta)
        self._buttons_down: set[Qt.MouseButton] = set()
        self._clip_seq = 0
        self.preedit = ""

        # overlay state
        self.control_state = "manual"
        self.dimmed = False  # reconnecting: grey last frame
        self.offline_notice = ""  # set while not streaming: greys the frozen frame and says why
        self._flash_until = 0.0
        self._reveal_until = 0.0
        self._flash_timer = QTimer(self)
        self._flash_timer.setInterval(30)
        self._flash_timer.timeout.connect(self._flash_tick)

        # stats
        self.fps = 0
        self._fps_count = 0
        self._fps_t0 = time.monotonic()

    # ------------------------------------------------------------ frames
    def attach(self, device: DeviceBackend) -> None:
        """(Re)bind to a device's FrameBus. Safe to call again after a reconnect."""
        if device is self._device:
            return
        self.detach()
        self._device = device
        self._unsubscribe = device.framebus.subscribe(self._on_frame)
        latest = device.framebus.latest()
        if latest is not None:
            self._on_frame(latest)

    def detach(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
        self._unsubscribe = None
        self._device = None

    def _on_frame(self, frame: Frame) -> None:
        """Decoder thread. Never blocks: keep the newest frame, signal at most once per GUI turn."""
        with self._lock:
            self._pending = frame
            if self._signal_pending:
                return
            self._signal_pending = True
        try:
            self.frameReady.emit()
        except RuntimeError:  # widget deleted
            pass

    def _consume_frame(self) -> None:
        with self._lock:
            frame = self._pending
            self._pending = None
            self._signal_pending = False
        if frame is None:
            return
        old = (self.image.width(), self.image.height()) if self.image is not None else None
        self.image = frame_to_qimage(frame.rgb)
        self.frame_seq = frame.seq
        self._fps_count += 1
        now = time.monotonic()
        if now - self._fps_t0 >= 1.0:
            self.fps = round(self._fps_count / (now - self._fps_t0))
            self._fps_count = 0
            self._fps_t0 = now
        if old != (frame.width, frame.height):
            self.frameSizeChanged.emit(frame.width, frame.height)
        self.update()

    def tick_fps(self) -> int:
        """Called by the status timer; decays the FPS readout when no frames arrive (static screen)."""
        self.resync()
        if time.monotonic() - self._fps_t0 > 2.0:
            self.fps = 0
        return self.fps

    def resync(self) -> bool:
        """Watchdog: if the FrameBus holds a newer frame than the one drawn (a lost frameReady signal leaves the
        view frozen while the stream is fine), take it now. Returns True when it had to step in."""
        dev = self._device
        latest = dev.framebus.latest() if dev is not None else None
        drawn_before, self._seq_at_last_check = self._seq_at_last_check, self.frame_seq
        # only when nothing was drawn since the last check: one frame in flight during live video is normal
        if latest is None or latest.seq == self.frame_seq or self.frame_seq != drawn_before:
            return False
        with self._lock:
            stuck = self._signal_pending
            self._pending = latest
            self._signal_pending = False
        log.warning("mirror was behind the stream (seq %s < %s, signal stuck=%s); redrawing", self.frame_seq, latest.seq, stuck)
        self._consume_frame()
        self._seq_at_last_check = self.frame_seq
        return True

    @property
    def frame_size(self) -> tuple[int, int]:
        if self.image is not None:
            return self.image.width(), self.image.height()
        if self._device is not None:
            return self._device.frame_size
        return (0, 0)

    # ----------------------------------------------------------- mapping
    def fit(self) -> FitRect:
        fw, fh = self.frame_size
        return fit_rect(self.width(), self.height(), fw, fh)

    def map_to_display(self, pos: QPointF, *, clamp: bool = False) -> tuple[int, int] | None:
        """Widget point -> device display pixels (FR-MIR-044). Letterbox -> None unless clamp."""
        dev = self._device
        if dev is None:
            return None
        r = self.fit()
        if r.scale <= 0:
            return None
        fw, fh = self.frame_size
        fx = (pos.x() - r.x) / r.scale
        fy = (pos.y() - r.y) / r.scale
        if not (0 <= fx < fw and 0 <= fy < fh):
            if not clamp:
                return None
            fx = min(max(fx, 0.0), fw - 1)
            fy = min(max(fy, 0.0), fh - 1)
        dw, dh = dev.display_size
        dx = int(min(max(round(fx * dw / fw), 0), dw - 1))
        dy = int(min(max(round(fy * dh / fh), 0), dh - 1))
        return dx, dy

    def _mirror_point(self, x: int, y: int) -> tuple[int, int]:
        """Virtual pinch finger: point symmetric to (x, y) around the screen center (FR-INP-008)."""
        assert self._device is not None
        dw, dh = self._device.display_size
        return int(min(max(dw - 1 - x, 0), dw - 1)), int(min(max(dh - 1 - y, 0), dh - 1))

    # -------------------------------------------------------------- mouse
    def _touch(self, action: p.MotionAction, x: int, y: int, pointer_id: int = p.POINTER_ID_GENERIC_FINGER) -> None:
        t = time.monotonic()  # the person's timing, before the input waits in the pump (FR-INP-035)
        self.pump.arbiter(lambda a: a.human_touch(action, x, y, pointer_id=pointer_id, t=t))

    def _key_tap(self, keycode: int, action: p.KeyAction) -> None:
        t = time.monotonic()
        self.pump.arbiter(lambda a: a.human_key(action, keycode, t=t))

    def _raw(self, msg: bytes) -> None:
        t = time.monotonic()
        self.pump.arbiter(lambda a: a.human_raw(msg, t=t))

    def mousePressEvent(self, e: QMouseEvent) -> None:
        btn = e.button()
        if btn == Qt.MouseButton.LeftButton:
            pt = self.map_to_display(e.position())
            if pt is None or self._touching:  # FR-INP-013: letterbox clicks are ignored
                return
            self._touching = True
            self._last_touch = pt
            self._touch(p.MotionAction.DOWN, *pt)
            self._pinch = bool(e.modifiers() & Qt.KeyboardModifier.ControlModifier)
            if self._pinch:
                self._touch(p.MotionAction.DOWN, *self._mirror_point(*pt), pointer_id=p.POINTER_ID_VIRTUAL_FINGER)
            e.accept()
            return
        self._buttons_down.add(btn)
        if btn == Qt.MouseButton.RightButton:  # FR-INP-003 / FR-MIR-063
            self._raw(p.back_or_screen_on(p.KeyAction.DOWN))
        elif btn == Qt.MouseButton.MiddleButton:
            self._key_tap(p.Keycode.HOME, p.KeyAction.DOWN)
        elif btn == Qt.MouseButton.XButton1:
            self._key_tap(p.Keycode.BACK, p.KeyAction.DOWN)
        elif btn == Qt.MouseButton.XButton2:
            self._key_tap(p.Keycode.APP_SWITCH, p.KeyAction.DOWN)
        e.accept()

    def mouseMoveEvent(self, e: QMouseEvent) -> None:
        if not self._touching:
            return  # FR-INP-042: hover is not input
        pt = self.map_to_display(e.position(), clamp=True)  # FR-INP-012/013: drags continue, clamped
        if pt is None or pt == self._last_touch:
            return
        self._last_touch = pt
        self._touch(p.MotionAction.MOVE, *pt)
        if self._pinch:
            self._touch(p.MotionAction.MOVE, *self._mirror_point(*pt), pointer_id=p.POINTER_ID_VIRTUAL_FINGER)
        e.accept()

    def mouseReleaseEvent(self, e: QMouseEvent) -> None:
        btn = e.button()
        if btn == Qt.MouseButton.LeftButton:
            if self._touching:
                pt = self.map_to_display(e.position(), clamp=True) or self._last_touch
                self._end_touch(pt)
            e.accept()
            return
        self._release_button(btn)
        e.accept()

    def _release_button(self, btn: Qt.MouseButton) -> None:
        if btn not in self._buttons_down:
            return
        self._buttons_down.discard(btn)
        if btn == Qt.MouseButton.RightButton:
            self._raw(p.back_or_screen_on(p.KeyAction.UP))
        elif btn == Qt.MouseButton.MiddleButton:
            self._key_tap(p.Keycode.HOME, p.KeyAction.UP)
        elif btn == Qt.MouseButton.XButton1:
            self._key_tap(p.Keycode.BACK, p.KeyAction.UP)
        elif btn == Qt.MouseButton.XButton2:
            self._key_tap(p.Keycode.APP_SWITCH, p.KeyAction.UP)

    def _end_touch(self, pt: tuple[int, int]) -> None:
        self._touch(p.MotionAction.UP, *pt)
        if self._pinch:
            self._touch(p.MotionAction.UP, *self._mirror_point(*pt), pointer_id=p.POINTER_ID_VIRTUAL_FINGER)
        self._touching = False
        self._pinch = False

    def wheelEvent(self, e: QWheelEvent) -> None:
        pt = self.map_to_display(e.position())
        if pt is None:
            return
        d = e.angleDelta()
        if e.modifiers() & Qt.KeyboardModifier.ShiftModifier:  # FR-INP-007
            h, v = (d.y() or d.x()) / 120.0, 0.0
        else:
            h, v = d.x() / 120.0, d.y() / 120.0
        if h or v:
            x, y = pt
            self.pump.arbiter(lambda a: a.human_scroll(x, y, h, v))
        e.accept()

    # ----------------------------------------------------------- keyboard
    def focusNextPrevChild(self, _next: bool) -> bool:  # Tab goes to the phone, not to the next widget
        return False

    def keyPressEvent(self, e: QKeyEvent) -> None:
        key = e.key()
        mods = e.modifiers()
        ctrl = bool(mods & Qt.KeyboardModifier.ControlModifier)
        alt = bool(mods & Qt.KeyboardModifier.AltModifier)
        shift = bool(mods & Qt.KeyboardModifier.ShiftModifier)
        win = self.window()
        if key == Qt.Key.Key_F11:
            self.fullscreenToggleKey.emit()
            return
        if key == Qt.Key.Key_Escape and win.isFullScreen():  # FR-MIR-035: Esc only leaves fullscreen
            self.escapeInFullscreen.emit()
            return
        if alt and not ctrl:  # Mod (left Alt) combos are PhonePilot shortcuts, never forwarded
            return
        if key in _MODIFIER_KEYS:
            return
        meta = (p.META_CTRL_ON if ctrl else 0) | (p.META_SHIFT_ON if shift else 0)
        if ctrl and key == Qt.Key.Key_V and not shift:
            self.paste_pc_clipboard()
            return
        if key in KEYMAP:
            self._key_down(key, KEYMAP[key], meta, e.isAutoRepeat())
            return
        if ctrl:
            code = ctrl_keycode(key)
            if code is not None:
                self._key_down(key, code, meta, e.isAutoRepeat())
            return
        text = e.text()
        if is_printable_ascii(text):
            self._raw(p.inject_text(text))
        elif text and text.isprintable():
            self.send_unicode(text)

    def _key_down(self, qt_key: int, keycode: int, meta: int, repeat: bool) -> None:
        self._pressed[qt_key] = (keycode, meta)
        rep = 1 if repeat else 0
        t = time.monotonic()
        self.pump.arbiter(lambda a: a.human_key(p.KeyAction.DOWN, keycode, meta, rep, t=t))

    def keyReleaseEvent(self, e: QKeyEvent) -> None:
        if e.isAutoRepeat():
            return
        held = self._pressed.pop(e.key(), None)
        if held is not None:
            code, meta = held
            self.pump.arbiter(lambda a: a.human_key(p.KeyAction.UP, code, meta))

    def release_all(self) -> None:
        """FR-INP-027 / NFR-REL-004: nothing stays pressed when we lose focus."""
        for code, meta in list(self._pressed.values()):
            self.pump.arbiter(lambda a, c=code, m=meta: a.human_key(p.KeyAction.UP, c, m))
        self._pressed.clear()
        if self._touching:
            self._end_touch(self._last_touch)
        for btn in list(self._buttons_down):
            self._release_button(btn)

    def focusOutEvent(self, e: QFocusEvent) -> None:
        self.release_all()
        super().focusOutEvent(e)

    def next_clip_seq(self) -> int:
        self._clip_seq += 1
        return self._clip_seq

    def send_unicode(self, text: str) -> None:
        """Non-ASCII text: set the phone clipboard and paste (FR-INP-021 default path)."""
        if text:
            self._raw(p.set_clipboard(self.next_clip_seq(), text, paste=True))

    def send_text(self, text: str) -> None:
        if is_printable_ascii(text):
            raw = text.encode("utf-8")
            for i in range(0, len(raw), p.INJECT_TEXT_MAX_BYTES):
                self._raw(p.inject_text_bytes(raw[i : i + p.INJECT_TEXT_MAX_BYTES]))
        else:
            self.send_unicode(text)

    def paste_pc_clipboard(self) -> None:
        text = QGuiApplication.clipboard().text()
        if text:
            self._raw(p.set_clipboard(self.next_clip_seq(), text, paste=True))

    def inputMethodEvent(self, e: QInputMethodEvent) -> None:
        commit = e.commitString()
        if commit:
            self.send_text(commit)
        self.preedit = e.preeditString()
        self.update()
        e.accept()

    def inputMethodQuery(self, query: Qt.InputMethodQuery):  # type: ignore[no-untyped-def]
        if query == Qt.InputMethodQuery.ImEnabled:
            return True
        if query == Qt.InputMethodQuery.ImCursorRectangle:
            r = self.fit()
            return QRect(int(r.x + r.w / 2), int(r.y + r.h * 0.8), 1, 20)
        if query in (Qt.InputMethodQuery.ImSurroundingText, Qt.InputMethodQuery.ImCurrentSelection):
            return ""
        if query == Qt.InputMethodQuery.ImCursorPosition:
            return 0
        return super().inputMethodQuery(query)

    # ------------------------------------------------------------ overlays
    def set_control_state(self, state: str) -> None:
        if state != self.control_state:
            self.control_state = state
            self.update()

    def flash_ai(self) -> None:
        """Briefly highlight an AI action (PRD FR-INP-033, simplified to a fading border)."""
        self._flash_until = time.monotonic() + FLASH_MS / 1000
        if not self._flash_timer.isActive():
            self._flash_timer.start()
        self.update()

    def _flash_tick(self) -> None:
        if time.monotonic() >= self._flash_until:
            self._flash_timer.stop()
        self.update()

    def reveal(self) -> None:
        """Mod+U: show the unblurred screen for 30 s (FR-UI-001)."""
        self._reveal_until = time.monotonic() + REVEAL_S
        QTimer.singleShot(int(REVEAL_S * 1000) + 50, self.update)
        self.update()

    def curtain_active(self) -> bool:
        if time.monotonic() < self._reveal_until:
            return False
        if self.runtime.config.privacy.curtain != "always":
            return False
        stack = self.runtime.stack
        obs = stack.observer.last if stack is not None else None
        return bool(obs is not None and getattr(obs, "sensitive_app", False))

    def _blurred(self, img: QImage) -> QImage:
        if self._blur_cache is not None and self._blur_cache[0] == self.frame_seq:
            return self._blur_cache[1]
        small = img.scaled(max(1, img.width() // 24), max(1, img.height() // 24), Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
        out = small.scaled(img.width(), img.height(), Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
        self._blur_cache = (self.frame_seq, out)
        return out

    def display_image(self) -> QImage | None:
        """What the user sees (blurred under the privacy curtain). Used for screenshots too."""
        if self.image is None:
            return None
        img = self._blurred(self.image) if self.curtain_active() else self.image
        if self.dimmed or self.offline_notice:
            img = img.convertToFormat(QImage.Format.Format_Grayscale8)
        return img

    def set_offline_notice(self, text: str) -> None:
        """Shown over the last frame while there is no live stream, so a frozen picture never looks connected."""
        if text != self.offline_notice:
            self.offline_notice = text
            self.update()

    def paintEvent(self, _e: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), Qt.GlobalColor.black)  # letterbox is always black (18.8)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r = self.fit()
        img = self.display_image()
        target = QRectF(r.x, r.y, r.w, r.h)
        if img is not None and r.scale > 0:
            painter.drawImage(target, img)
            if self.offline_notice:
                painter.fillRect(target, QColor(0, 0, 0, 120))
                self._draw_notice(painter, target, self.offline_notice)
            elif self.curtain_active():
                self._draw_notice(painter, target, "민감 화면 — Alt+U로 30초간 보기")
        else:
            self._draw_notice(painter, QRectF(self.rect()), self.offline_notice or "화면을 기다리는 중…")
        if self.preedit:
            self._draw_preedit(painter, target)
        if self.drop_hint:
            painter.setPen(QPen(QColor("#3b82f6"), 3, Qt.PenStyle.DashLine))
            painter.setBrush(QColor(59, 130, 246, 40))
            painter.drawRoundedRect(target.adjusted(6, 6, -6, -6), 12, 12)
            self._draw_notice(painter, target, self.drop_hint)
        self._draw_border(painter)
        painter.end()

    # ---------------------------------------------------------------- file drop
    @staticmethod
    def _local_paths(e: QDragEnterEvent | QDropEvent) -> list[str]:
        md = e.mimeData()
        if not md.hasUrls():
            return []
        return [u.toLocalFile() for u in md.urls() if u.isLocalFile() and u.toLocalFile()]

    def dragEnterEvent(self, e: QDragEnterEvent) -> None:
        paths = self._local_paths(e)
        if not paths or self._device is None:
            e.ignore()
            return
        apks = [x for x in paths if x.lower().endswith(".apk")]
        if apks and len(apks) == len(paths):
            self.drop_hint = "놓으면 APK 설치 여부를 묻습니다"
        else:
            self.drop_hint = f"놓으면 폰의 {self.drop_target} 폴더로 복사합니다 ({len(paths)}개)"
        e.acceptProposedAction()
        self.update()

    def dragLeaveEvent(self, _e: QDragLeaveEvent) -> None:
        self.drop_hint = ""
        self.update()

    def dropEvent(self, e: QDropEvent) -> None:
        self.drop_hint = ""
        self.update()
        paths = self._local_paths(e)
        if not paths:
            e.ignore()
            return
        e.acceptProposedAction()
        self.filesDropped.emit(paths)

    def _draw_notice(self, painter: QPainter, area: QRectF, text: str) -> None:
        f = QFont(self.font())
        f.setPointSizeF(max(10.0, f.pointSizeF() * 1.1))
        painter.setFont(f)
        flags = Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap
        width = min(360.0, max(160.0, area.width() - 32))
        text_rect = painter.fontMetrics().boundingRect(0, 0, int(width - 32), 10_000, int(flags), text)
        height = text_rect.height() + 28
        box = QRectF(area.center().x() - width / 2, area.center().y() - height / 2, width, height)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(0, 0, 0, 190))
        painter.drawRoundedRect(box, 10, 10)
        painter.setPen(QColor("white"))
        painter.drawText(box.adjusted(16, 14, -16, -14), flags, text)

    def _draw_preedit(self, painter: QPainter, area: QRectF) -> None:
        box = QRectF(area.center().x() - 120, area.bottom() - 70, 240, 36)
        painter.fillRect(box, QColor(30, 30, 30, 220))
        painter.setPen(QColor("white"))
        painter.drawText(box, Qt.AlignmentFlag.AlignCenter, self.preedit)

    def _draw_border(self, painter: QPainter) -> None:
        rect = QRectF(self.rect()).adjusted(1.5, 1.5, -1.5, -1.5)
        state = self.control_state
        if state == "killed":
            painter.setPen(QPen(KILLED_COLOR, 3))
            painter.drawRect(rect)
            return
        if state == "ai_paused":
            painter.setPen(QPen(PAUSED_COLOR, 2, Qt.PenStyle.DashLine))
            painter.drawRect(rect)
            return
        if state == "ai_running":
            painter.setPen(QPen(AI_COLOR, 2))
            painter.drawRect(rect)
        remaining = self._flash_until - time.monotonic()
        if remaining > 0:
            c = QColor(AI_COLOR)
            c.setAlphaF(min(1.0, remaining / (FLASH_MS / 1000)))
            painter.setPen(QPen(c, 5))
            painter.drawRect(rect.adjusted(2, 2, -2, -2))
