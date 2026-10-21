"""MainWindow (PRD 18): toolbar, mirror view, side panel, status badges, shortcuts, approvals."""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import math
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QObject, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QCloseEvent, QGuiApplication, QKeySequence, QShowEvent
from PySide6.QtWidgets import (
    QApplication,
    QDockWidget,
    QFrame,
    QLabel,
    QMainWindow,
    QMessageBox,
    QScrollArea,
    QTabWidget,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from phonepilot.agent.timeline import TimelineEvent
from phonepilot.core.scrcpy import protocol as p
from phonepilot.errors import PhonePilotError
from phonepilot.gui.bridge import Bridge, InputPump, core_call
from phonepilot.gui.dialogs import ApprovalDialog, HumanRequestBanner, SkillInputsDialog, WirelessCodeDialog, WirelessQrDialog
from phonepilot.gui.mirror_view import MirrorView
from phonepilot.gui.panels import ApprovalsPanel, DevicePanel, TimelinePanel, WorkflowPanel
from phonepilot.security.approvals import (
    ApprovalRequest,
    ApprovalResponse,
    HumanRequest,
    HumanResponse,
    abort_all,
    deny_all,
)

if TYPE_CHECKING:
    from phonepilot.runtime import CoreRuntime
    from phonepilot.testing.harness import Stack

log = logging.getLogger(__name__)

DOCK_WIDTH = 360
INITIAL_HEIGHT_RATIO = 0.85  # FR-MIR-031
AI_ACTIVE_WINDOW_S = 2.0
QUIET_AI_TOOLS = {"request_approval", "request_human", "checkpoint", "notify", "capture_backup_codes", "verify_2sv_methods"}

BADGE_COLORS = {
    "gray": ("#4b5563", "white"),
    "blue": ("#2563eb", "white"),
    "yellow": ("#facc15", "#1f2937"),
    "orange": ("#ea580c", "white"),
    "red": ("#dc2626", "white"),
    "green": ("#16a34a", "white"),
}

CONN_LABELS = {
    "DISCONNECTED": ("○ 연결 안 됨", "gray"),
    "DETECTED": ("◔ 기기 감지", "gray"),
    "UNAUTHORIZED": ("⚠ USB 디버깅 허용 필요", "orange"),
    "STARTING": ("◑ 연결 중…", "gray"),
    "STREAMING": ("● 연결됨", "green"),
    "RECONNECTING": ("↻ 재연결 중", "yellow"),
    "FAILED": ("✖ 연결 실패", "red"),
    "STOPPING": ("◌ 종료 중", "gray"),
}


def badge_style(color: str, blink_off: bool = False) -> str:
    bg, fg = BADGE_COLORS[color]
    if blink_off:
        bg, fg = "transparent", BADGE_COLORS[color][0]
    return f"QLabel{{background:{bg};color:{fg};border-radius:4px;padding:1px 8px;font-weight:bold;}}"


class ApprovalController(QObject):
    """Owns approval dialogs and human-request banners; installs the ApprovalCenter handlers.

    The handlers run on the core asyncio loop: they post a request to the GUI thread and await a
    concurrent.futures.Future that the GUI resolves. If the core cancels the wait (timeout), the
    future is cancelled and the dialog/banner closes.
    """

    changed = Signal()

    def __init__(self, window: MainWindow) -> None:
        super().__init__(window)
        self.window = window
        self.runtime = window.runtime
        self.bridge = window.bridge
        self.banner = HumanRequestBanner()
        self.banner.continued.connect(lambda req: self.resolve_human(req.id, HumanResponse("continued")))
        self.banner.aborted.connect(lambda req: self.resolve_human(req.id, HumanResponse("aborted", "user aborted")))
        self.dialogs: dict[int, tuple[ApprovalRequest, concurrent.futures.Future[ApprovalResponse], ApprovalDialog]] = {}
        self.humans: dict[int, tuple[HumanRequest, concurrent.futures.Future[HumanResponse]]] = {}
        self._stack: Stack | None = None

    # --------------------------------------------------------- install
    def install(self, stack: Stack) -> None:
        if self._stack is stack:
            return
        self._stack = stack
        stack.approvals.approval_handler = self.approval_handler
        stack.approvals.human_handler = self.human_handler

    def uninstall(self) -> None:
        if self._stack is not None:
            self._stack.approvals.approval_handler = deny_all
            self._stack.approvals.human_handler = abort_all
            self._stack = None

    # ---------------------------------------------- core-loop coroutines
    async def approval_handler(self, req: ApprovalRequest) -> ApprovalResponse:
        fut: concurrent.futures.Future[ApprovalResponse] = concurrent.futures.Future()
        self.bridge.post(lambda: self._show_approval(req, fut))
        return await asyncio.wrap_future(fut)

    async def human_handler(self, req: HumanRequest) -> HumanResponse:
        fut: concurrent.futures.Future[HumanResponse] = concurrent.futures.Future()
        self.bridge.post(lambda: self._show_human(req, fut))
        return await asyncio.wrap_future(fut)

    # ------------------------------------------------------- GUI thread
    def _show_approval(self, req: ApprovalRequest, fut: concurrent.futures.Future[ApprovalResponse]) -> None:
        if fut.done():
            return
        timeout = self._stack.approvals.timeout_s if self._stack is not None else 120
        dlg = ApprovalDialog(req, self.window, timeout_s=timeout)
        self.dialogs[req.id] = (req, fut, dlg)
        dlg.decided.connect(lambda resp, rid=req.id: self.resolve_approval(rid, resp))
        fut.add_done_callback(lambda _f, rid=req.id: self.bridge.post(lambda: self._approval_closed(rid)))
        self.raise_dialog(req.id)
        QApplication.beep()
        self.changed.emit()

    def raise_dialog(self, req_id: int) -> None:
        entry = self.dialogs.get(req_id)
        if entry is None:
            return
        dlg = entry[2]
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()
        QApplication.alert(self.window)  # taskbar flash

    def resolve_approval(self, req_id: int, resp: ApprovalResponse) -> None:
        entry = self.dialogs.get(req_id)
        if entry is None:
            return
        _req, fut, dlg = entry
        if not fut.done():
            try:
                fut.set_result(resp)
            except concurrent.futures.InvalidStateError:
                pass
        if dlg.isVisible():
            dlg.close_silently()
        if resp.stop_all:
            self.window.trigger_kill()

    def _approval_closed(self, req_id: int) -> None:
        entry = self.dialogs.pop(req_id, None)
        if entry is not None and entry[2].isVisible():
            entry[2].close_silently()
        if entry is not None:
            entry[2].deleteLater()
        self.changed.emit()

    def _show_human(self, req: HumanRequest, fut: concurrent.futures.Future[HumanResponse]) -> None:
        if fut.done():
            return
        self.humans[req.id] = (req, fut)
        self.banner.push(req)
        fut.add_done_callback(lambda _f, rid=req.id: self.bridge.post(lambda: self._human_closed(rid)))
        QApplication.alert(self.window)
        QApplication.beep()
        self.changed.emit()

    def resolve_human(self, req_id: int, resp: HumanResponse) -> None:
        entry = self.humans.get(req_id)
        if entry is None:
            return
        _req, fut = entry
        if not fut.done():
            try:
                fut.set_result(resp)
            except concurrent.futures.InvalidStateError:
                pass
        if resp.outcome == "continued" and self.runtime.stack is not None:
            core_call(self.runtime, self.runtime.stack.arbiter.resume_ai)
        self._human_closed(req_id)

    def _human_closed(self, req_id: int) -> None:
        self.humans.pop(req_id, None)
        self.banner.remove(req_id)
        self.changed.emit()

    def deny_all(self, note: str) -> None:
        """Kill switch / closing: every pending approval is denied, every hand-off aborted."""
        for rid in list(self.dialogs):
            self.resolve_approval(rid, ApprovalResponse(False, note=note))
        for rid in list(self.humans):
            self.resolve_human(rid, HumanResponse("aborted", note))

    def pending_requests(self) -> list[ApprovalRequest]:
        return [req for req, _f, _d in self.dialogs.values()]


class MainWindow(QMainWindow):
    def __init__(self, runtime: CoreRuntime, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.runtime = runtime
        self.setWindowTitle("PhonePilot" + (" — 데모" if runtime.demo else ""))
        self.bridge = Bridge(self)
        self.pump = InputPump(runtime)
        self._sized = False
        self._dock_sized = False
        self._pre_fullscreen: dict[str, bool] = {}
        self._last_ai_ts = -1e9
        self._last_ai_actor = ""
        self._runs: dict[str, dict[str, Any]] = {}
        self._blink = False
        self._attached_stack: Stack | None = None

        # central: human banner + mirror
        self.mirror = MirrorView(runtime, self.pump)
        self.approval_ctl = ApprovalController(self)
        self.approval_ctl.changed.connect(self._approvals_changed)
        central = QWidget()
        cl = QVBoxLayout(central)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(0)
        cl.addWidget(self.approval_ctl.banner)
        cl.addWidget(self.mirror, 1)
        self.setCentralWidget(central)

        self._build_dock()
        self._build_actions()
        self._build_toolbar()
        self._build_statusbar()

        self.mirror.frameSizeChanged.connect(self._on_frame_size)
        self.mirror.escapeInFullscreen.connect(self.exit_fullscreen)
        self.mirror.fullscreenToggleKey.connect(self.toggle_fullscreen)
        self.bridge.event.connect(self._on_event)
        runtime.listeners.append(self.bridge.forward)

        self._status_timer = QTimer(self)
        self._status_timer.timeout.connect(self.refresh_status)
        self._status_timer.start(250)

        self.resize(480 + DOCK_WIDTH, 860)
        if runtime.stack is not None and runtime.device is not None:
            self.attach_stack()
            self._set_conn(runtime.state.value, runtime.state_detail)
        self.refresh_status()
        self.mirror.setFocus()

    # ================================================================ build
    def _build_dock(self) -> None:
        self.tabs = QTabWidget()
        self.timeline_panel = TimelinePanel()
        self.workflow_panel = WorkflowPanel()
        self.approvals_panel = ApprovalsPanel()
        self.device_panel = DevicePanel()
        self.tabs.addTab(self.timeline_panel, "타임라인")
        self.tabs.addTab(self.workflow_panel, "워크플로")
        self.tabs.addTab(self.approvals_panel, "승인")
        device_scroll = QScrollArea()  # the device tab grew (wireless, calibration); scroll instead of forcing a tall window
        device_scroll.setWidgetResizable(True)
        device_scroll.setFrameShape(QFrame.Shape.NoFrame)
        device_scroll.setWidget(self.device_panel)
        self.tabs.addTab(device_scroll, "기기")
        self.dock = QDockWidget("패널", self)
        self.dock.setObjectName("sidePanel")
        self.dock.setWidget(self.tabs)
        self.dock.setFeatures(QDockWidget.DockWidgetFeature.DockWidgetClosable | QDockWidget.DockWidgetFeature.DockWidgetMovable)
        self.dock.setAllowedAreas(Qt.DockWidgetArea.RightDockWidgetArea | Qt.DockWidgetArea.LeftDockWidgetArea)
        self.dock.setMinimumWidth(280)
        self.dock.setMaximumWidth(560)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.dock)
        self.resizeDocks([self.dock], [DOCK_WIDTH], Qt.Orientation.Horizontal)

        self.workflow_panel.controlRequested.connect(self._workflow_control)
        self.workflow_panel.runRequested.connect(self._workflow_run)
        self.approvals_panel.openRequested.connect(self.approval_ctl.raise_dialog)
        self.device_panel.copyToken.connect(self.copy_token)
        self.device_panel.copyUrl.connect(lambda: self._copy_text(self.runtime.mcp_url, "MCP URL을 복사했습니다"))
        self.device_panel.reconnect.connect(lambda: self.connect_device(start_mcp=False))
        self.device_panel.wirelessQr.connect(self.wireless_pair_qr)
        self.device_panel.wirelessCode.connect(self.wireless_pair_code)
        self.device_panel.wirelessScan.connect(self.wireless_scan)
        self._qr_dialog: WirelessQrDialog | None = None
        self._code_dialog: WirelessCodeDialog | None = None
        self.device_panel.calibStart.connect(self.start_calibration)
        self.device_panel.calibFinish.connect(self.finish_calibration)
        self.device_panel.calibReset.connect(self.reset_calibration)
        self._calib_timer = QTimer(self)
        self._calib_timer.setInterval(300)
        self._calib_timer.timeout.connect(lambda: self.device_panel.set_calibrating(True, self.runtime.calibration_counts()))

    def _action(
        self,
        text: str,
        handler: Callable[[], Any],
        shortcuts: list[str] | None = None,
        tip: str = "",
        checkable: bool = False,
    ) -> QAction:
        act = QAction(text, self)
        if shortcuts:
            act.setShortcuts([QKeySequence(s) for s in shortcuts])
            act.setShortcutContext(Qt.ShortcutContext.WindowShortcut)
        act.setCheckable(checkable)
        keys = ", ".join(s.replace("Alt+", "Mod+") if s.startswith("Alt+") else s for s in shortcuts or [])
        act.setToolTip(f"{tip or text}" + (f" ({keys})" if keys else ""))
        act.triggered.connect(lambda _checked=False: handler())
        self.addAction(act)  # keeps shortcuts alive when the toolbar is hidden (fullscreen)
        return act

    def _build_actions(self) -> None:
        K = p.Keycode
        a = self._action
        self.back_action = a("뒤로", lambda: self.press_key(K.BACK), ["Alt+B", "Alt+Backspace"])
        self.home_action = a("홈", lambda: self.press_key(K.HOME), ["Alt+H"])
        self.recents_action = a("최근 앱", lambda: self.press_key(K.APP_SWITCH), ["Alt+R"])
        self.power_action = a("전원", lambda: self.press_key(K.POWER), ["Alt+P"])
        self.vol_up_action = a("볼륨 크게", lambda: self.press_key(K.VOLUME_UP), ["Alt+Up"])
        self.vol_down_action = a("볼륨 작게", lambda: self.press_key(K.VOLUME_DOWN), ["Alt+Down"])
        self.rotate_action = a("회전", lambda: self.send_raw(p.simple(p.ControlType.ROTATE_DEVICE)), ["Alt+Left", "Alt+Right"], "기기 회전")
        self.notif_action = a("알림 패널", lambda: self.send_raw(p.simple(p.ControlType.EXPAND_NOTIFICATION_PANEL)), ["Alt+N"])
        self.qs_action = a("빠른 설정", lambda: self.send_raw(p.simple(p.ControlType.EXPAND_SETTINGS_PANEL)), ["Alt+Shift+N"])
        self.collapse_action = a("패널 접기", lambda: self.send_raw(p.simple(p.ControlType.COLLAPSE_PANELS)), ["Alt+Shift+H"])
        self.screen_off_action = a("화면 끄기", lambda: self.set_display_power(False), ["Alt+O"], "폰 화면 끄기(미러링 유지)")
        self.screen_on_action = a("화면 켜기", lambda: self.set_display_power(True), ["Alt+Shift+O"], "폰 화면 켜기")
        self.screenshot_action = a("스크린샷", self.screenshot_to_clipboard, ["Alt+S"], "스크린샷을 클립보드로")
        self.fit_action = a("창 맞춤", self.fit_window_to_frame, ["Alt+G"], "창을 화면 비율에 맞춤")
        self.on_top_action = a("항상 위", lambda: self.set_always_on_top(self.on_top_action.isChecked()), ["Alt+T"], checkable=True)
        self.panel_action = a("패널", lambda: self.set_panel_visible(self.panel_action.isChecked()), ["Alt+\\"], "사이드 패널 토글", checkable=True)
        self.panel_action.setChecked(True)
        self.fullscreen_action = a("전체화면", self.toggle_fullscreen, ["F11"])
        self.escape_action = a("전체화면 해제", self.exit_fullscreen, ["Esc"])
        self.escape_action.setEnabled(False)  # Esc goes to the phone unless we are fullscreen
        self.reveal_action = a("가림막 해제", self.mirror.reveal, ["Alt+U"], "민감 화면 30초간 보기")
        self.pause_action = a("AI 일시정지", self.toggle_ai_pause, ["Alt+Space"], "AI 일시정지/재개", checkable=True)
        self.allow_action = a("AI 허용", self.allow_ai, [], "킬 스위치 해제 — AI 행동 다시 허용")
        self.allow_action.setVisible(False)
        self.stop_action = a("■ 정지", self.trigger_kill, [], "킬 스위치: 모든 AI 행동 즉시 정지 (Pause, Ctrl+Alt+End)")

    def _build_toolbar(self) -> None:
        tb = QToolBar("도구", self)
        tb.setObjectName("mainToolbar")
        tb.setMovable(False)
        tb.setIconSize(QSize(16, 16))
        tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        for act in (self.back_action, self.home_action, self.recents_action):
            tb.addAction(act)
        tb.addSeparator()
        for act in (self.rotate_action, self.screen_off_action, self.screenshot_action):
            tb.addAction(act)
        tb.addSeparator()
        for act in (self.fullscreen_action, self.on_top_action, self.panel_action):
            tb.addAction(act)
        tb.addSeparator()
        tb.addAction(self.pause_action)
        tb.addAction(self.allow_action)
        tb.addAction(self.stop_action)
        stop_btn = tb.widgetForAction(self.stop_action)
        if stop_btn is not None:
            stop_btn.setStyleSheet("QToolButton{background:#dc2626;color:white;font-weight:bold;padding:3px 12px;border-radius:4px;}")
        allow_btn = tb.widgetForAction(self.allow_action)
        if allow_btn is not None:
            allow_btn.setStyleSheet("QToolButton{background:#16a34a;color:white;font-weight:bold;padding:3px 10px;border-radius:4px;}")
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, tb)
        self.toolbar = tb

    def _build_statusbar(self) -> None:
        sb = self.statusBar()
        self.control_badge = QLabel()
        self.conn_badge = QLabel()
        self.res_label = QLabel()
        self.fps_label = QLabel()
        self.mcp_label = QLabel("MCP: 시작 전")
        self.hotkey_label = QLabel()
        for w in (self.control_badge, self.conn_badge, self.res_label, self.fps_label):
            sb.addWidget(w)
        sb.addPermanentWidget(self.hotkey_label)
        sb.addPermanentWidget(self.mcp_label)
        self._set_conn(self.runtime.state.value, self.runtime.state_detail)

    # ============================================================ device ops
    def press_key(self, keycode: int) -> None:
        self.pump.arbiter(lambda a: a.human_key(p.KeyAction.DOWN, keycode))
        self.pump.arbiter(lambda a: a.human_key(p.KeyAction.UP, keycode))

    def send_raw(self, msg: bytes) -> None:
        self.pump.arbiter(lambda a: a.human_raw(msg))

    def set_display_power(self, on: bool) -> None:
        self.send_raw(p.set_display_power(on))
        self.statusBar().showMessage("폰 화면을 켰습니다" if on else "폰 화면을 껐습니다 (미러링은 계속됩니다)", 3000)

    def screenshot_to_clipboard(self) -> None:
        img = self.mirror.display_image()
        if img is None:
            self.statusBar().showMessage("아직 화면이 없습니다", 3000)
            return
        QGuiApplication.clipboard().setImage(img)
        self.statusBar().showMessage("스크린샷을 클립보드에 복사했습니다", 3000)

    # ================================================================ AI ops
    def trigger_kill(self) -> None:
        """Kill switch (PRD 13.5). Safe to call from the global hotkey."""
        self.approval_ctl.deny_all("kill switch")
        self.mirror.release_all()
        try:
            self.runtime.call(self.runtime.kill())
        except RuntimeError:
            pass
        self.statusBar().showMessage("킬 스위치: 모든 AI 행동을 정지했습니다", 5000)
        self.refresh_status()

    def allow_ai(self) -> None:
        if self.runtime.stack is not None:
            core_call(self.runtime, self.runtime.stack.arbiter.allow_ai)
            self.timeline_panel.add_system("AI 허용(킬 스위치 해제)")

    def toggle_ai_pause(self) -> None:
        stack = self.runtime.stack
        if stack is None:
            return
        arb = stack.arbiter
        if arb.killed:
            self.pause_action.setChecked(False)
            return
        if arb.paused:
            core_call(self.runtime, arb.resume_ai)
        else:
            core_call(self.runtime, arb.pause_ai, "manual")

    # =========================================================== workflows
    def _workflow_control(self, run_id: str, action: str) -> None:
        stack = self.runtime.stack
        if stack is None:
            return
        self._core_task(stack.engine.control(run_id, action), "워크플로 제어 실패")

    def _workflow_run(self, skill: dict[str, Any]) -> None:
        stack = self.runtime.stack
        if stack is None:
            return
        dlg = SkillInputsDialog(skill, self)
        if dlg.exec() != SkillInputsDialog.DialogCode.Accepted:
            return
        self._core_task(stack.engine.start(skill["name"], dlg.inputs(), requested_by="user"), "워크플로 시작 실패")

    def _core_task(self, coro: Any, error_title: str) -> None:
        try:
            fut = self.runtime.call(coro)
        except RuntimeError:
            coro.close()
            return

        def done(f: concurrent.futures.Future[Any]) -> None:
            exc = f.exception() if not f.cancelled() else None
            if exc is not None:
                self.bridge.post(lambda: self._show_error(error_title, exc))

        fut.add_done_callback(done)

    def _show_error(self, title: str, exc: BaseException) -> None:
        msg = f"{exc.code}: {exc}" if isinstance(exc, PhonePilotError) else str(exc)
        self.timeline_panel.add_system(f"{title} — {msg}", "error")
        self.statusBar().showMessage(f"{title}: {msg}", 6000)

    # ============================================================ connection
    def connect_device(self, *, start_mcp: bool = True) -> None:
        self._set_conn("STARTING", "")
        try:
            fut = self.runtime.call(self.runtime.connect())
        except RuntimeError:
            return
        fut.add_done_callback(lambda f: self.bridge.post(lambda: self._connected(f, start_mcp)))

    def _connected(self, fut: concurrent.futures.Future[None], start_mcp: bool) -> None:
        exc = fut.exception()
        if exc is not None:
            self._show_error("기기 연결 실패", exc)
            self._set_conn(self.runtime.state.value, self.runtime.state_detail)
            return
        self.attach_stack()
        if start_mcp and not self.runtime.mcp_started:
            fut2 = self.runtime.call(self.runtime.start_mcp())
            fut2.add_done_callback(lambda f: self.bridge.post(lambda: self._mcp_started(f)))

    def _mcp_started(self, fut: concurrent.futures.Future[str]) -> None:
        exc = fut.exception()
        if exc is not None:
            self._show_error("MCP 서버 시작 실패", exc)
            self.mcp_label.setText("MCP: 실패")
            return
        self._set_mcp_url(fut.result())

    def _set_mcp_url(self, url: str) -> None:
        self.mcp_label.setText(f"MCP: {url}")
        self.mcp_label.setToolTip("Claude Code/Desktop에서 이 주소와 토큰으로 연결합니다")
        self.device_panel.mcp_url.setText(url)

    def attach_stack(self) -> None:
        stack, device = self.runtime.stack, self.runtime.device
        if stack is None or device is None:
            return
        self.mirror.attach(device)
        self.mirror.dimmed = False
        self.approval_ctl.install(stack)
        if self._attached_stack is not stack:
            self._attached_stack = stack
            try:
                self.workflow_panel.set_skills(stack.engine.list())
            except Exception as e:  # noqa: BLE001
                log.warning("could not list skills: %s", e)
            for ev in list(stack.timeline.events)[-200:]:
                self.timeline_panel.add_event(ev)
        info = device.info
        self.device_panel.set_field("model", f"{info.manufacturer} {info.model}".strip())
        self.device_panel.set_field("android", f"{info.release} (SDK {info.sdk})")
        self.device_panel.set_field("serial", f"…{info.serial_tail}" if info.serial_tail else "")
        dw, dh = device.display_size
        self.device_panel.set_field("display", f"{dw}×{dh} · {info.density}dpi")
        fw, fh = device.frame_size
        self.device_panel.set_field("frame", f"{fw}×{fh}")
        self.device_panel.set_field("ime", info.ime)
        self.device_panel.set_field("nav", info.nav_mode)
        self.device_panel.set_preflight(self.runtime.preflight_rows)
        if self.runtime.mcp_token is not None:
            self._set_mcp_url(self.runtime.mcp_url)

    # =============================================================== events
    def _on_event(self, event: str, data: dict[str, Any]) -> None:
        handler = getattr(self, f"_ev_{event}", None)
        if handler is not None:
            try:
                handler(data)
            except Exception:  # noqa: BLE001
                log.exception("event handler %s failed", event)

    def _ev_conn_state(self, data: dict[str, Any]) -> None:
        state = str(data.get("state", ""))
        self._set_conn(state, str(data.get("detail", "")))
        if state == "STREAMING":
            self.attach_stack()
        self.mirror.dimmed = state == "RECONNECTING"
        self.mirror.update()

    # ------------------------------------------------------------ wireless
    def wireless_pair_qr(self) -> None:
        from phonepilot.core.wireless import qr_payload, qr_png

        service, password, payload = qr_payload()
        dlg = WirelessQrDialog(qr_png(payload), self)
        self._qr_dialog = dlg
        dlg.cancelled.connect(self.runtime.cancel_pairing)
        fut = self.runtime.call(self.runtime.wireless_pair_qr(service, password))
        fut.add_done_callback(lambda f: self.bridge.post(lambda: self._pairing_done(f)))
        dlg.show()

    def wireless_pair_code(self) -> None:
        dlg = WirelessCodeDialog(self)

        def submit() -> None:
            address, code = dlg.values()
            dlg.clear_code()
            dlg.status.setText("페어링 중…")
            dlg.buttons.setEnabled(False)
            fut = self.runtime.call(self.runtime.wireless_pair_code(address, code))

            def done(f: concurrent.futures.Future[dict[str, Any]]) -> None:
                dlg.buttons.setEnabled(True)
                exc = f.exception()
                if exc is not None:
                    dlg.status.setText("✖ " + (exc.message if isinstance(exc, PhonePilotError) else str(exc)))
                    return
                res = f.result()
                dlg.status.setText(("✔ " if res["ok"] else "✖ ") + res["message"])
                if res["ok"]:
                    self._after_wireless_connected()
                    dlg.accept()

            fut.add_done_callback(lambda f: self.bridge.post(lambda: done(f)))

        dlg.buttons.accepted.connect(submit)
        self._code_dialog = dlg
        dlg.show()

    def wireless_scan(self) -> None:
        self.device_panel.wireless_status.setText("같은 Wi-Fi의 페어링된 폰을 찾는 중…")
        fut = self.runtime.call(self.runtime.wireless_reconnect())

        def done(f: concurrent.futures.Future[list[str]]) -> None:
            exc = f.exception()
            if exc is not None:
                self.device_panel.wireless_status.setText(f"✖ {exc}")
                return
            found = f.result()
            if found:
                self.device_panel.wireless_status.setText("연결됨: " + ", ".join(found))
                self._after_wireless_connected()
            else:
                self.device_panel.wireless_status.setText("찾지 못했습니다. 폰의 무선 디버깅이 켜져 있고 같은 Wi-Fi인지 확인하세요")

        fut.add_done_callback(lambda f: self.bridge.post(lambda: done(f)))

    def _pairing_done(self, fut: concurrent.futures.Future[dict[str, Any]]) -> None:
        exc = fut.exception()
        if exc is not None:
            ok, message = False, (exc.message if isinstance(exc, PhonePilotError) else str(exc))
        else:
            ok, message = bool(fut.result()["ok"]), str(fut.result()["message"])
        if self._qr_dialog is not None:
            self._qr_dialog.finish(ok, message)
        self.device_panel.wireless_status.setText(("✔ " if ok else "✖ ") + message)
        if ok:
            self._after_wireless_connected()

    def _after_wireless_connected(self) -> None:
        if self.runtime.state.value != "STREAMING":
            self.connect_device(start_mcp=not self.runtime.mcp_started)

    def _ev_pairing(self, data: dict[str, Any]) -> None:
        status = str(data.get("status", ""))
        if self._qr_dialog is not None and not data.get("done"):
            self._qr_dialog.set_status(status)
        self.device_panel.wireless_status.setText(status)

    # ------------------------------------------------------- gesture calibration
    def start_calibration(self) -> None:
        self.runtime.start_calibration()
        self.device_panel.set_calibrating(True, self.runtime.calibration_counts())
        self._calib_timer.start()
        self.statusBar().showMessage("제스처 보정 중: 미러 화면에서 탭·길게 누르기·스와이프를 해 주세요", 8000)

    def finish_calibration(self) -> None:
        self._calib_timer.stop()
        counts = self.runtime.calibration_counts()
        prof = self.runtime.finish_calibration(save=True)
        self.device_panel.set_calibrating(False, counts)
        self.device_panel.set_profile(prof)
        if prof.get("calibrated"):
            self.statusBar().showMessage("제스처 프로필을 저장했습니다", 5000)
        else:
            self.statusBar().showMessage("기록이 부족해 저장하지 않았습니다 (탭 3회 또는 스와이프 3회 이상 필요)", 8000)

    def reset_calibration(self) -> None:
        self.runtime.reset_calibration()
        self.statusBar().showMessage("제스처 프로필을 기본값으로 되돌렸습니다", 5000)

    def _ev_gesture_profile(self, data: dict[str, Any]) -> None:
        self.device_panel.set_profile(data)

    def _ev_preflight(self, data: dict[str, Any]) -> None:
        self.device_panel.set_preflight(list(data.get("rows", [])))

    def _ev_frame_size(self, data: dict[str, Any]) -> None:
        self.device_panel.set_field("frame", f"{data.get('width')}×{data.get('height')}")

    def _ev_timeline(self, data: dict[str, Any]) -> None:
        ev: TimelineEvent = data["event"]
        self.timeline_panel.add_event(ev)
        if ev.actor not in ("human", "system") and ev.tool not in QUIET_AI_TOOLS:
            self._last_ai_ts = time.monotonic()
            self._last_ai_actor = ev.actor
            self.mirror.flash_ai()

    def _ev_control(self, _data: dict[str, Any]) -> None:
        self.refresh_status()

    def _ev_workflow(self, data: dict[str, Any]) -> None:
        self._runs[str(data.get("run_id"))] = data
        self.workflow_panel.update_run(data)

    def _ev_notify(self, data: dict[str, Any]) -> None:
        msg = str(data.get("message", ""))
        level = str(data.get("level", "info"))
        self.timeline_panel.add_system(msg, level)
        self.statusBar().showMessage(msg, 6000)
        if level in ("warning", "error"):
            QApplication.alert(self)

    def _ev_device_clipboard(self, data: dict[str, Any]) -> None:
        text = str(data.get("text", ""))
        if text:
            QGuiApplication.clipboard().setText(text)

    def _ev_mcp(self, data: dict[str, Any]) -> None:
        self._set_mcp_url(str(data.get("url", "")))

    def _ev_killed(self, _data: dict[str, Any]) -> None:
        self.approval_ctl.deny_all("kill switch")
        self.timeline_panel.add_system("킬 스위치 작동 — AI 차단", "error")
        self.refresh_status()

    def _ev_cleanup(self, data: dict[str, Any]) -> None:
        self.statusBar().showMessage(f"세션 정리: {data.get('summary', '')}", 8000)

    # =============================================================== status
    def _set_conn(self, state: str, detail: str) -> None:
        text, color = CONN_LABELS.get(state, (state, "gray"))
        if state == "RECONNECTING" and detail:
            text = f"{text} {detail}"
        elif state == "STREAMING" and detail:
            text = f"{text} · {detail}"
        elif state in ("FAILED", "UNAUTHORIZED") and detail:
            text = f"{text} ({detail})"
        self.conn_badge.setText(text)
        self.conn_badge.setStyleSheet(badge_style(color))
        self.device_panel.set_field("state", text)
        self.device_panel.reconnect_button.setEnabled(state in ("DISCONNECTED", "FAILED", "UNAUTHORIZED"))

    def control_status(self) -> tuple[str, str, str, bool]:
        """(badge text, color, mirror state, blink) per PRD 18.3."""
        stack = self.runtime.stack
        if stack is None:
            return "● 수동", "gray", "manual", False
        state = stack.arbiter.state.value
        human = self.approval_ctl.banner.current
        n_approvals = len(self.approval_ctl.dialogs)
        running_wf = [r for r in self._runs.values() if r.get("state") == "RUNNING"]
        ai_active = state == "ai_running" or (time.monotonic() - self._last_ai_ts) < AI_ACTIVE_WINDOW_S or bool(running_wf)
        if state == "killed":
            return "■ 정지됨 — AI 차단", "red", "killed", False
        if human is not None:
            return f"✋ 사람 차례: {self.approval_ctl.banner.kind_label(human.kind)}", "orange", state, False
        if n_approvals:
            return f"⚠ 승인 대기 {n_approvals}건", "orange", state if state != "manual" else "ai_paused", True
        if state == "ai_paused":
            return "⏸ AI 일시정지", "yellow", "ai_paused", False
        if ai_active:
            owner = self._last_ai_actor or stack.arbiter.lease_owner or (f"workflow:{running_wf[0].get('skill')}" if running_wf else "AI")
            return f"▶ AI 실행 중 · {owner}", "blue", "ai_running", False
        return "● 수동", "gray", "manual", False

    def refresh_status(self) -> None:
        text, color, mirror_state, blink = self.control_status()
        self._blink = not self._blink if blink else False
        self.control_badge.setText(text)
        self.control_badge.setStyleSheet(badge_style(color, blink_off=self._blink))
        self.mirror.set_control_state(mirror_state)
        stack = self.runtime.stack
        killed = bool(stack and stack.arbiter.killed)
        paused = bool(stack and stack.arbiter.paused)
        self.allow_action.setVisible(killed)
        self.pause_action.setEnabled(not killed)
        if self.pause_action.isChecked() != paused:
            self.pause_action.setChecked(paused)
        self.pause_action.setText("AI 재개" if paused else "AI 일시정지")
        fw, fh = self.mirror.frame_size
        self.res_label.setText(f"{fw}×{fh}" if fw else "")
        self.fps_label.setText(f"{self.mirror.tick_fps()} fps")
        if stack is not None:
            self.approvals_panel.set_history(list(stack.approvals.history))

    def _approvals_changed(self) -> None:
        self.approvals_panel.set_pending(self.approval_ctl.pending_requests())
        self.refresh_status()

    def set_hotkey_status(self, registered: list[str], failed: list[str]) -> None:
        if failed and not registered:
            self.hotkey_label.setText("⚠ 전역 킬 스위치 없음")
            self.hotkey_label.setToolTip("전역 단축키 등록 실패: " + ", ".join(failed) + " — 툴바의 정지 버튼을 사용하세요")
        elif registered:
            self.hotkey_label.setText("킬 스위치: " + " / ".join(registered))
            self.hotkey_label.setToolTip("창 포커스와 무관하게 동작합니다" + (f" (실패: {', '.join(failed)})" if failed else ""))

    # ============================================================== clipboard
    def _copy_text(self, text: str, message: str) -> None:
        QGuiApplication.clipboard().setText(text)
        self.statusBar().showMessage(message, 3000)

    def copy_token(self) -> None:
        token = self.runtime.mcp_token
        if not token:
            self.statusBar().showMessage("MCP 서버가 아직 시작되지 않았습니다", 3000)
            return
        self._copy_text(token, "MCP 토큰을 복사했습니다 (붙여넣은 뒤 클립보드를 비우세요)")

    # ================================================================ window
    def _chrome_size(self) -> tuple[int, int]:
        """Pixels used around the mirror view (toolbar + status bar height, dock width)."""
        if self.isVisible() and self.mirror.height() > 0 and self._sized:
            return self.width() - self.mirror.width(), self.height() - self.mirror.height()
        h = 0
        if self.toolbar.isVisible() or not self.isVisible():
            h += self.toolbar.sizeHint().height()
        if self.statusBar().isVisible() or not self.isVisible():
            h += self.statusBar().sizeHint().height()
        w = DOCK_WIDTH if self.panel_action.isChecked() else 0
        return w, h

    def _available(self) -> Any:
        screen = self.screen() or QGuiApplication.primaryScreen()
        return screen.availableGeometry()

    def _on_frame_size(self, fw: int, fh: int) -> None:
        if self.isFullScreen() or self.isMaximized():
            self._sized = True
            return
        if not self._sized:
            self.size_to_frame(fw, fh)
        else:
            self._keep_area(fw, fh)

    def size_to_frame(self, fw: int, fh: int) -> None:
        """FR-MIR-031: 85% of the work-area height with the phone's aspect."""
        avail = self._available()
        cw, ch = self._chrome_size()
        win_h = int(avail.height() * INITIAL_HEIGHT_RATIO)
        view_h = max(200, win_h - ch)
        view_w = round(view_h * fw / fh)
        win_w = min(view_w + cw, avail.width())
        self.resize(win_w, win_h)
        self.move(avail.x() + (avail.width() - win_w) // 2, avail.y() + (avail.height() - win_h) // 2)
        self._sized = True
        QTimer.singleShot(0, self.fit_window_to_frame)  # correct the chrome estimate once laid out

    def _keep_area(self, fw: int, fh: int) -> None:
        """FR-MIR-050: on rotation keep the view area, switch to the new aspect."""
        vw, vh = self.mirror.width(), self.mirror.height()
        if vw <= 0 or vh <= 0:
            return
        area = vw * vh
        new_h = math.sqrt(area * fh / fw)
        new_w = area / new_h
        avail = self._available()
        self.resize(min(int(self.width() + new_w - vw), avail.width()), min(int(self.height() + new_h - vh), avail.height()))

    def fit_window_to_frame(self) -> None:
        """Mod+G (FR-MIR-032): remove the letterbox by matching the view to the frame aspect."""
        if self.isFullScreen():
            return
        fw, fh = self.mirror.frame_size
        if not fw or not fh:
            return
        if self.isMaximized():
            self.showNormal()
        vw, vh = self.mirror.width(), self.mirror.height()
        target_w = round(vh * fw / fh)
        avail = self._available()
        if self.width() + target_w - vw <= avail.width():
            self.resize(self.width() + target_w - vw, self.height())
        else:  # too wide: shrink the height instead
            target_h = round(vw * fh / fw)
            self.resize(self.width(), self.height() + target_h - vh)

    def set_always_on_top(self, on: bool) -> None:
        handle = self.windowHandle()
        if handle is not None:
            handle.setFlag(Qt.WindowType.WindowStaysOnTopHint, on)  # no native window re-creation
        else:
            self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, on)
        if self.on_top_action.isChecked() != on:
            self.on_top_action.setChecked(on)

    def set_panel_visible(self, on: bool) -> None:
        if self.isFullScreen():
            self._pre_fullscreen["dock"] = on
            return
        self.dock.setVisible(on)
        if self.panel_action.isChecked() != on:
            self.panel_action.setChecked(on)

    def toggle_fullscreen(self) -> None:
        if self.isFullScreen():
            self.exit_fullscreen()
        else:
            self.enter_fullscreen()

    def enter_fullscreen(self) -> None:
        """FR-MIR-034: borderless fullscreen on the current monitor, aspect kept, chrome hidden."""
        if self.isFullScreen():
            return
        self._pre_fullscreen = {
            "toolbar": self.toolbar.isVisible(),
            "dock": self.dock.isVisible(),
            "status": self.statusBar().isVisible(),
            "maximized": self.isMaximized(),
        }
        self.toolbar.hide()
        self.dock.hide()
        self.statusBar().hide()
        self.showFullScreen()
        self.escape_action.setEnabled(True)
        self.mirror.setFocus()

    def exit_fullscreen(self) -> None:
        """FR-MIR-035: F11 or Esc; Esc is never forwarded to the phone while fullscreen."""
        if not self.isFullScreen():
            return
        pre = self._pre_fullscreen
        if pre.get("maximized"):
            self.showMaximized()
        else:
            self.showNormal()
        self.toolbar.setVisible(pre.get("toolbar", True))
        self.dock.setVisible(pre.get("dock", True))
        self.panel_action.setChecked(pre.get("dock", True))
        self.statusBar().setVisible(pre.get("status", True))
        self.escape_action.setEnabled(False)
        self.mirror.setFocus()

    def showEvent(self, e: QShowEvent) -> None:
        super().showEvent(e)
        if not self._dock_sized:
            self._dock_sized = True
            self.resizeDocks([self.dock], [DOCK_WIDTH], Qt.Orientation.Horizontal)

    def closeEvent(self, e: QCloseEvent) -> None:
        if self.approval_ctl.dialogs:
            ans = QMessageBox.question(self, "PhonePilot", "대기 중인 승인 요청이 있습니다. 모두 거부하고 종료할까요?")
            if ans != QMessageBox.StandardButton.Yes:
                e.ignore()
                return
        self.shutdown_ui()
        super().closeEvent(e)

    def shutdown_ui(self) -> None:
        """Detach from the runtime (the caller shuts the runtime down)."""
        self._status_timer.stop()
        self.mirror.release_all()
        self.approval_ctl.deny_all("app closing")
        self.approval_ctl.uninstall()
        if self.bridge.forward in self.runtime.listeners:
            self.runtime.listeners.remove(self.bridge.forward)
        self.mirror.detach()
        self.pump.close()
