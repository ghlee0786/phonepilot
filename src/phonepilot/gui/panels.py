"""Side panel tabs (PRD 18.2): 타임라인 · 워크플로 · 승인 · 기기."""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QFormLayout,
    QGridLayout,
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

from phonepilot import __version__
from phonepilot.agent.timeline import TimelineEvent
from phonepilot.gui.help_popup import HelpGroupBox
from phonepilot.gui.theme import COLORS, icon
from phonepilot.security.approvals import ApprovalRequest, ApprovalResponse

# text colours tuned for the dark list background (contrast >= 4.5:1)
ERROR_COLOR = "#f87171"
WARN_COLOR = "#fbbf24"
OK_COLOR = "#4ade80"
LEVEL_COLORS = {
    "BLOCK": ERROR_COLOR,
    "CONFIRM_HIGH": "#fb923c",
    "CONFIRM": WARN_COLOR,
    "HUMAN": "#c084fc",
    "ALLOW": "",
    "": "",
}
ACTOR_ICONS = {"human": ("user", "#86efac"), "system": ("system", COLORS["text_muted"]), "workflow": ("workflow", "#c084fc"), "ai": ("bot", "#93c5fd")}
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


def section_label(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setObjectName("sectionLabel")
    return lbl


def help_label(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setObjectName("helpText")
    lbl.setWordWrap(True)
    return lbl


def button_grid(*rows: list[QPushButton]) -> QGridLayout:
    """Rows of equal-width buttons; a single button spans the row. Keeps the narrow dock from scrolling sideways."""
    grid = QGridLayout()
    grid.setHorizontalSpacing(8)
    grid.setVerticalSpacing(8)
    for r, buttons in enumerate(rows):
        for c, b in enumerate(buttons):
            b.setMinimumWidth(0)
            b.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
            grid.addWidget(b, r, c, 1, 2 if len(buttons) == 1 else 1)
    return grid


def actor_kind(actor: str) -> str:
    if actor in ("human", "system"):
        return actor
    if actor.startswith("workflow:"):
        return "workflow"
    return "ai"


def format_event(ev: TimelineEvent) -> str:
    ts = datetime.fromtimestamp(ev.ts).strftime("%H:%M:%S")
    parts = [ts, ev.actor, ev.tool]
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
        lay.setContentsMargins(10, 10, 10, 10)
        self.list = QListWidget()
        self.list.setWordWrap(True)
        self.list.setIconSize(QSize(16, 16))
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.setTextElideMode(Qt.TextElideMode.ElideNone)
        self.list.setResizeMode(QListWidget.ResizeMode.Adjust)  # re-wrap long entries when the dock is resized
        lay.addWidget(self.list)
        row = QHBoxLayout()
        clear = QPushButton(icon("trash", COLORS["text_muted"]), "목록 비우기")
        clear.setObjectName("ghostButton")
        clear.clicked.connect(self.list.clear)
        row.addStretch(1)
        row.addWidget(clear)
        lay.addLayout(row)

    def add_event(self, ev: TimelineEvent) -> None:
        item = QListWidgetItem(format_event(ev))
        item.setIcon(icon(*ACTOR_ICONS[actor_kind(ev.actor)]))
        tip = [f"주체: {ev.actor}", f"도구: {ev.tool}", f"대상: {ev.target}", f"등급: {ev.level}", f"결과: {ev.result}"]
        if ev.reason:
            tip.append(f"이유: {ev.reason}")
        item.setToolTip("\n".join(tip))
        color = LEVEL_COLORS.get(ev.level, "")
        if ev.result not in OK_RESULTS and not ev.result.startswith(("found", "saved", "phone=")):
            color = ERROR_COLOR
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
        lay.setContentsMargins(10, 4, 10, 10)

        runs_box = QGroupBox("실행 중·최근 워크플로")
        rl = QVBoxLayout(runs_box)
        self.runs = QTreeWidget()
        self.runs.setHeaderLabels(["ID", "스킬", "상태", "현재 단계", "진행"])
        self.runs.setRootIsDecorated(False)
        self.runs.header().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        rl.addWidget(self.runs)
        btns = QHBoxLayout()
        self.pause_button = QPushButton(icon("pause"), "일시정지")
        self.resume_button = QPushButton(icon("play"), "재개")
        self.abort_button = QPushButton(icon("stop", ERROR_COLOR), "중단")
        for b, action in ((self.pause_button, "pause"), (self.resume_button, "resume"), (self.abort_button, "abort")):
            b.clicked.connect(lambda _=False, a=action: self._control(a))
            btns.addWidget(b)
        rl.addLayout(btns)
        lay.addWidget(runs_box, 1)

        skills_box = QGroupBox("스킬")
        sl = QVBoxLayout(skills_box)
        self.skills = QListWidget()
        sl.addWidget(self.skills)
        self.run_button = QPushButton(icon("play", "#ffffff"), "실행…")
        self.run_button.setObjectName("primaryButton")
        self.run_button.clicked.connect(self._run)
        self.skills.itemDoubleClicked.connect(lambda _i: self._run())
        sl.addWidget(self.run_button)
        lay.addWidget(skills_box, 1)
        self._items: dict[str, QTreeWidgetItem] = {}

    def set_skills(self, skills: list[dict[str, Any]]) -> None:
        self.skills.clear()
        for s in skills:
            item = QListWidgetItem(s["name"])
            item.setIcon(icon("lock", WARN_COLOR) if s.get("sensitive") else icon("workflow", COLORS["text_muted"]))
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
            item.setForeground(2, QBrush(QColor(ERROR_COLOR)))
        elif state in ("WAITING_HUMAN", "WAITING_APPROVAL", "PAUSED", "PAUSED_DISCONNECTED", "NEEDS_ATTENTION"):
            item.setForeground(2, QBrush(QColor(WARN_COLOR)))
        elif state == "COMPLETED":
            item.setForeground(2, QBrush(QColor(OK_COLOR)))
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
        lay.setContentsMargins(10, 4, 10, 10)
        lay.addWidget(section_label("대기 중"))
        self.pending = QListWidget()
        lay.addWidget(self.pending, 1)
        self.open_button = QPushButton(icon("open", "#ffffff"), "승인 창 열기")
        self.open_button.setObjectName("primaryButton")
        self.open_button.clicked.connect(self._open)
        self.pending.itemDoubleClicked.connect(lambda _i: self._open())
        lay.addWidget(self.open_button)
        lay.addWidget(section_label("최근 기록"))
        self.history = QListWidget()
        lay.addWidget(self.history, 2)
        self._history_len = -1

    def set_pending(self, reqs: list[ApprovalRequest]) -> None:
        self.pending.clear()
        for r in reqs:
            item = QListWidgetItem(f"#{r.id} {r.decision.level.name} · {r.client} · {r.summary}")
            item.setData(Qt.ItemDataRole.UserRole, r.id)
            if r.high:
                item.setForeground(QBrush(QColor(LEVEL_COLORS["CONFIRM_HIGH"])))
            self.pending.addItem(item)

    def set_history(self, history: list[tuple[ApprovalRequest, ApprovalResponse]]) -> None:
        if len(history) == self._history_len:
            return
        self._history_len = len(history)
        self.history.clear()
        for req, resp in reversed(history):
            mark = "승인" if resp.approved else "거부"
            ts = datetime.fromtimestamp(time.time() - (time.monotonic() - req.created)).strftime("%H:%M:%S")
            item = QListWidgetItem(f"{ts} {mark} · {req.client} · {req.summary}" + (f" ({resp.note})" if resp.note else ""))
            item.setIcon(icon("check", OK_COLOR) if resp.approved else icon("stop", ERROR_COLOR))
            item.setForeground(QBrush(QColor(OK_COLOR if resp.approved else ERROR_COLOR)))
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
    calibFidelity = Signal()
    recordStart = Signal()
    recordFinish = Signal()
    recordCancel = Signal()
    checkUpdate = Signal()
    reportProblem = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 4, 10, 10)
        lay.setSpacing(4)
        info_box = QGroupBox("기기")
        form = QFormLayout(info_box)
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(7)
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
            key_lbl = QLabel(label)
            key_lbl.setObjectName("fieldKey")
            form.addRow(key_lbl, lbl)
        self.reconnect_button = QPushButton(icon("refresh"), "다시 연결")
        self.reconnect_button.clicked.connect(self.reconnect.emit)
        form.addRow(self.reconnect_button)
        lay.addWidget(info_box)

        mcp_box = QGroupBox("AI 연결 (MCP)")
        ml = QVBoxLayout(mcp_box)
        self.mcp_url = QLabel("시작 전")
        self.mcp_url.setObjectName("monoValue")
        self.mcp_url.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.mcp_url.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        ml.addWidget(self.mcp_url)
        self.copy_url_button = QPushButton(icon("copy"), "URL 복사")
        self.copy_token_button = QPushButton(icon("copy"), "토큰 복사")
        self.copy_url_button.clicked.connect(self.copyUrl.emit)
        self.copy_token_button.clicked.connect(self.copyToken.emit)
        ml.addLayout(button_grid([self.copy_url_button, self.copy_token_button]))
        lay.addWidget(mcp_box)

        app_box = QGroupBox("앱 정보")
        al = QVBoxLayout(app_box)
        self.version_label = QLabel(f"PhonePilot {__version__}")
        al.addWidget(self.version_label)
        self.update_status = help_label("")
        self.update_status.setVisible(False)
        al.addWidget(self.update_status)
        self.update_button = QPushButton(icon("refresh"), "업데이트 확인")
        self.report_button = QPushButton(icon("alert"), "문제 신고")
        self.update_button.clicked.connect(self.checkUpdate.emit)
        self.report_button.clicked.connect(self.reportProblem.emit)
        al.addLayout(button_grid([self.update_button, self.report_button]))
        lay.addWidget(app_box)

        wl_box = HelpGroupBox(
            "무선 연결 (Android 11+)",
            "케이블 없이 같은 Wi-Fi로 연결합니다. 처음 한 번 페어링하면 이후에는 자동으로 찾아 연결합니다.\n"
            "공용 Wi-Fi에서는 쓰지 마세요.",
        )
        wl = QVBoxLayout(wl_box)
        self.wireless_qr_button = QPushButton(icon("qr"), "QR 페어링")
        self.wireless_code_button = QPushButton(icon("keypad"), "코드 페어링")
        self.wireless_scan_button = QPushButton(icon("wifi"), "페어링된 기기 찾기")
        self.wireless_scan_button.setToolTip("같은 Wi-Fi에서 페어링된 폰을 찾아 연결합니다")
        self.wireless_qr_button.clicked.connect(self.wirelessQr.emit)
        self.wireless_code_button.clicked.connect(self.wirelessCode.emit)
        self.wireless_scan_button.clicked.connect(self.wirelessScan.emit)
        wl.addLayout(button_grid([self.wireless_qr_button, self.wireless_code_button], [self.wireless_scan_button]))
        self.wireless_status = QLabel("")
        self.wireless_status.setWordWrap(True)
        wl.addWidget(self.wireless_status)
        lay.addWidget(wl_box)

        cal_box = HelpGroupBox(
            "제스처 보정",
            "[보정 시작]을 누른 뒤 미러 화면에서 평소처럼 탭 5회, 길게 누르기 3회, 위아래 스와이프 5회 이상을 해 주세요. "
            "AI 제스처의 기본 속도·누름 시간·곡률이 내 손에 맞춰집니다(무작위 흔들림 없음).\n\n"
            "[유사도 보기]: 마지막 보정 기록과 AI 제스처를 같은 기준으로 재서 점수로 보여 줍니다.",
        )
        cl = QVBoxLayout(cal_box)
        self.calib_counts = QLabel("기록: -")
        cl.addWidget(self.calib_counts)
        self.calib_profile = QLabel("프로필: 기본값")
        self.calib_profile.setWordWrap(True)
        self.calib_profile.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        cl.addWidget(self.calib_profile)
        self.calib_start_button = QPushButton(icon("hand", "#ffffff"), "보정 시작")
        self.calib_start_button.setObjectName("primaryButton")
        self.calib_finish_button = QPushButton(icon("check"), "완료·저장")
        self.calib_reset_button = QPushButton(icon("undo", COLORS["text_muted"]), "기본값으로")
        self.calib_reset_button.setObjectName("ghostButton")
        self.calib_finish_button.setEnabled(False)
        self.calib_start_button.clicked.connect(self.calibStart.emit)
        self.calib_finish_button.clicked.connect(self.calibFinish.emit)
        self.calib_reset_button.clicked.connect(self.calibReset.emit)
        self.calib_fidelity_button = QPushButton(icon("chart", COLORS["text_muted"]), "유사도 보기")
        self.calib_fidelity_button.setObjectName("ghostButton")
        self.calib_fidelity_button.clicked.connect(self.calibFidelity.emit)
        cl.addLayout(button_grid([self.calib_start_button, self.calib_finish_button], [self.calib_reset_button, self.calib_fidelity_button]))
        lay.addWidget(cal_box)

        rec_box = HelpGroupBox(
            "작업 녹화",
            "[녹화 시작]을 누르고 미러 화면에서 작업을 한 번 직접 해 주세요. 누른 버튼·스와이프·뒤로가기·기다린 시간이 "
            "스킬로 저장되고, 워크플로 탭에서 다시 실행하면 내 제스처 프로필로 같은 순서를 따라 합니다.\n\n"
            "입력한 글자는 저장하지 않고 실행할 때 다시 물어봅니다. 녹화는 이 PC에만 저장됩니다.",
        )
        rl = QVBoxLayout(rec_box)
        self.record_counts = QLabel("기록: -")
        rl.addWidget(self.record_counts)
        self.record_start_button = QPushButton(icon("record", "#ffffff"), "녹화 시작")
        self.record_start_button.setObjectName("primaryButton")
        self.record_finish_button = QPushButton(icon("check"), "완료·저장")
        self.record_cancel_button = QPushButton(icon("undo", COLORS["text_muted"]), "취소")
        self.record_cancel_button.setObjectName("ghostButton")
        self.record_finish_button.setEnabled(False)
        self.record_cancel_button.setEnabled(False)
        self.record_start_button.clicked.connect(self.recordStart.emit)
        self.record_finish_button.clicked.connect(self.recordFinish.emit)
        self.record_cancel_button.clicked.connect(self.recordCancel.emit)
        rl.addLayout(button_grid([self.record_start_button, self.record_finish_button], [self.record_cancel_button]))
        lay.addWidget(rec_box)

        pf_box = QGroupBox("사전 점검")
        pl = QVBoxLayout(pf_box)
        self.preflight = QTableWidget(0, 3)
        self.preflight.setHorizontalHeaderLabels(["항목", "상태", "상세"])
        self.preflight.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.preflight.verticalHeader().setVisible(False)
        self.preflight.setShowGrid(False)
        self.preflight.setMinimumHeight(160)
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

    def set_recording(self, active: bool, counts: dict[str, int] | None = None) -> None:
        self.record_start_button.setEnabled(not active)
        self.record_finish_button.setEnabled(active)
        self.record_cancel_button.setEnabled(active)
        if counts is not None:
            names = {"tap": "탭", "long_press": "길게", "swipe": "스와이프", "key": "키", "text": "입력"}
            parts = " · ".join(f"{names.get(k, k)} {v}" for k, v in counts.items()) or "없음"
            self.record_counts.setText(f"기록: {parts}" + ("  (녹화 중…)" if active else ""))

    def set_profile(self, prof: dict[str, Any]) -> None:
        if not prof.get("calibrated"):
            self.calib_profile.setText("프로필: 기본값 (탭 70ms · 길게 800ms · 스와이프 300ms)")
            return
        speed = prof.get("swipe_px_per_ms") or 0
        self.calib_profile.setText(
            f"프로필: 탭 {prof.get('tap_ms')}ms · 길게 {prof.get('long_press_ms')}ms · "
            f"스와이프 {speed:.2f}px/ms · 곡률 {prof.get('arc')} · 속도 정점 {prof.get('peak')} · 멈춤 {prof.get('settle_ms')}ms"
        )

    def set_update_status(self, text: str) -> None:
        self.update_status.setText(text)
        self.update_status.setVisible(bool(text))

    def set_field(self, key: str, value: str) -> None:
        if key in self.fields:
            self.fields[key].setText(value or "-")
            self.fields[key].setToolTip(value)

    def set_preflight(self, rows: list[dict[str, str]]) -> None:
        icons = {"ok": "정상", "warn": "주의", "fail": "실패", "info": "정보"}
        colors = {"ok": OK_COLOR, "warn": WARN_COLOR, "fail": ERROR_COLOR, "info": COLORS["text_muted"]}
        self.preflight.setRowCount(len(rows))
        for i, row in enumerate(rows):
            status = row.get("status", "")
            cells = [row.get("item", ""), icons.get(status, status), row.get("detail", "")]
            for j, text in enumerate(cells):
                cell = QTableWidgetItem(text)
                if j == 1 and status in colors:
                    cell.setForeground(QBrush(QColor(colors[status])))
                self.preflight.setItem(i, j, cell)
