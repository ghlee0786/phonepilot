"""PolicyEngine: classifies every AI action as ALLOW / CONFIRM / CONFIRM_HIGH / BLOCK (PRD 13.1-13.2, 13.9).

Classification is independent of the model: it looks at the tool, the foreground package,
the label of the element the action lands on, and the text on the screen.
When signals disagree the stricter level wins.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import Any

import yaml


class Level(IntEnum):
    ALLOW = 0
    CONFIRM = 1
    CONFIRM_HIGH = 2
    BLOCK = 3


DEFAULT_KEYWORDS: dict[str, list[str]] = {
    "payment": [
        "구매", "결제", "결제하기", "주문하기", "유료 체험 시작", "무료 체험 시작", "충전", "한 번에 구매", "정기결제", "구독 시작",
        "buy", "purchase", "pay", "checkout", "place order", "start trial", "start free trial", "1-tap buy", "subscribe now",
    ],
    "trading": ["매수", "매도", "시장가", "지정가", "주문 확인", "정정", "sell", "confirm order", "limit order", "market order"],
    "transfer": ["송금", "이체", "출금", "보내기 확인", "입금 주소", "send money", "transfer", "withdraw", "withdrawal"],
    "account_security": [
        "비밀번호 변경", "2단계 인증 사용 중지", "2단계 인증 사용 설정", "2단계 인증 사용", "사용 중지", "계정 삭제", "복구 전화번호",
        "복구 이메일", "모든 기기에서 로그아웃", "삭제", "change password", "turn off 2-step", "turn on 2-step", "delete account",
        "recovery phone", "recovery email", "sign out of all",
    ],
    "engagement": ["좋아요", "싫어요", "구독", "댓글", "댓글 달기", "공유", "like", "dislike", "subscribe", "comment", "share"],
    "outbound": ["보내기", "전송", "게시", "업로드", "공개", "send", "post", "upload", "publish", "reply"],
    "commit": ["다음", "계속", "확인", "동의", "동의합니다", "next", "continue", "ok", "confirm", "agree", "i agree"],
    "phone_2sv": ["전화번호 추가", "휴대전화 추가", "전화번호 사용", "add phone", "add a phone", "use phone number", "음성 또는 문자 메시지", "voice or text message"],
    "captcha": ["로봇이 아닙니다", "보안문자", "captcha", "i'm not a robot", "recaptcha", "자동입력 방지"],
    "permission_allow": ["허용", "앱 사용 중에만 허용", "이번만 허용", "allow", "while using the app", "only this time"],
}

TWO_SV_CONTEXT = ["2단계 인증", "2-step verification", "2단계", "2-step"]
DEFAULT_SENSITIVE_APPS = {
    "packages": [
        "com.android.vending",  # Play Store purchase sheets
        "viva.republica.toss",
    ],
    "label_patterns": ["은행", "뱅크", "뱅킹", "증권", "업비트", "빗썸", "코인원", "코빗", "페이", "bank", "securities", "upbit", "bithumb", "pay"],
}
PERMISSION_PACKAGES = {"com.google.android.permissioncontroller", "com.android.permissioncontroller", "com.android.packageinstaller"}
ENGAGEMENT_PACKAGES = {"com.google.android.youtube", "com.instagram.android", "com.zhiliaoapp.musically", "com.ss.android.ugc.trill"}
AMOUNT = re.compile(r"(?:₩|\\|KRW|US\$|\$)\s?[\d,]+(?:\.\d+)?|[\d,]+(?:\.\d+)?\s?(?:원|KRW|USDT|달러)")
READ_ONLY_TOOLS = {
    "observe", "screenshot", "ui_tree", "find_elements", "zoom", "get_device_info", "get_foreground_app", "list_apps",
    "read_notifications", "wait_for", "wait_stable", "sleep", "list_secret_refs", "list_skills", "get_skill",
    "workflow_status", "session_info", "notify_user", "verify_2sv_methods", "get_gesture_profile",
}
ALWAYS_HIGH_TOOLS = {"install_apk", "order_submit", "order_cancel"}
GESTURE_TOOLS = {"tap", "double_tap", "long_press", "drag", "type_text", "type_secret", "type_profile", "press_key"}


@dataclass
class ActionContext:
    tool: str
    package: str = ""
    app_label: str = ""
    target_label: str = ""
    target_role: str = ""
    screen_texts: list[str] = field(default_factory=list)
    secure_surface: bool = False
    in_workflow: bool = False
    in_batch: bool = False
    coordinate_tap: bool = False
    args: dict[str, Any] = field(default_factory=dict)


@dataclass
class PolicyDecision:
    level: Level
    category: str = "general"
    reasons: list[str] = field(default_factory=list)
    amounts: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)  # every signal that fired, not only the strictest one
    auto_approved: bool = False

    @property
    def needs_approval(self) -> bool:
        return self.level in (Level.CONFIRM, Level.CONFIRM_HIGH)

    @property
    def permission_only(self) -> bool:
        """A runtime-permission "allow" in the system dialog and nothing else (auto-approval mode)."""
        return self.level is Level.CONFIRM and set(self.categories) == {"permission"}

    def to_dict(self) -> dict[str, Any]:
        return {"level": self.level.name, "category": self.category, "reasons": self.reasons, "amounts": self.amounts}


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip().lower())


def _match(label: str, words: list[str]) -> str | None:
    lab = _norm(label)
    if not lab:
        return None
    for w in words:
        wn = _norm(w)
        if re.search(r"[a-z]", wn):
            if re.search(rf"(?<![a-z]){re.escape(wn)}(?![a-z])", lab):
                return w
        elif wn in lab:
            return w
    return None


class PolicyEngine:
    def __init__(
        self,
        keywords: dict[str, list[str]] | None = None,
        sensitive_apps: dict[str, list[str]] | None = None,
    ) -> None:
        self.keywords = {k: list(v) for k, v in DEFAULT_KEYWORDS.items()}
        for k, v in (keywords or {}).items():  # user additions extend, never remove (PRD 13.2)
            self.keywords.setdefault(k, [])
            self.keywords[k].extend(w for w in v if w not in self.keywords[k])
        apps = sensitive_apps or DEFAULT_SENSITIVE_APPS
        self.sensitive_packages: set[str] = set(DEFAULT_SENSITIVE_APPS["packages"]) | set(apps.get("packages", []))
        self.sensitive_label_patterns: list[str] = list(
            dict.fromkeys(DEFAULT_SENSITIVE_APPS["label_patterns"] + list(apps.get("label_patterns", [])))
        )

    @classmethod
    def from_files(cls, keywords_file: Path | None, sensitive_file: Path | None) -> PolicyEngine:
        kw = yaml.safe_load(keywords_file.read_text(encoding="utf-8")) if keywords_file and keywords_file.exists() else None
        sa = yaml.safe_load(sensitive_file.read_text(encoding="utf-8")) if sensitive_file and sensitive_file.exists() else None
        return cls(kw, sa)

    def is_sensitive_app(self, package: str, app_label: str = "") -> bool:
        if package in self.sensitive_packages:
            return True
        label = _norm(app_label)
        return bool(label) and any(_norm(p) in label for p in self.sensitive_label_patterns)

    def add_sensitive_package(self, package: str) -> None:
        self.sensitive_packages.add(package)

    def classify(self, ctx: ActionContext) -> PolicyDecision:
        if ctx.tool in READ_ONLY_TOOLS:
            return PolicyDecision(Level.ALLOW, "read_only")
        d = PolicyDecision(Level.ALLOW)

        def bump(level: Level, category: str, reason: str) -> None:
            if level > d.level:
                d.level = level
                d.category = category
            d.reasons.append(reason)
            d.categories.append(category)

        screen = " \n ".join(ctx.screen_texts)
        screen_norm = _norm(screen)
        label = ctx.target_label
        d.amounts = AMOUNT.findall(screen)[:5]

        if ctx.tool in ALWAYS_HIGH_TOOLS:
            bump(Level.CONFIRM_HIGH, "money_or_install", f"tool {ctx.tool} always needs strong approval")

        if _match(screen, self.keywords["captcha"]) and ctx.tool in GESTURE_TOOLS | {"swipe", "scroll"}:
            bump(Level.BLOCK, "captcha", "CAPTCHA screens are for humans only")

        acts_on_target = ctx.tool in GESTURE_TOOLS or ctx.tool in {"batch_actions"}
        if acts_on_target and label:
            if w := _match(label, self.keywords["transfer"]):
                bump(Level.BLOCK, "transfer", f"transfers/withdrawals are never automated ('{w}')")
            if (w := _match(label, self.keywords["phone_2sv"])) and any(c.lower() in screen_norm for c in TWO_SV_CONTEXT):
                bump(Level.BLOCK, "phone_2sv", f"phone-based 2-step verification is forbidden ('{w}')")
            if w := _match(label, self.keywords["payment"]):
                bump(Level.CONFIRM_HIGH, "payment", f"payment action '{w}'")
            if w := _match(label, self.keywords["trading"]):
                bump(Level.CONFIRM_HIGH, "trading", f"trading action '{w}'")
            if w := _match(label, self.keywords["account_security"]):
                bump(Level.CONFIRM_HIGH, "account_security", f"account security change '{w}'")
            if d.amounts and _match(label, self.keywords["commit"]):
                bump(Level.CONFIRM_HIGH, "payment", f"commit button on a screen showing an amount ({d.amounts[0]})")
            if w := _match(label, self.keywords["engagement"]):
                is_paid_sub = w in ("구독", "subscribe") and d.amounts and ctx.package not in ENGAGEMENT_PACKAGES
                if is_paid_sub:
                    bump(Level.CONFIRM_HIGH, "payment", "paid subscription")
                elif ctx.in_workflow or ctx.in_batch:
                    bump(Level.BLOCK, "engagement", f"engagement action '{w}' inside an automated sequence (PRD 13.9)")
                else:
                    bump(Level.CONFIRM, "engagement", f"engagement action '{w}'")
            if w := _match(label, self.keywords["outbound"]):
                bump(Level.CONFIRM, "outbound", f"sends or publishes something ('{w}')")
            if ctx.package in PERMISSION_PACKAGES and (w := _match(label, self.keywords["permission_allow"])):
                bump(Level.CONFIRM, "permission", f"grants a permission ('{w}')")

        if acts_on_target or ctx.tool in {"swipe", "scroll", "fling", "pinch"}:
            if self.is_sensitive_app(ctx.package, ctx.app_label) and ctx.tool not in {"scroll", "swipe", "fling", "pinch"}:
                bump(Level.CONFIRM, "sensitive_app", f"sensitive app {ctx.app_label or ctx.package}")
            if ctx.secure_surface and ctx.coordinate_tap:
                bump(Level.CONFIRM, "secure_surface", "coordinate tap on a screen that blocks capture")

        if ctx.tool == "type_secret" and ctx.args.get("kind") == "pin":
            bump(Level.CONFIRM_HIGH, "pin", "PIN entry")

        if ctx.tool == "open_url":
            url = str(ctx.args.get("url", ""))
            if not re.match(r"^https?://", url, re.IGNORECASE):
                bump(Level.BLOCK, "url_scheme", "only http(s) URLs may be opened")

        if not d.reasons:
            d.reasons.append("no sensitive signal")
        return d
