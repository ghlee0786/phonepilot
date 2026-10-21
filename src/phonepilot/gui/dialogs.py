"""Approval dialog (PRD 13.3), human hand-off banner (18.3) and the skill inputs dialog."""

from __future__ import annotations

import time
from typing import Any

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from phonepilot.security.approvals import ApprovalRequest, ApprovalResponse, HumanRequest

CONFIRM_WORD = "승인"


def digits(s: str) -> str:
    return "".join(ch for ch in s if ch.isdigit())


class ApprovalDialog(QDialog):
    """Modal approval window. [거부] has the default focus; CONFIRM_HIGH needs a typed confirmation."""

    decided = Signal(object)  # ApprovalResponse, emitted exactly once

    def __init__(self, req: ApprovalRequest, parent: QWidget | None = None, *, timeout_s: float = 120) -> None:
        super().__init__(parent)
        self.req = req
        self._done = False
        self._deadline = time.monotonic() + timeout_s
        self.setWindowTitle("PhonePilot — 승인 요청")
        # non-modal (FR-HIL-002): the person may still use the phone while deciding; AI input is blocked by the core
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.setMinimumWidth(420)

        lay = QVBoxLayout(self)
        if req.high:
            band = QLabel("⚠ 돈 또는 계정 보안에 영향을 주는 행동입니다")
            band.setObjectName("highBand")
            band.setStyleSheet("background:#b91c1c;color:white;font-weight:bold;padding:8px;")
            lay.addWidget(band)
        title = QLabel(req.title or "AI가 다음 행동을 요청합니다")
        title.setWordWrap(True)
        title.setStyleSheet("font-size:15px;font-weight:bold;")
        lay.addWidget(title)

        if req.preview_png:
            pix = QPixmap()
            if pix.loadFromData(req.preview_png):
                prev = QLabel()
                prev.setPixmap(pix.scaled(380, 380, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
                prev.setAlignment(Qt.AlignmentFlag.AlignCenter)
                prev.setStyleSheet("background:#000;")
                lay.addWidget(prev)

        summary = QLabel(req.summary)
        summary.setWordWrap(True)
        summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(summary)

        who = f"요청 주체: {req.client}"
        if req.reason:
            who += f" — 이유: {req.reason}"
        who_lbl = QLabel(who)
        who_lbl.setWordWrap(True)
        lay.addWidget(who_lbl)

        d = req.decision
        reasons = d.reasons or list(req.details.get("reasons", []) or [])
        category = d.category or str(req.details.get("category", ""))
        meta = f"등급: {d.level.name} · 분류: {category}"
        if reasons:
            meta += " · 근거: " + ", ".join(str(r) for r in reasons)
        meta_lbl = QLabel(meta)
        meta_lbl.setWordWrap(True)
        meta_lbl.setStyleSheet("color:#6b7280;")
        lay.addWidget(meta_lbl)

        amount = req.expected_amount or (d.amounts[0] if d.amounts else None)
        if req.high:
            amt = QLabel(f"감지 금액: {amount}" if amount else "감지 금액: 확인 불가")
            amt.setStyleSheet("font-weight:bold;color:#b91c1c;")
            lay.addWidget(amt)

        self.confirm_edit: QLineEdit | None = None
        if req.high:
            if req.expected_amount:
                hint = f"확인을 위해 금액 숫자를 입력하세요 (예: {digits(req.expected_amount)})"
            else:
                hint = f"확인을 위해 '{CONFIRM_WORD}'을 입력하세요"
            lay.addWidget(QLabel(hint))
            self.confirm_edit = QLineEdit()
            self.confirm_edit.setPlaceholderText(digits(req.expected_amount) if req.expected_amount else CONFIRM_WORD)
            self.confirm_edit.textChanged.connect(self._update_approve)
            lay.addWidget(self.confirm_edit)

        self.stop_all_box = QCheckBox("모두 중지 (킬 스위치)")
        lay.addWidget(self.stop_all_box)

        self.countdown = QLabel()
        self.countdown.setStyleSheet("color:#6b7280;")
        lay.addWidget(self.countdown)

        row = QHBoxLayout()
        row.addStretch(1)
        self.deny_button = QPushButton("거부")
        self.approve_button = QPushButton("승인")
        self.approve_button.setAutoDefault(False)
        self.deny_button.setAutoDefault(True)
        self.deny_button.setDefault(True)
        row.addWidget(self.deny_button)
        row.addWidget(self.approve_button)
        lay.addLayout(row)
        self.deny_button.clicked.connect(lambda: self.deny())
        self.approve_button.clicked.connect(lambda: self.approve())
        self._update_approve()
        self.deny_button.setFocus(Qt.FocusReason.OtherFocusReason)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(500)
        self._tick()

    # ------------------------------------------------------------ logic
    def confirmation_ok(self) -> bool:
        if not self.req.high:
            return True
        text = self.confirm_edit.text() if self.confirm_edit is not None else ""
        if self.req.expected_amount:
            want = digits(self.req.expected_amount)
            return bool(want) and digits(text) == want
        return text.strip() in {CONFIRM_WORD, "approve"}

    def _update_approve(self) -> None:
        self.approve_button.setEnabled(self.confirmation_ok())

    def _tick(self) -> None:
        left = max(0, int(self._deadline - time.monotonic()))
        self.countdown.setText(f"{left}초 후 자동 거부")

    def _finish(self, resp: ApprovalResponse) -> None:
        if self._done:
            return
        self._done = True
        self._timer.stop()
        self.decided.emit(resp)

    def approve(self) -> None:
        if not self.confirmation_ok():
            return
        typed = self.confirm_edit.text() if self.confirm_edit is not None else ""
        self._finish(ApprovalResponse(True, typed_confirmation=typed, stop_all=self.stop_all_box.isChecked()))
        self.accept()

    def deny(self, note: str = "") -> None:
        self._finish(ApprovalResponse(False, note=note or "denied by user", stop_all=self.stop_all_box.isChecked()))
        self.reject()

    def close_silently(self) -> None:
        """Request was cancelled elsewhere (timeout, kill switch): just close."""
        self._done = True
        self._timer.stop()
        self.reject()

    def done(self, r: int) -> None:  # window X / Esc count as deny
        if not self._done:
            self._finish(ApprovalResponse(False, note="dialog closed", stop_all=self.stop_all_box.isChecked()))
        super().done(r)


class HumanRequestBanner(QFrame):
    """Non-modal bar on top of the mirror: '사람 차례: …' + [계속]/[중단]."""

    continued = Signal(object)  # HumanRequest
    aborted = Signal(object)

    KIND_LABELS = {
        "otp": "OTP 입력",
        "captcha": "보안 문자",
        "login": "로그인",
        "manual_step": "수동 단계",
        "unlock": "잠금 해제",
    }

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("humanBanner")
        self.setStyleSheet("#humanBanner{background:#fff7ed;border-bottom:2px solid #ea580c;} QLabel{color:#7c2d12;}")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 6, 10, 6)
        self.label = QLabel()
        self.label.setWordWrap(True)
        lay.addWidget(self.label, 1)
        self.continue_button = QPushButton("계속")
        self.abort_button = QPushButton("중단")
        lay.addWidget(self.continue_button)
        lay.addWidget(self.abort_button)
        self.continue_button.clicked.connect(lambda: self._emit(self.continued))
        self.abort_button.clicked.connect(lambda: self._emit(self.aborted))
        self.queue: list[HumanRequest] = []
        self.hide()

    @property
    def current(self) -> HumanRequest | None:
        return self.queue[0] if self.queue else None

    def kind_label(self, kind: str) -> str:
        return self.KIND_LABELS.get(kind, kind)

    def push(self, req: HumanRequest) -> None:
        self.queue.append(req)
        self._refresh()

    def remove(self, req_id: int) -> None:
        self.queue = [r for r in self.queue if r.id != req_id]
        self._refresh()

    def _refresh(self) -> None:
        req = self.current
        if req is None:
            self.hide()
            return
        more = f"  (+{len(self.queue) - 1})" if len(self.queue) > 1 else ""
        self.label.setText(f"✋ 사람 차례: {self.kind_label(req.kind)} — {req.instructions}{more}")
        self.show()

    def _emit(self, sig: Signal) -> None:  # type: ignore[type-arg]
        req = self.current
        if req is not None:
            sig.emit(req)  # type: ignore[attr-defined]


class SkillInputsDialog(QDialog):
    """Ask for workflow inputs as `key=value` lines."""

    def __init__(self, skill: dict[str, Any], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"워크플로 실행 — {skill.get('name', '')}")
        self.setMinimumWidth(420)
        lay = QVBoxLayout(self)
        desc = QLabel(str(skill.get("description", "")))
        desc.setWordWrap(True)
        lay.addWidget(desc)
        lay.addWidget(QLabel("입력값 (한 줄에 하나, key=value). 비밀값은 .env 키 이름만 적으세요."))
        lines = []
        for key, spec in (skill.get("inputs") or {}).items():
            default = spec.get("default") if isinstance(spec, dict) else None
            lines.append(f"{key}={'' if default is None else default}")
        self.edit = QPlainTextEdit("\n".join(lines))
        lay.addWidget(self.edit)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("실행")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("취소")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

    def inputs(self) -> dict[str, Any]:
        return parse_inputs(self.edit.toPlainText())


def parse_inputs(text: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if key and value != "":
            out[key] = value
    return out


class WirelessQrDialog(QDialog):
    """QR pairing for wireless debugging (PRD 8.2). Shows the QR and live status until the core finishes."""

    cancelled = Signal()

    def __init__(self, qr_png: bytes, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("PhonePilot — 무선 디버깅 QR 페어링")
        lay = QVBoxLayout(self)
        steps = QLabel(
            "1. 폰과 PC를 같은 Wi-Fi에 연결합니다.\n"
            "2. 폰: 설정 → 개발자 옵션 → 무선 디버깅 켜기 → 'QR 코드로 기기 페어링'.\n"
            "3. 아래 QR 코드를 폰으로 스캔하세요.\n"
            "공용 Wi-Fi에서는 사용하지 마세요(같은 네트워크의 다른 기기가 ADB 포트에 접근할 수 있습니다)."
        )
        steps.setWordWrap(True)
        lay.addWidget(steps)
        self.qr = QLabel()
        pix = QPixmap()
        pix.loadFromData(qr_png, "PNG")
        self.qr.setPixmap(pix)
        self.qr.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self.qr)
        self.status = QLabel("준비 중…")
        self.status.setWordWrap(True)
        lay.addWidget(self.status)
        row = QHBoxLayout()
        row.addStretch(1)
        self.cancel_button = QPushButton("취소")
        self.close_button = QPushButton("닫기")
        self.close_button.setEnabled(False)
        self.cancel_button.clicked.connect(self._cancel)
        self.close_button.clicked.connect(self.accept)
        row.addWidget(self.cancel_button)
        row.addWidget(self.close_button)
        lay.addLayout(row)
        self.finished_ok: bool | None = None

    def set_status(self, text: str) -> None:
        self.status.setText(text)

    def finish(self, ok: bool, message: str) -> None:
        self.finished_ok = ok
        self.status.setText(("✔ " if ok else "✖ ") + message)
        self.cancel_button.setEnabled(False)
        self.close_button.setEnabled(True)
        self.qr.setEnabled(False)

    def _cancel(self) -> None:
        self.cancelled.emit()
        self.reject()


class WirelessCodeDialog(QDialog):
    """Pairing-code flow: the phone shows IP:port and a 6-digit code under 'Pair device with pairing code'."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("PhonePilot — 페어링 코드로 연결")
        lay = QVBoxLayout(self)
        help_ = QLabel("폰: 개발자 옵션 → 무선 디버깅 → '페어링 코드로 기기 페어링'에 표시된 IP 주소·포트와 6자리 코드를 입력하세요.")
        help_.setWordWrap(True)
        lay.addWidget(help_)
        self.address = QLineEdit()
        self.address.setPlaceholderText("192.168.0.12:37123")
        self.code = QLineEdit()
        self.code.setPlaceholderText("123456")
        self.code.setMaxLength(6)
        self.code.setEchoMode(QLineEdit.EchoMode.Password)  # short-lived, but still a credential
        lay.addWidget(QLabel("IP 주소:포트"))
        lay.addWidget(self.address)
        lay.addWidget(QLabel("페어링 코드"))
        lay.addWidget(self.code)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        lay.addWidget(self.status)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("페어링")
        self.buttons.rejected.connect(self.reject)
        lay.addWidget(self.buttons)

    def values(self) -> tuple[str, str]:
        return self.address.text().strip(), self.code.text().strip()

    def clear_code(self) -> None:
        self.code.clear()
