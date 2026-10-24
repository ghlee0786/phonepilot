"""End-to-end scenarios through the real window: mouse/keyboard in the mirror -> calibration / task recording -> replay.

FR-INP-035..039, FR-INP-047, FR-SKL-014, FR-SKL-016. Everything goes through MirrorView -> InputPump -> InputArbiter,
the same path a person's mouse takes.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator

import pytest
import yaml
from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QInputDialog, QMessageBox

from phonepilot.core.calibration import ProfileStore
from phonepilot.gui.main_window import MainWindow
from phonepilot.gui.mirror_view import fit_rect
from phonepilot.runtime import CoreRuntime
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
def window(qtbot, runtime: CoreRuntime, tmp_path, monkeypatch) -> Iterator[MainWindow]:  # noqa: ANN001
    # keep the person's real profile, skills and approvals untouched
    reg = runtime.stack.registry
    monkeypatch.setattr(reg, "user_dir", tmp_path / "appdata" / "PhonePilot" / "skills")
    monkeypatch.setattr(reg, "approved_file", tmp_path / "appdata" / "PhonePilot" / "approved_skills.json")
    monkeypatch.setattr(reg, "recorded_dir", tmp_path / "recordings")
    runtime.profiles = ProfileStore(tmp_path / "gesture_profiles.yaml")
    dev = runtime.device
    assert isinstance(dev, FakeDevice)
    dev.goto("home", push=False)
    dev.stack.clear()
    win = MainWindow(runtime)
    qtbot.addWidget(win)
    win.show()
    qtbot.waitExposed(win)
    qtbot.waitUntil(lambda: win.mirror.image is not None, timeout=3000)
    yield win
    win.shutdown_ui()
    runtime.call_sync(_reset(runtime))


async def _reset(rt: CoreRuntime) -> None:
    from phonepilot.core.calibration import GestureProfile

    rt.stack.arbiter.release_lease()
    rt.stack.arbiter.allow_ai()
    rt.stack.arbiter.last_human_ts = 0.0
    if rt.task_recorder is not None:
        rt.finish_task_recording("", save=False)
    if rt.recorder.active:
        rt.recorder.stop()
    rt.stack.tools.gesture_profile = GestureProfile()


def pt(win: MainWindow, x: int, y: int) -> QPoint:
    """Display pixels (1080x2400) -> mirror widget position."""
    m = win.mirror
    r = fit_rect(m.width(), m.height(), 540, 1200)
    return QPoint(int(r.x + x / 2 * r.scale), int(r.y + y / 2 * r.scale))


def tap(qtbot, win: MainWindow, x: int, y: int, hold_ms: int = 90) -> None:  # noqa: ANN001
    qtbot.mousePress(win.mirror, Qt.MouseButton.LeftButton, pos=pt(win, x, y))
    qtbot.wait(hold_ms)
    qtbot.mouseRelease(win.mirror, Qt.MouseButton.LeftButton, pos=pt(win, x, y))


def drag(qtbot, win: MainWindow, x: int, y0: int, y1: int, move_ms: int = 300, rest_ms: int = 0, steps: int = 15) -> None:  # noqa: ANN001
    qtbot.mousePress(win.mirror, Qt.MouseButton.LeftButton, pos=pt(win, x, y0))
    for i in range(1, steps + 1):
        qtbot.wait(move_ms // steps)
        qtbot.mouseMove(win.mirror, pt(win, x, int(y0 + (y1 - y0) * i / steps)))
    if rest_ms:
        qtbot.wait(rest_ms)
    qtbot.mouseRelease(win.mirror, Qt.MouseButton.LeftButton, pos=pt(win, x, y1))


def busy_core(runtime: CoreRuntime, seconds: float) -> None:
    """Block the core loop, like a slow UI dump or decoder hiccup would."""
    runtime.loop.call_soon_threadsafe(time.sleep, seconds)


# ------------------------------------------------------------------ calibration


def test_s1_calibration_by_mouse_matches_what_the_person_did(qtbot, window: MainWindow, runtime: CoreRuntime) -> None:  # noqa: ANN001
    panel = window.device_panel
    qtbot.mouseClick(panel.calib_start_button, Qt.MouseButton.LeftButton)
    for _ in range(5):
        tap(qtbot, window, 540, 1500, hold_ms=100)
        qtbot.wait(150)
    for _ in range(3):
        tap(qtbot, window, 540, 1500, hold_ms=900)
        qtbot.wait(150)
    for _ in range(4):
        drag(qtbot, window, 540, 1900, 700, move_ms=400, rest_ms=150)
        qtbot.wait(200)
    qtbot.waitUntil(lambda: runtime.calibration_counts() == {"tap": 5, "long_press": 3, "swipe": 4}, timeout=5000)
    qtbot.mouseClick(panel.calib_finish_button, Qt.MouseButton.LeftButton)
    prof = runtime.stack.tools.gesture_profile
    assert prof.calibrated
    assert 70 <= prof.tap_ms <= 150, prof
    assert 850 <= prof.long_press_ms <= 1100, prof
    # 1200 px in ~400 ms of movement
    assert 2.0 <= prof.swipe_px_per_ms <= 4.0, prof
    assert 100 <= prof.settle_ms <= 250, prof


def test_s2_calibration_timing_survives_a_busy_core(qtbot, window: MainWindow, runtime: CoreRuntime) -> None:  # noqa: ANN001
    """The person's timing must come from the mouse events, not from when the core got round to them."""
    qtbot.mouseClick(window.device_panel.calib_start_button, Qt.MouseButton.LeftButton)
    for _ in range(3):
        busy_core(runtime, 0.4)  # core stalls while the person presses and releases
        tap(qtbot, window, 540, 1500, hold_ms=120)
        qtbot.wait(600)
    qtbot.waitUntil(lambda: runtime.calibration_counts()["tap"] == 3, timeout=5000)
    taps = [s.duration_ms for s in runtime.recorder.samples]
    runtime.finish_calibration(save=False)
    assert all(90 <= d <= 250 for d in taps), taps


def test_s3_ai_gestures_are_not_calibration_samples(qtbot, window: MainWindow, runtime: CoreRuntime) -> None:  # noqa: ANN001
    qtbot.mouseClick(window.device_panel.calib_start_button, Qt.MouseButton.LeftButton)
    runtime.call_sync(runtime.stack.tools.scroll(actor="claude-code", direction="down"))
    runtime.call_sync(runtime.stack.tools.tap(actor="claude-code", x_norm=500, y_norm=900))
    assert runtime.calibration_counts() == {"tap": 0, "long_press": 0, "swipe": 0}
    runtime.finish_calibration(save=False)


def test_s4_fidelity_dialog_after_calibration(qtbot, window: MainWindow, runtime: CoreRuntime, monkeypatch) -> None:  # noqa: ANN001
    shown: list[str] = []
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: shown.append(a[2]))
    qtbot.mouseClick(window.device_panel.calib_start_button, Qt.MouseButton.LeftButton)
    for _ in range(4):
        tap(qtbot, window, 540, 1500, hold_ms=100)
        qtbot.wait(120)
    for _ in range(3):
        drag(qtbot, window, 540, 1900, 700, move_ms=400, rest_ms=150)
        qtbot.wait(200)
    qtbot.waitUntil(lambda: runtime.calibration_counts()["swipe"] == 3, timeout=5000)
    qtbot.mouseClick(window.device_panel.calib_finish_button, Qt.MouseButton.LeftButton)
    qtbot.mouseClick(window.device_panel.calib_fidelity_button, Qt.MouseButton.LeftButton)
    assert shown and "유사도" in shown[0]
    rep = runtime.fidelity_report()
    assert rep["overall"] is not None and rep["overall"] >= 60, rep["text"]


# ------------------------------------------------------------------ task recording


def finish_recording(qtbot, window: MainWindow, monkeypatch, name: str) -> list[str]:  # noqa: ANN001
    answers = iter([(name, True), ("", True)])
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: next(answers))
    shown: list[str] = []
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: shown.append(a[2]))
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: shown.append("WARN " + a[2]))
    qtbot.mouseClick(window.device_panel.record_finish_button, Qt.MouseButton.LeftButton)
    return shown


def run_skill(runtime: CoreRuntime, name: str, inputs: dict | None = None) -> dict:
    async def go() -> dict:
        stack = runtime.stack
        stack.arbiter.last_human_ts = 0.0  # the demo is over; the person hands control to the AI
        stack.arbiter.release_lease()
        stack.arbiter.allow_ai()
        started = await stack.engine.start(name, inputs or {}, requested_by="test")
        for _ in range(300):
            st = stack.engine.status(started["run_id"])
            if st["state"] in {"COMPLETED", "FAILED", "ABORTED", "NEEDS_ATTENTION", "WAITING_HUMAN"}:
                return st
            await asyncio.sleep(0.1)
        return stack.engine.status(started["run_id"])

    return runtime.call_sync(go(), timeout=60)


def test_s5_record_navigation_and_replay(qtbot, window: MainWindow, runtime: CoreRuntime, monkeypatch, tmp_path) -> None:  # noqa: ANN001
    dev: FakeDevice = runtime.device  # type: ignore[assignment]
    qtbot.mouseClick(window.device_panel.record_start_button, Qt.MouseButton.LeftButton)
    qtbot.wait(400)
    tap(qtbot, window, 540, 460)  # 설정
    qtbot.waitUntil(lambda: dev.current == "settings", timeout=3000)
    qtbot.wait(1200)
    tap(qtbot, window, 540, 460)  # Google
    qtbot.waitUntil(lambda: dev.current == "google_signin", timeout=3000)
    qtbot.wait(600)
    qtbot.mouseClick(window.mirror, Qt.MouseButton.RightButton, pos=pt(window, 540, 1200))  # back
    qtbot.waitUntil(lambda: dev.current == "settings", timeout=3000)
    qtbot.wait(600)
    shown = finish_recording(qtbot, window, monkeypatch, "open google")
    assert shown and "recorded.open_google" in shown[0], shown

    path = tmp_path / "recordings" / "open_google.yaml"
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    actions = [(s.get("action"), s.get("args", {}).get("selector", {}).get("text")) for s in doc["steps"] if s.get("action") != "sleep"]
    assert actions == [("tap", "설정"), ("tap", "Google"), ("system_action", None)], doc["steps"]
    assert "recorded.open_google" in runtime.stack.registry.skills

    dev.goto("home", push=False)
    dev.stack.clear()
    st = run_skill(runtime, "recorded.open_google")
    assert st["state"] == "COMPLETED", st
    assert dev.current == "settings"


def test_s6_record_typing_never_stores_text_and_replays_with_input(qtbot, window: MainWindow, runtime: CoreRuntime, monkeypatch, tmp_path) -> None:  # noqa: ANN001
    dev: FakeDevice = runtime.device  # type: ignore[assignment]
    dev.goto("youtube_create", push=False)
    qtbot.mouseClick(window.device_panel.record_start_button, Qt.MouseButton.LeftButton)
    qtbot.wait(400)
    tap(qtbot, window, 540, 360)  # 이름 field
    qtbot.wait(500)
    window.mirror.setFocus()
    qtbot.keyClicks(window.mirror, "secret demo")
    qtbot.keyClick(window.mirror, Qt.Key.Key_Return)
    qtbot.waitUntil(lambda: dev.field_value("youtube_create", "com.google.android.youtube:id/name") == "secret demo", timeout=3000)
    qtbot.wait(300)
    shown = finish_recording(qtbot, window, monkeypatch, "name field")
    assert shown and "text_1" in shown[0], shown
    text = (tmp_path / "recordings" / "name_field.yaml").read_text(encoding="utf-8")
    assert "secret demo" not in text
    doc = yaml.safe_load(text)
    kinds = [s.get("action") for s in doc["steps"] if s.get("action") != "sleep"]
    assert kinds == ["tap", "type_text", "press_key"], doc["steps"]

    dev.values.clear()
    st = run_skill(runtime, "recorded.name_field", {"text_1": "새 이름"})
    assert st["state"] == "COMPLETED", st
    assert dev.field_value("youtube_create", "com.google.android.youtube:id/name") == "새 이름"


def test_s7_fast_taps_before_refresh_still_replay(qtbot, window: MainWindow, runtime: CoreRuntime, monkeypatch) -> None:  # noqa: ANN001
    dev: FakeDevice = runtime.device  # type: ignore[assignment]
    qtbot.mouseClick(window.device_panel.record_start_button, Qt.MouseButton.LeftButton)
    qtbot.wait(400)
    tap(qtbot, window, 540, 460)  # 설정
    tap(qtbot, window, 540, 460)  # Google, immediately
    qtbot.waitUntil(lambda: dev.current == "google_signin", timeout=3000)
    qtbot.wait(500)
    finish_recording(qtbot, window, monkeypatch, "fast taps")
    dev.goto("home", push=False)
    dev.stack.clear()
    st = run_skill(runtime, "recorded.fast_taps")
    assert st["state"] == "COMPLETED", st
    assert dev.current == "google_signin"


def test_s8_replay_uses_the_calibrated_profile(qtbot, window: MainWindow, runtime: CoreRuntime, monkeypatch) -> None:  # noqa: ANN001
    from phonepilot.core.calibration import GestureProfile

    dev: FakeDevice = runtime.device  # type: ignore[assignment]
    qtbot.mouseClick(window.device_panel.record_start_button, Qt.MouseButton.LeftButton)
    qtbot.wait(400)
    tap(qtbot, window, 540, 460)
    qtbot.waitUntil(lambda: dev.current == "settings", timeout=3000)
    qtbot.wait(500)
    finish_recording(qtbot, window, monkeypatch, "profile tap")

    runtime.stack.tools.gesture_profile = GestureProfile(calibrated=True, tap_ms=140.0)
    played: list = []
    orig = runtime.stack.arbiter.ai_gesture

    async def spy(steps):  # noqa: ANN001, ANN202
        played.append(steps)
        await orig(steps)

    monkeypatch.setattr(runtime.stack.arbiter, "ai_gesture", spy)
    dev.goto("home", push=False)
    dev.stack.clear()
    st = run_skill(runtime, "recorded.profile_tap")
    assert st["state"] == "COMPLETED", st
    assert played and played[0][-1].t_ms == pytest.approx(140.0)


def test_s9_cancel_recording_saves_nothing(qtbot, window: MainWindow, runtime: CoreRuntime, tmp_path) -> None:  # noqa: ANN001
    qtbot.mouseClick(window.device_panel.record_start_button, Qt.MouseButton.LeftButton)
    tap(qtbot, window, 540, 460)
    qtbot.wait(300)
    qtbot.mouseClick(window.device_panel.record_cancel_button, Qt.MouseButton.LeftButton)
    assert runtime.task_recorder is None and runtime.stack.arbiter.task_recorder is None
    assert not (tmp_path / "recordings").exists()
    assert window.device_panel.record_start_button.isEnabled()


# ------------------------------------------------------------------ file drop (FR-INP-053/054)


def drop_on_mirror(qtbot, window: MainWindow, paths: list) -> None:  # noqa: ANN001
    from PySide6.QtCore import QMimeData, QPointF, QUrl
    from PySide6.QtGui import QDragEnterEvent, QDropEvent

    md = QMimeData()
    md.setUrls([QUrl.fromLocalFile(str(x)) for x in paths])
    pos = pt(window, 540, 1200)
    enter = QDragEnterEvent(pos, Qt.DropAction.CopyAction, md, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    QApplication_send(window.mirror, enter)
    assert window.mirror.drop_hint, "the screen should show where the files go"
    drop = QDropEvent(QPointF(pos), Qt.DropAction.CopyAction, md, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    QApplication_send(window.mirror, drop)
    assert not window.mirror.drop_hint


def QApplication_send(widget, event) -> None:  # noqa: ANN001, N802
    from PySide6.QtWidgets import QApplication

    QApplication.sendEvent(widget, event)


def test_s10_drop_files_and_folder_onto_the_phone(qtbot, window: MainWindow, runtime: CoreRuntime, tmp_path, monkeypatch) -> None:  # noqa: ANN001
    dev: FakeDevice = runtime.device  # type: ignore[assignment]
    dev.pushed.clear()
    (tmp_path / "a.jpg").write_bytes(b"jpg")
    folder = tmp_path / "trip"
    (folder / "day1").mkdir(parents=True)
    (folder / "day1" / "b.txt").write_text("b", encoding="utf-8")
    dev.pushed["/sdcard/Download/a.jpg"] = b"already there"
    drop_on_mirror(qtbot, window, [tmp_path / "a.jpg", folder])
    qtbot.waitUntil(lambda: "복사했습니다" in window.device_panel.drop_status.text(), timeout=5000)
    assert dev.pushed["/sdcard/Download/a (1).jpg"] == b"jpg"  # never overwrites the phone's file
    assert dev.pushed["/sdcard/Download/a.jpg"] == b"already there"
    assert dev.pushed["/sdcard/Download/trip/day1/b.txt"] == b"b"
    assert any("MEDIA_SCANNER_SCAN_FILE" in c and "a (1).jpg" in c for c in dev.shell_log)
    assert "/sdcard/Download/a (1).jpg" not in runtime.stack.tools.pushed_files  # the person's files stay after the session


def test_s11_drop_folder_setting_is_limited_to_shared_storage(qtbot, window: MainWindow, runtime: CoreRuntime, tmp_path, monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))  # set_drop_dir saves config.yaml
    warned: list[str] = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warned.append(a[2]))
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("/data/data/com.example", True))
    qtbot.mouseClick(window.device_panel.drop_dir_button, Qt.MouseButton.LeftButton)
    assert warned and runtime.config.transfer.drop_dir == "/sdcard/Download"
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("/sdcard/Pictures/PhonePilot/", True))
    qtbot.mouseClick(window.device_panel.drop_dir_button, Qt.MouseButton.LeftButton)
    try:
        assert runtime.config.transfer.drop_dir == "/sdcard/Pictures/PhonePilot"
        assert window.device_panel.drop_dir.text() == "/sdcard/Pictures/PhonePilot"
        assert "drop_dir: /sdcard/Pictures/PhonePilot" in (tmp_path / "roaming" / "PhonePilot" / "config.yaml").read_text(encoding="utf-8")
        dev: FakeDevice = runtime.device  # type: ignore[assignment]
        (tmp_path / "c.png").write_bytes(b"png")
        drop_on_mirror(qtbot, window, [tmp_path / "c.png"])
        qtbot.waitUntil(lambda: "/sdcard/Pictures/PhonePilot/c.png" in dev.pushed, timeout=5000)
    finally:
        runtime.config.transfer.drop_dir = "/sdcard/Download"


def test_s12_dropped_apk_asks_before_installing(qtbot, window: MainWindow, runtime: CoreRuntime, tmp_path, monkeypatch) -> None:  # noqa: ANN001
    dev: FakeDevice = runtime.device  # type: ignore[assignment]
    apk = tmp_path / "tool.apk"
    apk.write_bytes(b"PK")
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Cancel)
    drop_on_mirror(qtbot, window, [apk])
    qtbot.wait(300)
    assert not any(c.startswith("pm install") for c in dev.shell_log[-5:])
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    drop_on_mirror(qtbot, window, [apk])
    qtbot.waitUntil(lambda: any(c.startswith("pm install -r /data/local/tmp/pp_drop_") for c in dev.shell_log), timeout=5000)
    qtbot.waitUntil(lambda: any(c.startswith("rm -f /data/local/tmp/pp_drop_") for c in dev.shell_log), timeout=5000)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.No)
    drop_on_mirror(qtbot, window, [apk])
    qtbot.waitUntil(lambda: "/sdcard/Download/tool.apk" in dev.pushed, timeout=5000)  # copied, not installed


# ------------------------------------------------------------------ health (PRD 25)


def test_s13_after_self_recovery_the_mirror_follows_the_new_session(qtbot, window: MainWindow, runtime: CoreRuntime) -> None:  # noqa: ANN001
    old = runtime.device
    old.decoder_dead = True
    runtime.call_sync(runtime.check_health(), timeout=30)
    qtbot.waitUntil(lambda: runtime.device is not old and window.mirror._device is runtime.device, timeout=5000)  # noqa: SLF001
    new = runtime.device
    new.goto("settings")
    qtbot.waitUntil(lambda: window.mirror.frame_seq == new.framebus.latest().seq, timeout=3000)
    qtbot.waitUntil(lambda: "최근 장애" in window.device_panel.health_incidents.text(), timeout=3000)
    assert "영상 디코더가 멈춤" in window.device_panel.health_incidents.text()


def test_s14_reconnect_button_works_while_streaming(qtbot, window: MainWindow, runtime: CoreRuntime) -> None:  # noqa: ANN001
    window.refresh_status()
    btn = window.device_panel.reconnect_button
    assert btn.isEnabled(), "a frozen session still looks connected; the person must be able to replace it"
    old = runtime.device
    qtbot.mouseClick(btn, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: runtime.device is not old and runtime.state.value == "STREAMING", timeout=10000)
    qtbot.waitUntil(lambda: window.mirror._device is runtime.device, timeout=3000)  # noqa: SLF001


def test_s15_check_now_shows_the_state(qtbot, window: MainWindow, runtime: CoreRuntime) -> None:  # noqa: ANN001
    qtbot.mouseClick(window.device_panel.health_check_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: window.device_panel.health_status.text() == "✔ 정상", timeout=5000)


def test_s16_mirror_reattaches_if_it_was_left_on_an_old_session(qtbot, window: MainWindow, runtime: CoreRuntime) -> None:  # noqa: ANN001
    from phonepilot.testing.fake_device import FakeDevice, demo_screens

    stale = FakeDevice(demo_screens(), "home")
    window.mirror.attach(stale)  # e.g. a missed state event
    window.refresh_status()
    assert window.mirror._device is runtime.device  # noqa: SLF001


def test_s17_badge_never_says_connected_while_the_stream_is_broken(qtbot, window: MainWindow, runtime: CoreRuntime) -> None:  # noqa: ANN001
    """The person must see that something is wrong even when the USB link itself is still up."""
    window.refresh_status()
    qtbot.waitUntil(lambda: "연결됨" in window.conn_badge.text(), timeout=3000)
    window._ev_health({"state": "STREAMING", "recovering": False, "codes": ["WAITING_KEYFRAME"], "problems": ["영상이 키 프레임을 기다리며 멈춤"]})  # noqa: SLF001
    assert window.conn_badge.text().startswith("⚠") and "연결됨" not in window.conn_badge.text()
    window._ev_health({"state": "STREAMING", "recovering": True, "codes": ["NO_FRAME_AFTER_RESET"], "problems": []})  # noqa: SLF001
    assert window.conn_badge.text() == "⚠ 복구 중" and window.mirror.offline_notice
    window._ev_health({"state": "STREAMING", "recovering": False, "codes": ["VIDEO_STALLED"], "problems": []})  # noqa: SLF001
    assert "연결됨" in window.conn_badge.text() and not window.mirror.offline_notice  # a quiet screen is not an alarm
