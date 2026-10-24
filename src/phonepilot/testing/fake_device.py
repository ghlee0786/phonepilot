"""FakeDevice (PRD 23.2): a scriptable phone for tests and the GUI demo mode.

Screens are UI-tree states; taps on an element follow a transition to the next screen.
Control messages are decoded exactly like the scrcpy server would, so the whole input
path (gesture synthesis -> serialization -> coordinates) is exercised.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, field
from xml.sax.saxutils import quoteattr

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from phonepilot.core.device import DeviceBackend, DeviceInfo
from phonepilot.core.framebus import FrameBus
from phonepilot.core.scrcpy import protocol as p


@dataclass
class FakeNode:
    text: str = ""
    rid: str = ""
    cls: str = "android.widget.TextView"
    bounds: tuple[int, int, int, int] = (0, 0, 10, 10)
    clickable: bool = False
    password: bool = False
    scrollable: bool = False
    checkable: bool = False
    checked: bool = False
    desc: str = ""
    goto: str | None = None  # screen to switch to on tap

    @property
    def is_field(self) -> bool:
        return "EditText" in self.cls


@dataclass
class FakeScreen:
    name: str
    package: str
    activity: str = ".Main"
    nodes: list[FakeNode] = field(default_factory=list)
    secure: bool = False  # simulate FLAG_SECURE: black frames


_FONTS: dict[int, ImageFont.ImageFont | ImageFont.FreeTypeFont] = {}


def _font(size: int) -> ImageFont.ImageFont | ImageFont.FreeTypeFont:
    """Korean-capable font for the demo screens (Malgun Gothic on Windows), cached per size."""
    if size not in _FONTS:
        for name in ("malgun.ttf", "NanumGothic.ttf", "AppleSDGothicNeo.ttc", "NotoSansCJK-Regular.ttc"):
            try:
                _FONTS[size] = ImageFont.truetype(name, size)
                break
            except OSError:
                continue
        else:
            _FONTS[size] = ImageFont.load_default()
    return _FONTS[size]


def button(text: str, y: int, goto: str | None = None, rid: str = "", x0: int = 60, x1: int = 1020, h: int = 120) -> FakeNode:
    return FakeNode(text=text, rid=rid, cls="android.widget.Button", bounds=(x0, y, x1, y + h), clickable=True, goto=goto)


def field_node(hint: str, y: int, rid: str, password: bool = False) -> FakeNode:
    return FakeNode(text="", rid=rid, cls="android.widget.EditText", bounds=(60, y, 1020, y + 130), clickable=True, password=password, desc=hint)


def label(text: str, y: int, h: int = 80) -> FakeNode:
    return FakeNode(text=text, bounds=(60, y, 1020, y + h))


class FakeDevice(DeviceBackend):
    def __init__(
        self,
        screens: list[FakeScreen],
        start: str,
        display: tuple[int, int] = (1080, 2400),
        frame: tuple[int, int] = (540, 1200),
    ) -> None:
        self.screens = {s.name: s for s in screens}
        self.current = start
        self.stack: list[str] = []
        self._display = display
        self._frame = frame
        self.framebus = FrameBus()
        self.info = DeviceInfo(serial_hash="fake", serial_tail="FAKE", manufacturer="PhonePilot", model="FakePhone", sdk=34, release="14", display_w=display[0], display_h=display[1], density=420, ime="com.google.android.inputmethod.latin/.LatinIME", nav_mode="gesture")
        self.sent: list[bytes] = []
        self.decoded: list[dict[str, object]] = []
        self.values: dict[tuple[str, str], str] = {}  # (screen, rid) -> text
        self.focus: tuple[str, str] | None = None
        self.clipboard = ""
        self.paste_delay_s = 0.0  # > 0: the "app" reads the clipboard this long after the paste key, like a busy real app
        self.shell_log: list[str] = []
        self.pushed: dict[str, bytes] = {}
        self.ui_dump_failures = 0
        self.taps: list[tuple[int, int]] = []
        self._down: dict[int, tuple[int, int]] = {}
        self.extra_shell: dict[str, str] = {}
        self.render()

    # ----------------------------------------------------------- properties
    @property
    def display_size(self) -> tuple[int, int]:
        return self._display

    @property
    def frame_size(self) -> tuple[int, int]:
        return self._frame

    @property
    def screen(self) -> FakeScreen:
        return self.screens[self.current]

    # -------------------------------------------------------------- render
    def _node_text(self, n: FakeNode) -> str:
        if n.is_field:
            return self.values.get((self.current, n.rid), "")
        return n.text

    def render(self) -> None:
        w, h = self._frame
        s = self.screen
        if s.secure:
            self.framebus.publish(np.zeros((h, w, 3), dtype=np.uint8))
            return
        seed = int(hashlib.sha256((s.name + repr([self._node_text(n) for n in s.nodes])).encode()).hexdigest()[:6], 16)
        bg = (200 + seed % 50, 200 + (seed >> 8) % 50, 200 + (seed >> 16) % 50)
        img = Image.new("RGB", (w, h), bg)
        d = ImageDraw.Draw(img)
        sx, sy = w / self._display[0], h / self._display[1]
        font = _font(max(12, int(h / 50)))
        for i, n in enumerate(s.nodes):
            x0, y0, x1, y1 = n.bounds
            box = (x0 * sx, y0 * sy, x1 * sx, y1 * sy)
            shade = (seed * (i + 3)) % 160
            d.rectangle(box, outline=(shade, 40, 120), width=3, fill=(255 - shade // 2, 255, 255 - shade // 3) if n.clickable else None)
            text = self._node_text(n)
            if n.password and text:
                text = "*" * len(text)
            d.text((box[0] + 8, box[1] + 6), text[:40], fill=(0, 0, 0), font=font)
        self.framebus.publish(np.asarray(img))

    def _xml(self) -> str:
        s = self.screen
        parts = [
            "<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>",
            '<hierarchy rotation="0">',
            f'<node index="0" text="" resource-id="" class="android.widget.FrameLayout" package="{s.package}" content-desc="" checkable="false" checked="false" clickable="false" enabled="true" focusable="false" focused="false" scrollable="false" long-clickable="false" password="false" selected="false" bounds="[0,0][{self._display[0]},{self._display[1]}]">',
        ]
        for i, n in enumerate(s.nodes):
            x0, y0, x1, y1 = n.bounds
            text = self._node_text(n)
            if n.password and text:
                text = "•" * len(text)
            focused = self.focus == (self.current, n.rid) and n.is_field
            attrs = {
                "index": str(i), "text": text, "resource-id": n.rid, "class": n.cls, "package": s.package,
                "content-desc": n.desc, "checkable": str(n.checkable).lower(), "checked": str(n.checked).lower(),
                "clickable": str(n.clickable).lower(), "enabled": "true", "focusable": str(n.clickable).lower(),
                "focused": str(focused).lower(), "scrollable": str(n.scrollable).lower(), "long-clickable": "false",
                "password": str(n.password).lower(), "selected": "false", "bounds": f"[{x0},{y0}][{x1},{y1}]",
            }
            parts.append("<node " + " ".join(f"{k}={quoteattr(v)}" for k, v in attrs.items()) + " />")
        parts.append("</node></hierarchy>")
        return "".join(parts)

    # ----------------------------------------------------------- behaviour
    def goto(self, name: str, push: bool = True) -> None:
        if push:
            self.stack.append(self.current)
        self.current = name
        self.focus = None
        self.render()

    def _tap(self, x: int, y: int) -> None:
        self.taps.append((x, y))
        hits = [n for n in self.screen.nodes if n.bounds[0] <= x < n.bounds[2] and n.bounds[1] <= y < n.bounds[3]]
        if not hits:
            return
        n = min(hits, key=lambda k: (k.bounds[2] - k.bounds[0]) * (k.bounds[3] - k.bounds[1]))
        if n.is_field:
            self.focus = (self.current, n.rid)
            self.render()
        elif n.checkable:
            n.checked = not n.checked
            self.render()
        elif n.goto:
            self.goto(n.goto)

    def _type(self, text: str) -> None:
        if self.focus is None:
            return
        self.values[self.focus] = self.values.get(self.focus, "") + text
        self.render()

    async def send_control(self, msg: bytes) -> None:
        self.sent.append(bytes(msg))
        m = p.decode_control(msg)
        self.decoded.append(m)
        t = m["type"]
        if t == p.ControlType.INJECT_TOUCH_EVENT:
            fw, fh = self._frame
            assert (m["screen_w"], m["screen_h"]) == (fw, fh), "touch must use the current frame size"
            dx, dy = self.frame_to_display(int(m["x"]), int(m["y"]))  # type: ignore[arg-type]
            pid = int(m["pointer_id"])  # type: ignore[arg-type]
            if m["action"] == p.MotionAction.DOWN:
                self._down[pid] = (dx, dy)
            elif m["action"] == p.MotionAction.UP:
                start = self._down.pop(pid, (dx, dy))
                if abs(start[0] - dx) < 30 and abs(start[1] - dy) < 30 and not self._down:
                    self._tap(dx, dy)
            elif m["action"] == p.MotionAction.CANCEL:
                self._down.pop(pid, None)
        elif t == p.ControlType.BACK_OR_SCREEN_ON and m["action"] == p.KeyAction.DOWN and self.stack:
            self.goto(self.stack.pop(), push=False)  # right click in the mirror
        elif t == p.ControlType.INJECT_TEXT:
            self._type(str(m["text"]))
        elif t == p.ControlType.INJECT_KEYCODE and m["action"] == p.KeyAction.DOWN:
            code = m["keycode"]
            if code == p.Keycode.BACK and self.stack:
                self.goto(self.stack.pop(), push=False)
            elif code == p.Keycode.HOME:
                home = next((s for s in self.screens if s == "home"), None)
                if home:
                    self.goto(home)
            elif code == p.Keycode.DEL and self.focus:
                self.values[self.focus] = self.values.get(self.focus, "")[:-1]
                self.render()
        elif t == p.ControlType.SET_CLIPBOARD:
            self.clipboard = str(m["text"])
            if m["paste"]:
                if self.paste_delay_s > 0:
                    asyncio.get_running_loop().call_later(self.paste_delay_s, lambda: self._type(self.clipboard))
                else:
                    self._type(self.clipboard)
        await asyncio.sleep(0)

    async def dump_ui(self) -> str:
        if self.ui_dump_failures > 0:
            self.ui_dump_failures -= 1
            raise RuntimeError("ERROR: could not get idle state")
        await asyncio.sleep(0)
        return self._xml()

    async def shell(self, cmd: str, timeout: float = 10.0) -> str:
        self.shell_log.append(cmd)
        await asyncio.sleep(0)
        if cmd.startswith("monkey -p "):
            pkg = cmd.split()[2]
            target = next((s for s in self.screens.values() if s.package == pkg), None)
            if target:
                self.goto(target.name)
            return "Events injected: 1"
        if cmd.startswith("pm list packages"):
            return "\n".join(f"package:{pkg}" for pkg in sorted({s.package for s in self.screens.values()}))
        if "input_method" in cmd:
            return "mInputShown=" + ("true" if self.focus else "false")
        for prefix, out in self.extra_shell.items():
            if cmd.startswith(prefix):
                return out
        return ""

    async def foreground(self) -> tuple[str, str]:
        return self.screen.package, self.screen.activity

    async def push(self, local: str, remote: str) -> None:
        from pathlib import Path

        self.pushed[remote] = Path(local).read_bytes()

    def field_value(self, screen: str, rid: str) -> str:
        return self.values.get((screen, rid), "")


def demo_screens() -> list[FakeScreen]:
    """A small Google-sign-in-like flow plus a store purchase screen, used by tests and GUI demo mode."""
    return [
        FakeScreen("home", "com.android.launcher3", ".Launcher", [
            label("홈 화면", 120),
            button("설정", 400, goto="settings", rid="com.android.launcher3:id/settings"),
            button("Play 스토어", 560, goto="store", rid="com.android.launcher3:id/store"),
            button("YouTube", 720, goto="youtube", rid="com.android.launcher3:id/youtube"),
        ]),
        FakeScreen("settings", "com.android.settings", ".Settings", [
            label("설정", 120),
            button("Google", 400, goto="google_signin", rid="com.android.settings:id/google"),
            button("디스플레이", 560),
        ]),
        FakeScreen("google_signin", "com.google.android.gms", ".auth.SignInActivity", [
            label("로그인", 120),
            field_node("이메일 또는 휴대전화", 300, "com.google.android.gms:id/email"),
            field_node("비밀번호", 480, "com.google.android.gms:id/password", password=True),
            button("이메일을 잊으셨나요?", 660, rid="com.google.android.gms:id/forgot"),
            button("계정 만들기", 1900, x0=60, x1=500, rid="com.google.android.gms:id/create"),
            button("다음", 1900, goto="google_2sv", x0=580, x1=1020, rid="com.google.android.gms:id/next"),
        ]),
        FakeScreen("google_2sv", "com.google.android.gms", ".security.TwoStep", [
            label("2단계 인증", 120),
            label("인증자 앱", 300),
            label("백업 코드", 420),
            button("전화번호 추가", 600, rid="com.google.android.gms:id/add_phone"),
            button("백업 코드 받기", 800, goto="backup_codes", rid="com.google.android.gms:id/get_codes"),
            button("2단계 인증 사용 설정", 1000, goto="google_2sv_on", rid="com.google.android.gms:id/turn_on"),
        ]),
        FakeScreen("google_2sv_on", "com.google.android.gms", ".security.TwoStepOn", [
            label("2단계 인증 사용 중", 120),
            label("인증자 앱", 300),
            label("백업 코드", 420),
        ]),
        FakeScreen("backup_codes", "com.google.android.gms", ".security.BackupCodes", [
            label("백업 코드", 120),
            *[label(f"{(1000 + i * 811) % 10000:04d} {(2000 + i * 777) % 10000:04d}", 260 + i * 100) for i in range(10)],
            button("완료", 1400, goto="google_2sv_on"),
        ]),
        FakeScreen("store", "com.android.vending", ".AssetBrowserActivity", [
            label("프리미엄 테마", 120),
            label("₩4,400", 240),
            button("구매", 400, goto="store_done", rid="com.android.vending:id/buy"),
        ]),
        FakeScreen("store_done", "com.android.vending", ".Done", [label("구매 완료", 120)]),
        FakeScreen("youtube", "com.google.android.youtube", ".HomeActivity", [
            label("추천 영상", 120),
            button("좋아요", 400, rid="com.google.android.youtube:id/like"),
            button("구독", 560, rid="com.google.android.youtube:id/subscribe"),
            button("나", 2200, goto="youtube_me", x0=860, x1=1020),
        ]),
        FakeScreen("youtube_me", "com.google.android.youtube", ".MeActivity", [
            label("내 페이지", 120),
            button("채널 만들기", 400, goto="youtube_create", rid="com.google.android.youtube:id/create_channel"),
        ]),
        FakeScreen("youtube_create", "com.google.android.youtube", ".CreateChannel", [
            label("채널 만들기", 120),
            field_node("이름", 300, "com.google.android.youtube:id/name"),
            field_node("핸들", 480, "com.google.android.youtube:id/handle"),
            button("채널 만들기", 1900, goto="youtube_channel", rid="com.google.android.youtube:id/create"),
        ]),
        FakeScreen("youtube_channel", "com.google.android.youtube", ".Channel", [label("내 채널", 120)]),
        FakeScreen("bank", "com.example.bank", ".Main", [label("잔액", 120), button("송금", 400)], secure=True),
    ]
