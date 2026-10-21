"""Side panel tabs (PRD 18.2): 타임라인 · 워크플로 · 승인 · 기기."""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from phonepilot.agent.timeline import TimelineEvent
from phonepilot.security.approvals import ApprovalRequest, ApprovalResponse

LEVEL_COLORS = {
    "BLOCK": "#dc2626",
    "CONFIRM_HIGH": "#ea580c",
    "CONFIRM": "#d97706",
    "HUMAN": "#9333ea",
    "ALLOW": "",
    "": "",
}
OK_RESULTS = {"ok", "approved", "continued", "waiting"}
MAX_TIMELINE_ITEMS = 2000

STATE_LABELS = {
    "PENDING": "대기",
    "RUNNING": "실행 중",
    "WAITING_HUMAN": "사람 차례",
    "WAITING_APPROVAL": "승인 대기",
    "PAUSED": "일시정지",
    "PAUSED_DISCONNECTED": "연결 끊김(일시정지)",
    "COMPLETED": "완료",
    "NEEDS_ATTENTION": "확인 필요",
    "FAILED": "실패",
    "ABORTED": "중단됨",
}


def actor_icon(actor: str) -> str:
    if actor == "human":
        return "👤"
    if actor == "system":
        return "⚙"
    if actor.startswith("workflow:"):
        return "🔁"
    return "🤖"


def format_event(ev: TimelineEvent) -> str:
    ts = datetime.fromtimestamp(ev.ts).strftime("%H:%M:%S")
    parts = [ts, actor_icon(ev.actor), ev.actor, ev.tool]
    if ev.target:
        parts.append(f"'{ev.target}'")
    if ev.level:
        parts.append(f"[{ev.level}]")
    parts.append(ev.result)
    if ev.elapsed_ms:
        parts.append(f"{ev.elapsed_ms}ms")
    return " ".join(parts)


class TimelinePanel(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        self.list = QListWidget()
        self.list.setWordWrap(True)
        lay.addWidget(self.list)
        row = QHBoxLayout()
        clear = QPushButton("목록 비우기")
        clear.clicked.connect(self.list.clear)
        row.addStretch(1)
        row.addWidget(clear)
        lay.addLayout(row)

    def add_event(self, ev: TimelineEvent) -> None:
        item = QListWidgetItem(format_event(ev))
        tip = [f"주체: {ev.actor}", f"도구: {ev.tool}", f"대상: {ev.target}", f"등급: {ev.level}", f"결과: {ev.result}"]
        if ev.reason:
            tip.append(f"이유: {ev.reason}")
        item.setToolTip("\n".join(tip))
        color = LEVEL_COLORS.get(ev.level, "")
        if ev.result not in OK_RESULTS and not ev.result.startswith(("found", "saved", "phone=")):
            color = "#dc2626"
        if color:
            item.setForeground(QBrush(QColor(color)))
        self.list.insertItem(0, item)  # newest first
        while self.list.count() > MAX_TIMELINE_ITEMS:
            self.list.takeItem(self.list.count() - 1)

    def add_system(self, text: str, level: str = "info") -> None:
        self.add_event(TimelineEvent("system", "notify", text, "", "ok" if level != "error" else "error"))


class WorkflowPanel(QWidget):
    controlRequested = Signal(str, str)  # run_id, action
    runRequested = Signal(dict)  # skill dict from engine.list()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)

        runs_box = QGroupBox("실행 중·최근 워크플로")
        rl = QVBoxLayout(runs_box)
        self.runs = QTreeWidget()
        self.runs.setHeaderLabels(["ID", "스킬", "상태", "현재 단계", "진행"])
        self.runs.setRootIsDecorated(False)
        self.runs.header().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        rl.addWidget(self.runs)
        btns = QHBoxLayout()
        self.pause_button = QPushButton("일시정지")
        self.resume_button = QPushButton("재개")
        self.abort_button = QPushButton("중단")
        for b, action in ((self.pause_button, "pause"), (self.resume_button, "resume"), (self.abort_button, "abort")):
            b.clicked.connect(lambda _=False, a=action: self._control(a))
            btns.addWidget(b)
        rl.addLayout(btns)
        lay.addWidget(runs_box, 1)

        skills_box = QGroupBox("스킬")
        sl = QVBoxLayout(skills_box)
        self.skills = QListWidget()
        sl.addWidget(self.skills)
        self.run_button = QPushButton("실행…")
        self.run_button.clicked.connect(self._run)
        self.skills.itemDoubleClicked.connect(lambda _i: self._run())
        sl.addWidget(self.run_button)
        lay.addWidget(skills_box, 1)
        self._items: dict[str, QTreeWidgetItem] = {}

    def set_skills(self, skills: list[dict[str, Any]]) -> None:
        self.skills.clear()
        for s in skills:
            label = s["name"] + ("  🔒" if s.get("sensitive") else "")
            item = QListWidgetItem(label)
            item.setToolTip(str(s.get("description", "")))
            item.setData(Qt.ItemDataRole.UserRole, s)
            self.skills.addItem(item)

    def update_run(self, run: dict[str, Any]) -> None:
        rid = str(run.get("run_id"))
        item = self._items.get(rid)
        if item is None:
            item = QTreeWidgetItem()
            self.runs.insertTopLevelItem(0, item)
            self._items[rid] = item
        state = str(run.get("state", ""))
        done = len(run.get("completed") or [])
        total = int(run.get("total_steps") or 0)
        item.setText(0, rid)
        item.setText(1, str(run.get("skill", "")))
        item.setText(2, STATE_LABELS.get(state, state))
        item.setText(3, str(run.get("current_step") or ""))
        item.setText(4, f"{done}/{total}" if total else str(done))
        item.setData(0, Qt.ItemDataRole.UserRole, rid)
        err = run.get("error")
        item.setToolTip(2, str(err) if err else state)
        if state in ("FAILED", "ABORTED"):
            item.setForeground(2, QBrush(QColor("#dc2626")))
        elif state in ("WAITING_HUMAN", "WAITING_APPROVAL", "PAUSED", "PAUSED_DISCONNECTED", "NEEDS_ATTENTION"):
            item.setForeground(2, QBrush(QColor("#d97706")))
        elif state == "COMPLETED":
            item.setForeground(2, QBrush(QColor("#16a34a")))
        if self.runs.currentItem() is None:
            self.runs.setCurrentItem(item)

    def selected_run(self) -> str | None:
        item = self.runs.currentItem()
        return None if item is None else str(item.data(0, Qt.ItemDataRole.UserRole))

    def _control(self, action: str) -> None:
        rid = self.selected_run()
        if rid:
            self.controlRequested.emit(rid, action)

    def _run(self) -> None:
        item = self.skills.currentItem()
        if item is not None:
            self.runRequested.emit(item.data(Qt.ItemDataRole.UserRole))


class ApprovalsPanel(QWidget):
    openRequested = Signal(int)  # approval id

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.addWidget(QLabel("대기 중"))
        self.pending = QListWidget()
        lay.addWidget(self.pending, 1)
        self.open_button = QPushButton("승인 창 열기")
        self.open_button.clicked.connect(self._open)
        self.pending.itemDoubleClicked.connect(lambda _i: self._open())
        lay.addWidget(self.open_button)
        lay.addWidget(QLabel("최근 기록"))
        self.history = QListWidget()
        lay.addWidget(self.history, 2)
        self._history_len = -1

    def set_pending(self, reqs: list[ApprovalRequest]) -> None:
        self.pending.clear()
        for r in reqs:
            item = QListWidgetItem(f"#{r.id} {r.decision.level.name} · {r.client} · {r.summary}")
            item.setData(Qt.ItemDataRole.UserRole, r.id)
            if r.high:
                item.setForeground(QBrush(QColor("#ea580c")))
            self.pending.addItem(item)

    def set_history(self, history: list[tuple[ApprovalRequest, ApprovalResponse]]) -> None:
        if len(history) == self._history_len:
            return
        self._history_len = len(history)
        self.history.clear()
        for req, resp in reversed(history):
            mark = "✔ 승인" if resp.approved else "✖ 거부"
            ts = datetime.fromtimestamp(time.time() - (time.monotonic() - req.created)).strftime("%H:%M:%S")
            item = QListWidgetItem(f"{ts} {mark} · {req.client} · {req.summary}" + (f" ({resp.note})" if resp.note else ""))
            item.setForeground(QBrush(QColor("#16a34a" if resp.approved else "#dc2626")))
            self.history.addItem(item)

    def _open(self) -> None:
        item = self.pending.currentItem() or self.pending.item(0)
        if item is not None:
            self.openRequested.emit(int(item.data(Qt.ItemDataRole.UserRole)))


class DevicePanel(QWidget):
    copyToken = Signal()
    copyUrl = Signal()
    reconnect = Signal()
    calibStart = Signal()
    wirelessQr = Signal()
    wirelessCode = Signal()
    wirelessScan = Signal()
    calibFinish = Signal()
    calibReset = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        info_box = QGroupBox("기기")
        form = QFormLayout(info_box)
        self.fields: dict[str, QLabel] = {}
        for key, label in (
            ("state", "연결 상태"),
            ("model", "모델"),
            ("android", "Android"),
            ("serial", "시리얼(끝자리)"),
            ("display", "디스플레이"),
            ("frame", "영상 프레임"),
            ("ime", "입력기"),
            ("nav", "내비게이션"),
        ):
            lbl = QLabel("-")
            lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            # long unbreakable values (IME component names) must not widen the dock
            lbl.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            self.fields[key] = lbl
            form.addRow(label, lbl)
        self.reconnect_button = QPushButton("다시 연결")
        self.reconnect_button.clicked.connect(self.reconnect.emit)
        form.addRow(self.reconnect_button)
        lay.addWidget(info_box)

        mcp_box = QGroupBox("AI 연결 (MCP)")
        ml = QVBoxLayout(mcp_box)
        self.mcp_url = QLabel("시작 전")
        self.mcp_url.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.mcp_url.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        ml.addWidget(self.mcp_url)
        row = QHBoxLayout()
        self.copy_url_button = QPushButton("URL 복사")
        self.copy_token_button = QPushButton("토큰 복사")
        self.copy_url_button.clicked.connect(self.copyUrl.emit)
        self.copy_token_button.clicked.connect(self.copyToken.emit)
        row.addWidget(self.copy_url_button)
        row.addWidget(self.copy_token_button)
        ml.addLayout(row)
        lay.addWidget(mcp_box)

        wl_box = QGroupBox("무선 연결 (Android 11+)")
        wl = QVBoxLayout(wl_box)
        wl_help = QLabel("케이블 없이 같은 Wi-Fi로 연결합니다. 처음 한 번 페어링하면 이후에는 자동으로 찾아 연결합니다.")
        wl_help.setWordWrap(True)
        wl.addWidget(wl_help)
        wrow = QHBoxLayout()
        self.wireless_qr_button = QPushButton("QR로 페어링")
        self.wireless_code_button = QPushButton("코드로 페어링")
        self.wireless_scan_button = QPushButton("무선 기기 찾기")
        self.wireless_qr_button.clicked.connect(self.wirelessQr.emit)
        self.wireless_code_button.clicked.connect(self.wirelessCode.emit)
        self.wireless_scan_button.clicked.connect(self.wirelessScan.emit)
        for b in (self.wireless_qr_button, self.wireless_code_button, self.wireless_scan_button):
            wrow.addWidget(b)
        wl.addLayout(wrow)
        self.wireless_status = QLabel("")
        self.wireless_status.setWordWrap(True)
        wl.addWidget(self.wireless_status)
        lay.addWidget(wl_box)

        cal_box = QGroupBox("제스처 보정")
        cl = QVBoxLayout(cal_box)
        self.calib_help = QLabel(
            "[보정 시작]을 누른 뒤 미러 화면에서 평소처럼 탭 5회, 길게 누르기 3회, 위아래 스와이프 5회 이상을 해 주세요. "
            "AI 제스처의 기본 속도·누름 시간·곡률이 내 손에 맞춰집니다(무작위 흔들림 없음)."
        )
        self.calib_help.setWordWrap(True)
        cl.addWidget(self.calib_help)
        self.calib_counts = QLabel("기록: -")
        cl.addWidget(self.calib_counts)
        self.calib_profile = QLabel("프로필: 기본값")
        self.calib_profile.setWordWrap(True)
        self.calib_profile.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        cl.addWidget(self.calib_profile)
        crow = QHBoxLayout()
        self.calib_start_button = QPushButton("보정 시작")
        self.calib_finish_button = QPushButton("완료·저장")
        self.calib_reset_button = QPushButton("기본값으로")
        self.calib_finish_button.setEnabled(False)
        self.calib_start_button.clicked.connect(self.calibStart.emit)
        self.calib_finish_button.clicked.connect(self.calibFinish.emit)
        self.calib_reset_button.clicked.connect(self.calibReset.emit)
        for b in (self.calib_start_button, self.calib_finish_button, self.calib_reset_button):
            crow.addWidget(b)
        cl.addLayout(crow)
        lay.addWidget(cal_box)

        pf_box = QGroupBox("사전 점검")
        pl = QVBoxLayout(pf_box)
        self.preflight = QTableWidget(0, 3)
        self.preflight.setHorizontalHeaderLabels(["항목", "상태", "상세"])
        self.preflight.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.preflight.verticalHeader().setVisible(False)
        self.preflight.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        pl.addWidget(self.preflight)
        lay.addWidget(pf_box, 1)

    def set_calibrating(self, active: bool, counts: dict[str, int] | None = None) -> None:
        self.calib_start_button.setEnabled(not active)
        self.calib_finish_button.setEnabled(active)
        if counts is not None:
            self.calib_counts.setText(
                f"기록: 탭 {counts.get('tap', 0)} · 길게 누르기 {counts.get('long_press', 0)} · 스와이프 {counts.get('swipe', 0)}"
                + ("  (기록 중…)" if active else "")
            )

    def set_profile(self, prof: dict[str, Any]) -> None:
        if not prof.get("calibrated"):
            self.calib_profile.setText("프로필: 기본값 (탭 70ms · 길게 800ms · 스와이프 300ms)")
            return
        speed = prof.get("swipe_px_per_ms") or 0
        self.calib_profile.setText(
            f"프로필: 탭 {prof.get('tap_ms')}ms · 길게 {prof.get('long_press_ms')}ms · "
            f"스와이프 {speed:.2f}px/ms · 곡률 {prof.get('arc')} · 속도 정점 {prof.get('peak')} · 멈춤 {prof.get('settle_ms')}ms"
        )

    def set_field(self, key: str, value: str) -> None:
        if key in self.fields:
            self.fields[key].setText(value or "-")
            self.fields[key].setToolTip(value)

    def set_preflight(self, rows: list[dict[str, str]]) -> None:
        icons = {"ok": "✔ 정상", "warn": "⚠ 주의", "fail": "✖ 실패", "info": "ℹ 정보"}
        colors = {"ok": "#16a34a", "warn": "#d97706", "fail": "#dc2626", "info": "#6b7280"}
        self.preflight.setRowCount(len(rows))
        for i, row in enumerate(rows):
            status = row.get("status", "")
            cells = [row.get("item", ""), icons.get(status, status), row.get("detail", "")]
            for j, text in enumerate(cells):
                cell = QTableWidgetItem(text)
                if j == 1 and status in colors:
                    cell.setForeground(QBrush(QColor(colors[status])))
                self.preflight.setItem(i, j, cell)
