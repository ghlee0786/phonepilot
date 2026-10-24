"""Stable error codes shared by GUI, MCP and timeline (PRD 22.1)."""

from __future__ import annotations

RETRYABLE = {
    "ADB_SERVER_CONFLICT",
    "DEVICE_NOT_FOUND",
    "DEVICE_UNAUTHORIZED",
    "DEVICE_OFFLINE",
    "SERVER_PUSH_FAILED",
    "SERVER_START_TIMEOUT",
    "ENCODER_FAILED",
    "DECODER_FAILED",
    "CONTROL_UNAVAILABLE",
    "UI_TREE_UNAVAILABLE",
    "ELEMENT_STALE",
    "ELEMENT_NOT_FOUND",
    "HUMAN_ACTIVE",
    "CONTROL_BUSY",
    "APPROVAL_INVALIDATED",
    "RATE_LIMITED",
    "WORKFLOW_STEP_FAILED",
    "APP_NOT_RUNNING",
    "UPDATE_CHECK_FAILED",
    "UPDATE_DOWNLOAD_FAILED",
}

HINTS = {
    "ADB_NOT_FOUND": "scripts/fetch_vendor.py 를 실행하거나 설정에서 adb 경로를 지정하세요",
    "DEVICE_NOT_FOUND": "USB 케이블과 USB 디버깅 설정을 확인하세요",
    "DEVICE_UNAUTHORIZED": "폰 화면에서 'USB 디버깅 허용'을 눌러 주세요",
    "INJECT_PERMISSION_DENIED": "샤오미 기기는 개발자 옵션의 'USB 디버깅(보안 설정)'을 켜야 합니다",
    "SECURE_SURFACE": "앱이 화면 캡처를 막고 있습니다. 폰에서 직접 조작하세요",
    "ELEMENT_STALE": "observe 를 다시 호출해 최신 요소 번호를 받으세요",
    "ELEMENT_NOT_FOUND": "scroll_to 로 요소를 찾거나 observe 로 화면을 다시 확인하세요",
    "HUMAN_ACTIVE": "사람이 조작 중이거나 AI가 일시정지 상태입니다. 사용자가 [AI 재개]를 누를 때까지 기다리세요",
    "CONTROL_BUSY": "다른 MCP 세션이 제어 중입니다",
    "KILL_SWITCH_ACTIVE": "사용자가 정지시켰습니다. 사용자가 [AI 허용]을 누르기 전에는 행동할 수 없습니다",
    "POLICY_BLOCKED": "정책상 허용되지 않는 행동입니다. 사용자에게 알리세요",
    "APPROVAL_DENIED": "사용자가 거부했습니다. 이유를 묻거나 작업을 중단하세요",
    "APPROVAL_TIMEOUT": "사용자가 응답하지 않았습니다",
    "APPROVAL_INVALIDATED": "승인 후 화면이 바뀌었습니다. observe 후 다시 시도하세요",
    "RATE_LIMITED": "잠시 후 다시 시도하세요",
    "LIMIT_EXCEEDED": "설정된 한도를 넘었습니다",
    "SECRET_NOT_FOUND": ".env 에 해당 키를 추가하세요",
    "SECRET_SCOPE_DENIED": "허용된 앱·호스트·비밀번호 필드에서만 입력할 수 있습니다",
    "USE_TYPE_SECRET": "비밀값은 type_secret(ref=...) 으로 입력하세요",
    "NON_ASCII_SECRET_UNSUPPORTED": "ADBKeyBoard를 설치하거나 사람이 직접 입력하세요",
    "STEP_BUDGET_EXCEEDED": "단계 예산을 넘었습니다. 상황을 사용자에게 보고하세요",
    "APP_NOT_RUNNING": "PhonePilot 앱을 실행하세요",
    "ORDER_STATE_UNKNOWN": "주문 내역을 조회해 확인하세요. 재제출하지 마세요",
    "UPDATE_CHECK_FAILED": "인터넷 연결을 확인하고 잠시 후 다시 시도하세요",
    "UPDATE_DOWNLOAD_FAILED": "인터넷 연결을 확인하고 다시 시도하세요",
    "UPDATE_CHECKSUM_MISMATCH": "내려받은 설치 파일이 손상됐거나 바뀌었습니다. 설치하지 않았습니다",
}


class PhonePilotError(Exception):
    def __init__(self, code: str, message: str = "", hint: str | None = None, **extra: object) -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message or code
        self.hint = hint if hint is not None else HINTS.get(code, "")
        self.retryable = code in RETRYABLE
        self.extra = extra

    def to_dict(self) -> dict[str, object]:
        data: dict[str, object] = {
            "code": self.code,
            "message": self.message,
            "hint": self.hint,
            "retryable": self.retryable,
        }
        data.update(self.extra)
        return data
