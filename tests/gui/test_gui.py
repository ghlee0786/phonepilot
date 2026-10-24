"""GUI tests on top of the demo runtime (FakeDevice: 540x1200 frames for a 1080x2400 display)."""

from __future__ import annotations

from collections.abc import Iterator
from urllib.parse import unquote_plus

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QApplication, QLabel

from phonepilot import update
from phonepilot.agent.timeline import TimelineEvent
from phonepilot.core.coords import fit_rect
from phonepilot.core.scrcpy import protocol as p
from phonepilot.errors import PhonePilotError
from phonepilot.gui.bridge import InputPump
from phonepilot.gui.dialogs import ApprovalDialog, parse_inputs
from phonepilot.gui.main_window import MainWindow
from phonepilot.gui.mirror_view import MirrorView
from phonepilot.runtime import CoreRuntime
from phonepilot.security.approvals import ApprovalRequest
from phonepilot.security.policy import Level, PolicyDecision
from phonepilot.testing.fake_device import FakeDevice


@pytest.fixture(scope="module")
def runtime() -> Iterator[CoreRuntime]:
    rt = CoreRuntime(demo=True)
    rt.start()
    rt.call_sync(rt.connect())
    yield rt
    rt.call_sync(rt.shutdown())
    rt.stop_loop()
    rt.thread.join(timeout=5)


@pytest.fixture
def device(runtime: CoreRuntime) -> FakeDevice:
    dev = runtime.device
    assert isinstance(dev, FakeDevice)
    dev.goto("home", push=False)
    dev.stack.clear()
    return dev


@pytest.fixture
def window(qtbot, runtime: CoreRuntime, device: FakeDevice) -> Iterator[MainWindow]:  # noqa: ANN001
    win = MainWindow(runtime)
    qtbot.addWidget(win)
    win.show()
    qtbot.waitExposed(win)
    yield win
    win.shutdown_ui()
    stack = runtime.stack
    assert stack is not None
    runtime.call_sync(_allow(stack))


async def _allow(stack) -> None:  # noqa: ANN001
    stack.arbiter.allow_ai()


@pytest.fixture
def view(qtbot, runtime: CoreRuntime, device: FakeDevice) -> Iterator[MirrorView]:  # noqa: ANN001
    pump = InputPump(runtime)
    v = MirrorView(runtime, pump)
    qtbot.addWidget(v)
    v.attach(device)
    v.resize(800, 600)  # wide: letterbox left/right
    v.show()
    qtbot.waitExposed(v)
    qtbot.waitUntil(lambda: v.image is not None)
    yield v
    v.detach()
    pump.close()


def _expected_display(v: MirrorView, x: float, y: float) -> tuple[int, int]:
    r = fit_rect(v.width(), v.height(), 540, 1200)
    fx, fy = (x - r.x) / r.scale, (y - r.y) / r.scale
    return round(fx * 1080 / 540), round(fy * 2400 / 1200)


def test_aspect_fit_click_maps_to_display(qtbot, view: MirrorView, device: FakeDevice) -> None:  # noqa: ANN001
    r = view.fit()
    assert r.h == pytest.approx(600)
    assert r.w == pytest.approx(600 * 540 / 1200)
    assert r.x == pytest.approx((800 - r.w) / 2)
    # tap on the "설정" button (display y 400..520)
    target_y = r.y + 460 / 2400 * r.h
    target_x = r.x + r.w / 2
    pos = QPoint(round(target_x), round(target_y))
    n_taps = len(device.taps)
    qtbot.mouseClick(view, Qt.MouseButton.LeftButton, pos=pos)
    view.pump.flush()
    assert len(device.taps) == n_taps + 1
    ex, ey = _expected_display(view, pos.x(), pos.y())
    tx, ty = device.taps[-1]
    assert abs(tx - ex) <= 2 and abs(ty - ey) <= 2
    assert device.current == "settings"
    touches = [m for m in device.decoded[-2:] if m["type"] == p.ControlType.INJECT_TOUCH_EVENT]
    assert [m["action"] for m in touches] == [p.MotionAction.DOWN, p.MotionAction.UP]
    assert all(m["pointer_id"] == p.POINTER_ID_GENERIC_FINGER for m in touches)
    assert all((m["screen_w"], m["screen_h"]) == (540, 1200) for m in touches)


def test_letterbox_click_ignored(qtbot, view: MirrorView, device: FakeDevice) -> None:  # noqa: ANN001
    before = len(device.decoded)
    qtbot.mouseClick(view, Qt.MouseButton.LeftButton, pos=QPoint(10, 300))
    qtbot.mouseClick(view, Qt.MouseButton.LeftButton, pos=QPoint(790, 300))
    view.pump.flush()
    assert len(device.decoded) == before
    assert view.map_to_display(QPoint(10, 300).toPointF()) is None
    assert view.map_to_display(QPoint(10, 300).toPointF(), clamp=True) == (0, _expected_display(view, 10, 300)[1])


def test_drag_from_inside_continues_clamped(qtbot, view: MirrorView, device: FakeDevice) -> None:  # noqa: ANN001
    r = view.fit()
    inside = QPoint(round(r.x + r.w / 2), 300)
    before = len(device.decoded)
    qtbot.mousePress(view, Qt.MouseButton.LeftButton, pos=inside)
    qtbot.mouseMove(view, QPoint(5, 300))
    qtbot.mouseRelease(view, Qt.MouseButton.LeftButton, pos=QPoint(5, 300))
    view.pump.flush()
    msgs = [m for m in device.decoded[before:] if m["type"] == p.ControlType.INJECT_TOUCH_EVENT]
    assert msgs[0]["action"] == p.MotionAction.DOWN
    assert msgs[-1]["action"] == p.MotionAction.UP
    assert msgs[-1]["x"] == 0  # clamped to the left edge


def test_keyboard_text_and_keycodes(qtbot, view: MirrorView, device: FakeDevice) -> None:  # noqa: ANN001
    before = len(device.decoded)
    view.setFocus()
    qtbot.keyClicks(view, "ab")
    qtbot.keyClick(view, Qt.Key.Key_Return)
    qtbot.keyClick(view, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
    view.send_text("한글")
    view.pump.flush()
    msgs = device.decoded[before:]
    texts = [m["text"] for m in msgs if m["type"] == p.ControlType.INJECT_TEXT]
    assert texts == ["a", "b"]
    keys = [(m["keycode"], m["action"], m["meta"]) for m in msgs if m["type"] == p.ControlType.INJECT_KEYCODE]
    assert (p.Keycode.ENTER, p.KeyAction.DOWN, 0) in keys
    assert (p.Keycode.ENTER, p.KeyAction.UP, 0) in keys
    assert (29, p.KeyAction.DOWN, p.META_CTRL_ON) in keys  # Ctrl+A passes through
    clips = [m for m in msgs if m["type"] == p.ControlType.SET_CLIPBOARD]
    assert clips and clips[-1]["text"] == "한글" and clips[-1]["paste"] is True


def test_focus_loss_releases_keys(qtbot, view: MirrorView, device: FakeDevice) -> None:  # noqa: ANN001
    before = len(device.decoded)
    view.setFocus()
    qtbot.keyPress(view, Qt.Key.Key_Left)
    view.release_all()
    view.pump.flush()
    keys = [(m["keycode"], m["action"]) for m in device.decoded[before:] if m["type"] == p.ControlType.INJECT_KEYCODE]
    assert keys == [(p.Keycode.DPAD_LEFT, p.KeyAction.DOWN), (p.Keycode.DPAD_LEFT, p.KeyAction.UP)]


def test_f11_fullscreen_and_esc_not_forwarded(qtbot, window: MainWindow, device: FakeDevice) -> None:  # noqa: ANN001
    window.mirror.setFocus()
    qtbot.keyClick(window.mirror, Qt.Key.Key_F11)
    qtbot.waitUntil(window.isFullScreen)
    assert not window.toolbar.isVisible()
    assert not window.dock.isVisible()
    assert not window.statusBar().isVisible()
    before = len(device.decoded)
    qtbot.keyClick(window.mirror, Qt.Key.Key_Escape)
    qtbot.waitUntil(lambda: not window.isFullScreen())
    window.pump.flush()
    escapes = [m for m in device.decoded[before:] if m["type"] == p.ControlType.INJECT_KEYCODE and m["keycode"] == p.Keycode.ESCAPE]
    assert escapes == []
    assert window.toolbar.isVisible() and window.dock.isVisible()
    # outside fullscreen, Esc goes to the phone
    qtbot.keyClick(window.mirror, Qt.Key.Key_Escape)
    window.pump.flush()
    assert any(m["type"] == p.ControlType.INJECT_KEYCODE and m["keycode"] == p.Keycode.ESCAPE for m in device.decoded[before:])


def test_stop_button_kills(qtbot, window: MainWindow, runtime: CoreRuntime) -> None:  # noqa: ANN001
    stack = runtime.stack
    assert stack is not None
    window.stop_action.trigger()
    qtbot.waitUntil(lambda: stack.arbiter.state.value == "killed", timeout=2000)
    window.refresh_status()
    assert "정지됨" in window.control_badge.text()
    assert window.mirror.control_state == "killed"
    assert window.allow_action.isVisible()


def _high_request(amount: str | None = "₩4,400") -> ApprovalRequest:
    return ApprovalRequest(
        title="돈 또는 계정 보안에 영향을 주는 행동입니다",
        summary="Play 스토어에서 '구매' 버튼 탭",
        decision=PolicyDecision(Level.CONFIRM_HIGH, "payment", ["구매"], [amount] if amount else []),
        client="claude-code",
        reason="테마 구매 테스트",
        expected_amount=amount,
    )


def test_approval_dialog_defaults_to_deny(qtbot) -> None:  # noqa: ANN001
    req = ApprovalRequest(title="AI가 다음 행동을 요청합니다", summary="YouTube에서 '구독' 탭", decision=PolicyDecision(Level.CONFIRM, "engagement", ["구독"]))
    dlg = ApprovalDialog(req)
    qtbot.addWidget(dlg)
    dlg.show()
    qtbot.waitExposed(dlg)
    assert dlg.deny_button.isDefault()
    assert not dlg.approve_button.autoDefault()
    assert dlg.focusWidget() is dlg.deny_button
    assert dlg.approve_button.isEnabled()  # normal CONFIRM needs no typed confirmation
    with qtbot.waitSignal(dlg.decided) as blocker:
        qtbot.keyClick(dlg, Qt.Key.Key_Return)  # Enter = default button = 거부
    assert blocker.args[0].approved is False


def test_high_risk_approve_needs_digits(qtbot) -> None:  # noqa: ANN001
    dlg = ApprovalDialog(_high_request())
    qtbot.addWidget(dlg)
    dlg.show()
    assert dlg.focusWidget() is dlg.deny_button
    assert not dlg.approve_button.isEnabled()
    assert dlg.confirm_edit is not None
    qtbot.keyClicks(dlg.confirm_edit, "440")
    assert not dlg.approve_button.isEnabled()
    qtbot.keyClicks(dlg.confirm_edit, "0")
    assert dlg.approve_button.isEnabled()
    with qtbot.waitSignal(dlg.decided) as blocker:
        qtbot.mouseClick(dlg.approve_button, Qt.MouseButton.LeftButton)
    resp = blocker.args[0]
    assert resp.approved and resp.typed_confirmation == "4400"


def test_high_risk_without_amount_needs_word(qtbot) -> None:  # noqa: ANN001
    dlg = ApprovalDialog(_high_request(None))
    qtbot.addWidget(dlg)
    assert dlg.confirm_edit is not None
    assert not dlg.approve_button.isEnabled()
    dlg.confirm_edit.setText("승인")
    assert dlg.approve_button.isEnabled()


def test_approval_roundtrip_through_core(qtbot, window: MainWindow, runtime: CoreRuntime) -> None:  # noqa: ANN001
    stack = runtime.stack
    assert stack is not None
    fut = runtime.call(stack.approvals.request(_high_request()))
    qtbot.waitUntil(lambda: len(window.approval_ctl.dialogs) == 1, timeout=3000)
    (_req, _f, dlg), = window.approval_ctl.dialogs.values()
    assert window.approvals_panel.pending.count() == 1
    assert dlg.confirm_edit is not None
    dlg.confirm_edit.setText("4,400")
    dlg.approve()
    resp = fut.result(timeout=3)
    assert resp.approved
    qtbot.waitUntil(lambda: not window.approval_ctl.dialogs, timeout=2000)

    # a denial raises APPROVAL_DENIED in the core
    fut = runtime.call(stack.approvals.request(_high_request()))
    qtbot.waitUntil(lambda: len(window.approval_ctl.dialogs) == 1, timeout=3000)
    (_req, _f, dlg), = window.approval_ctl.dialogs.values()
    dlg.deny()
    with pytest.raises(PhonePilotError) as ei:
        fut.result(timeout=3)
    assert ei.value.code == "APPROVAL_DENIED"


def test_approval_closes_on_core_timeout(qtbot, window: MainWindow, runtime: CoreRuntime) -> None:  # noqa: ANN001
    stack = runtime.stack
    assert stack is not None
    old = stack.approvals.timeout_s
    stack.approvals.timeout_s = 0.3
    try:
        fut = runtime.call(stack.approvals.request(_high_request()))
        with pytest.raises(PhonePilotError) as ei:
            fut.result(timeout=3)
        assert ei.value.code == "APPROVAL_TIMEOUT"
        qtbot.waitUntil(lambda: not window.approval_ctl.dialogs, timeout=2000)
    finally:
        stack.approvals.timeout_s = old


def test_human_banner_continue(qtbot, window: MainWindow, runtime: CoreRuntime) -> None:  # noqa: ANN001
    stack = runtime.stack
    assert stack is not None
    fut = runtime.call(stack.tools.request_human(actor="claude-code", kind="otp", instructions="폰에서 OTP를 입력하세요", timeout_s=10))
    banner = window.approval_ctl.banner
    qtbot.waitUntil(lambda: banner.current is not None, timeout=3000)
    assert banner.isVisible()
    window.refresh_status()
    assert "사람 차례" in window.control_badge.text()
    qtbot.mouseClick(banner.continue_button, Qt.MouseButton.LeftButton)
    assert fut.result(timeout=3)["outcome"] == "continued"
    qtbot.waitUntil(lambda: not banner.isVisible(), timeout=2000)


async def _add_event(stack, ev: TimelineEvent) -> None:  # noqa: ANN001
    stack.timeline.add(ev)


def test_timeline_events_appear(qtbot, window: MainWindow, runtime: CoreRuntime) -> None:  # noqa: ANN001
    stack = runtime.stack
    assert stack is not None
    runtime.call_sync(_add_event(stack, TimelineEvent("claude-code", "tap", "구독", "CONFIRM", "ok")))
    runtime.call_sync(_add_event(stack, TimelineEvent("claude-code", "swipe", "목록", "ALLOW", "ok")))
    lst = window.timeline_panel.list
    qtbot.waitUntil(lambda: lst.count() >= 2 and "swipe" in lst.item(0).text(), timeout=2000)
    assert "tap" in lst.item(1).text()  # newest first
    window.refresh_status()
    assert "AI 실행 중" in window.control_badge.text()
    assert window.mirror.control_state == "ai_running"


def test_lost_connection_greys_out_device_controls(window: MainWindow) -> None:
    window._set_conn("FAILED", "DEVICE_OFFLINE")
    assert not window.home_action.isEnabled() and not window.back_action.isEnabled()
    assert "자동으로 연결" in window.mirror.offline_notice
    assert window.stop_action.isEnabled()  # the kill switch never depends on the phone
    window._set_conn("STREAMING", "FakePhone")
    assert window.home_action.isEnabled()
    assert window.mirror.offline_notice == ""


def test_report_problem_prefills_issue(window: MainWindow) -> None:
    window.report_problem("DEVICE_OFFLINE: lost", "Traceback ...")
    dlg = window._report_dialog
    assert dlg is not None and "/ghlee0786/phonepilot/issues/new?" in dlg.url
    assert "DEVICE_OFFLINE" in unquote_plus(dlg.url)
    dlg.reject()


def test_update_check_offers_newer_release(window: MainWindow, qtbot, monkeypatch, tmp_path) -> None:  # noqa: ANN001
    rel = update.Release("9.9.9", "v9.9.9", "- 새 기능", "https://example/r", "https://example/s.exe", "PhonePilot-9.9.9-setup.exe", 1, "")

    async def fake_fetch(_repo: str) -> update.Release:
        return rel

    monkeypatch.setattr(update, "fetch_latest", fake_fetch)
    monkeypatch.setattr("phonepilot.update.localdata_dir", lambda: tmp_path)
    window.check_for_update(manual=True)
    qtbot.waitUntil(lambda: window._update_dialog is not None, timeout=3000)
    assert "9.9.9" in window.device_panel.update_status.text()
    window._update_dialog.reject()


def test_workflow_panel_lists_skills(window: MainWindow) -> None:
    assert window.workflow_panel.skills.count() > 0
    window.workflow_panel.update_run({"run_id": "wf-9999", "skill": "demo", "state": "RUNNING", "current_step": "s1", "completed": ["s0"], "total_steps": 3})
    item = window.workflow_panel.runs.topLevelItem(0)
    assert item.text(2) == "실행 중" and item.text(4) == "1/3"


def test_parse_inputs() -> None:
    assert parse_inputs("a=1\n# c\nb = x=y\nempty=\n junk") == {"a": "1", "b": "x=y"}


def test_ai_pause_toggle(qtbot, window: MainWindow, runtime: CoreRuntime) -> None:  # noqa: ANN001
    stack = runtime.stack
    assert stack is not None
    window.pause_action.trigger()
    qtbot.waitUntil(lambda: stack.arbiter.paused, timeout=2000)
    window.refresh_status()
    assert "일시정지" in window.control_badge.text()
    window.pause_action.trigger()
    qtbot.waitUntil(lambda: not stack.arbiter.paused, timeout=2000)


def test_screenshot_to_clipboard(qtbot, window: MainWindow) -> None:  # noqa: ANN001
    qtbot.waitUntil(lambda: window.mirror.image is not None)
    window.screenshot_action.trigger()
    img = QApplication.clipboard().image()
    assert not img.isNull() and (img.width(), img.height()) == (540, 1200)


def test_initial_size_follows_aspect(qtbot, window: MainWindow) -> None:  # noqa: ANN001
    window._sized = False  # noqa: SLF001
    window.size_to_frame(540, 1200)
    qtbot.wait(50)
    avail = window._available()  # noqa: SLF001
    assert window.height() == int(avail.height() * 0.85)
    window.fit_window_to_frame()
    qtbot.wait(50)
    r = window.mirror.fit()
    # no letterbox after Mod+G (within rounding)
    assert abs(r.w - window.mirror.width()) <= 3 and abs(r.h - window.mirror.height()) <= 3


def test_mod_shortcuts_reach_device(qtbot, window: MainWindow, device: FakeDevice) -> None:  # noqa: ANN001
    window.activateWindow()
    qtbot.waitActive(window)
    window.mirror.setFocus()
    before = len(device.decoded)
    qtbot.keyClick(window.mirror, Qt.Key.Key_R, Qt.KeyboardModifier.AltModifier)  # Mod+R -> recents
    qtbot.keyClick(window.mirror, Qt.Key.Key_N, Qt.KeyboardModifier.AltModifier)  # Mod+N -> notifications
    qtbot.keyClick(window.mirror, Qt.Key.Key_Q, Qt.KeyboardModifier.AltModifier)  # unbound Mod combo: dropped
    window.pump.flush()
    msgs = device.decoded[before:]
    assert [(m["keycode"], m["action"]) for m in msgs if m["type"] == p.ControlType.INJECT_KEYCODE] == [
        (p.Keycode.APP_SWITCH, p.KeyAction.DOWN),
        (p.Keycode.APP_SWITCH, p.KeyAction.UP),
    ]
    assert any(m["type"] == p.ControlType.EXPAND_NOTIFICATION_PANEL for m in msgs)
    assert not any(m["type"] == p.ControlType.INJECT_TEXT for m in msgs)


def test_ctrl_drag_pinch_adds_virtual_finger(qtbot, view: MirrorView, device: FakeDevice) -> None:  # noqa: ANN001
    r = view.fit()
    start = QPoint(round(r.x + r.w * 0.4), round(r.y + r.h * 0.4))
    end = QPoint(round(r.x + r.w * 0.3), round(r.y + r.h * 0.3))
    before = len(device.decoded)
    ctrl = Qt.KeyboardModifier.ControlModifier
    qtbot.mousePress(view, Qt.MouseButton.LeftButton, ctrl, start)
    qtbot.mouseMove(view, end)
    qtbot.mouseRelease(view, Qt.MouseButton.LeftButton, ctrl, end)
    view.pump.flush()
    touches = [m for m in device.decoded[before:] if m["type"] == p.ControlType.INJECT_TOUCH_EVENT]
    virtual = [m for m in touches if m["pointer_id"] == p.POINTER_ID_VIRTUAL_FINGER]
    real = [m for m in touches if m["pointer_id"] == p.POINTER_ID_GENERIC_FINGER]
    assert [m["action"] for m in virtual][0] == p.MotionAction.DOWN and virtual[-1]["action"] == p.MotionAction.UP
    # mirrored around the center (frame coords): real + virtual ~= frame size
    assert abs(real[0]["x"] + virtual[0]["x"] - 540) <= 2 and abs(real[0]["y"] + virtual[0]["y"] - 1200) <= 2
    assert device._down == {}  # noqa: SLF001 - no pointer left down


def test_wheel_scrolls(qtbot, view: MirrorView, device: FakeDevice) -> None:  # noqa: ANN001
    from PySide6.QtCore import QPointF
    from PySide6.QtGui import QWheelEvent

    r = view.fit()
    pos = QPointF(r.x + r.w / 2, r.y + r.h / 2)
    before = len(device.decoded)
    for mods, delta in ((Qt.KeyboardModifier.NoModifier, -120), (Qt.KeyboardModifier.ShiftModifier, 120)):
        ev = QWheelEvent(pos, view.mapToGlobal(pos), QPoint(0, 0), QPoint(0, delta), Qt.MouseButton.NoButton, mods, Qt.ScrollPhase.NoScrollPhase, False)
        QApplication.sendEvent(view, ev)
    view.pump.flush()
    scrolls = [m for m in device.decoded[before:] if m["type"] == p.ControlType.INJECT_SCROLL_EVENT]
    assert len(scrolls) == 2
    assert scrolls[0]["vscroll"] == pytest.approx(-1, abs=0.01) and scrolls[0]["hscroll"] == 0
    assert scrolls[1]["hscroll"] == pytest.approx(1, abs=0.01) and scrolls[1]["vscroll"] == 0


def test_gesture_calibration_panel(qtbot, window: MainWindow, runtime: CoreRuntime, tmp_path) -> None:  # noqa: ANN001
    from phonepilot.core import gesture as g
    from phonepilot.core.calibration import ProfileStore

    runtime.profiles = ProfileStore(tmp_path / "gesture_profiles.yaml")  # keep the user's real profile untouched
    panel = window.device_panel
    qtbot.mouseClick(panel.calib_start_button, Qt.MouseButton.LeftButton)
    assert runtime.recorder.active and panel.calib_finish_button.isEnabled()
    # a real mouse drag in the mirror goes through the arbiter into the recorder
    mirror = window.mirror
    r = fit_rect(mirror.width(), mirror.height(), 540, 1200)
    x = int(r.x + r.w / 2)
    qtbot.mousePress(mirror, Qt.MouseButton.LeftButton, pos=QPoint(x, int(r.y + r.h * 0.8)))
    for i in range(1, 8):
        qtbot.wait(30)
        qtbot.mouseMove(mirror, QPoint(x, int(r.y + r.h * (0.8 - i * 0.08))))
    qtbot.wait(30)
    qtbot.mouseRelease(mirror, Qt.MouseButton.LeftButton, pos=QPoint(x, int(r.y + r.h * 0.24)))
    qtbot.waitUntil(lambda: runtime.calibration_counts()["swipe"] >= 1, timeout=3000)
    # more samples, as if the person kept going
    for ms in (70, 80, 90):
        for s in g.tap(500, 1500, ms):
            runtime.recorder.feed({g.TouchAction.DOWN: "down", g.TouchAction.UP: "up"}[s.action], s.x, s.y, 1000 + s.t_ms)
    for ms in (300, 400):
        for s in g.swipe(540, 1900, 540, 700, ms):
            runtime.recorder.feed({g.TouchAction.DOWN: "down", g.TouchAction.MOVE: "move", g.TouchAction.UP: "up"}[s.action], s.x, s.y, 5000 + s.t_ms)
    qtbot.mouseClick(panel.calib_finish_button, Qt.MouseButton.LeftButton)
    assert not runtime.recorder.active
    assert "탭 80" in panel.calib_profile.text() and "스와이프" in panel.calib_profile.text()
    assert (tmp_path / "gesture_profiles.yaml").exists()
    qtbot.mouseClick(panel.calib_reset_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: "기본값" in panel.calib_profile.text(), timeout=3000)


def test_wireless_qr_dialog_flow(qtbot, window: MainWindow, runtime: CoreRuntime) -> None:  # noqa: ANN001
    from phonepilot.core.wireless import CONNECT_SERVICE, PAIRING_SERVICE, WirelessADB

    class Stub:
        def __init__(self) -> None:
            self.service = None

        def wireless(self):  # noqa: ANN202
            async def run(args, timeout):  # noqa: ANN001, ANN202
                dlg = window._qr_dialog  # noqa: SLF001
                if args[:2] == ["mdns", "services"]:
                    # the phone 'scans' the QR: advertise whatever service name the dialog generated
                    svc = runtime._pairing_service  # noqa: SLF001
                    return 0, f"x\n{svc}\t{PAIRING_SERVICE}\t192.168.0.12:37123\nadb-A\t{CONNECT_SERVICE}\t192.168.0.12:41235\n"
                if args[0] == "pair":
                    return 0, "Successfully paired to 192.168.0.12:37123"
                return 0, "connected to 192.168.0.12:41235"

            return WirelessADB(run, poll_interval=0.01)

        def stop(self) -> None:
            pass

    real_pair = runtime.wireless_pair_qr

    async def spy(service, password, timeout=90):  # noqa: ANN001, ANN202
        runtime._pairing_service = service  # noqa: SLF001
        return await real_pair(service, password, timeout)

    old_manager = runtime.manager
    runtime.manager = Stub()
    runtime.wireless_pair_qr = spy  # type: ignore[method-assign]
    try:
        qtbot.mouseClick(window.device_panel.wireless_qr_button, Qt.MouseButton.LeftButton)
        dlg = window._qr_dialog  # noqa: SLF001
        assert dlg is not None and not dlg.qr.pixmap().isNull()
        qtbot.waitUntil(lambda: dlg.finished_ok is not None, timeout=5000)
        assert dlg.finished_ok is True and "연결됨" in dlg.status.text()
        assert dlg.close_button.isEnabled() and not dlg.cancel_button.isEnabled()
        dlg.accept()
    finally:
        runtime.manager = old_manager
        del runtime.wireless_pair_qr


def test_wireless_code_dialog_rejects_bad_input(qtbot, window: MainWindow) -> None:  # noqa: ANN001
    qtbot.mouseClick(window.device_panel.wireless_code_button, Qt.MouseButton.LeftButton)
    dlg = window._code_dialog  # noqa: SLF001
    dlg.address.setText("8.8.8.8:5555")
    dlg.code.setText("123456")
    dlg.buttons.accepted.emit()
    qtbot.waitUntil(lambda: dlg.status.text().startswith("✖"), timeout=5000)
    assert "사설 네트워크" in dlg.status.text()
    assert dlg.code.text() == ""  # the code is cleared right after use
    dlg.reject()


def test_help_is_behind_question_marks(qtbot, window: MainWindow) -> None:  # noqa: ANN001
    """Explanations live in balloons behind "?" buttons, not as paragraphs in the panels."""
    from phonepilot.gui.help_popup import HelpButton

    panel = window.device_panel
    buttons = panel.findChildren(HelpButton)
    assert len(buttons) == 4  # wireless, calibration, task recording, file drop
    assert not [lbl for lbl in panel.findChildren(QLabel) if lbl.objectName() == "helpText" and len(lbl.text()) > 40]
    b = next(x for x in buttons if "보정 시작" in x.help_text)
    qtbot.mouseClick(b, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: b.balloon is not None and b.balloon.isVisible(), timeout=2000)
    assert "보정 시작" in b.balloon.label.text()
    b.balloon.close()
    qtbot.waitUntil(lambda: b.balloon is None, timeout=2000)


def test_mirror_recovers_from_a_lost_frame_signal(qtbot, view: MirrorView, device: FakeDevice) -> None:  # noqa: ANN001
    """The stream keeps arriving but a frameReady delivery got lost: the status-timer watchdog redraws within a tick."""
    qtbot.waitUntil(lambda: view.frame_seq == device.framebus.latest().seq, timeout=2000)
    with view._lock:  # noqa: SLF001  simulate a delivery that never reached the GUI thread
        view._signal_pending = True  # noqa: SLF001
    device.goto("settings")  # new frame on the bus; _on_frame sees a pending signal and does not emit again
    qtbot.wait(200)
    assert view.frame_seq < device.framebus.latest().seq, "precondition: the view is frozen"
    view.tick_fps()  # first tick: notes that nothing was drawn
    view.tick_fps()  # second tick: still behind -> resync
    assert view.frame_seq == device.framebus.latest().seq
    device.goto("home")
    qtbot.waitUntil(lambda: view.frame_seq == device.framebus.latest().seq, timeout=2000)  # normal delivery works again
