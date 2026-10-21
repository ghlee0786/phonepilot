"""Builds Observations for the agent (PRD 9.2) with masking applied (PRD 12.4)."""

from __future__ import annotations

import asyncio
import itertools
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from phonepilot.core.device import DeviceBackend
from phonepilot.errors import PhonePilotError
from phonepilot.observe import uitree, vision
from phonepilot.observe.uitree import UIElement
from phonepilot.security.policy import PolicyEngine
from phonepilot.security.redactor import Redactor

SENSITIVE_SCREEN_MARKERS = [
    "백업 코드", "backup codes", "인증자 앱", "인증 앱", "authenticator", "설정 키", "setup key", "스캔할 수 없나요",
    "can't scan", "qr 코드를 스캔", "scan the qr", "인증 코드 입력", "enter the code", "보안 코드",
]
DISMISS_WORDS = ["나중에", "거부", "허용 안함", "허용 안 함", "닫기", "건너뛰기", "취소", "아니요", "not now", "deny", "don't allow", "close", "skip", "no thanks", "later", "cancel"]
DIALOG_PACKAGES = {"com.google.android.permissioncontroller", "com.android.permissioncontroller", "com.android.packageinstaller"}

_obs_ids = itertools.count(1)


@dataclass
class Observation:
    obs_id: str
    package: str
    activity: str
    app_label: str
    screen: dict[str, Any]
    keyboard_visible: bool
    focused_element: int | None
    secure_surface: bool
    sensitive_app: bool
    sensitive_screen: bool
    elements: list[UIElement]
    text_blocks: list[dict[str, Any]]
    scroll_containers: list[dict[str, Any]]
    dialogs: list[dict[str, Any]]
    diff: dict[str, Any]
    source: str
    warnings: list[str]
    truncated: int = 0
    screen_hash: str = ""
    image_jpeg: bytes | None = None
    created: float = field(default_factory=time.monotonic)

    def element(self, eid: int) -> UIElement | None:
        return next((e for e in self.elements if e.id == eid), None)

    def screen_texts(self) -> list[str]:
        return [e.label for e in self.elements] + [str(b["text"]) for b in self.text_blocks]

    def to_dict(self, redactor: Redactor) -> dict[str, Any]:
        ctx = self.sensitive_screen
        data = {
            "obs_id": self.obs_id,
            "package": self.package,
            "activity": self.activity,
            "app_label": self.app_label,
            "screen": self.screen,
            "keyboard_visible": self.keyboard_visible,
            "focused_element": self.focused_element,
            "secure_surface": self.secure_surface,
            "sensitive_app": self.sensitive_app,
            "sensitive_screen": self.sensitive_screen,
            "elements": uitree.serialize_elements(self.elements),
            "untrusted_screen_text": [b["text"] for b in self.text_blocks],
            "scroll_containers": self.scroll_containers,
            "dialogs": self.dialogs,
            "diff": self.diff,
            "source": self.source,
            "warnings": self.warnings,
            "truncated": self.truncated,
        }
        return redactor.redact_obj(data, sensitive_context=ctx)


class Observer:
    def __init__(
        self,
        device: DeviceBackend,
        redactor: Redactor,
        policy: PolicyEngine,
        *,
        send_images_in_sensitive_apps: bool = False,
        screenshot_max_edge: int = 1280,
    ) -> None:
        self.device = device
        self.redactor = redactor
        self.policy = policy
        self.send_images_in_sensitive_apps = send_images_in_sensitive_apps
        self.screenshot_max_edge = screenshot_max_edge
        self._cache: OrderedDict[str, Observation] = OrderedDict()
        self.last: Observation | None = None
        self.labels: dict[str, str] = {}

    # ------------------------------------------------------------ helpers
    async def _keyboard_visible(self) -> bool:
        try:
            out = await self.device.shell("dumpsys input_method | grep -i mInputShown", timeout=4)
        except Exception:
            return False
        return "mInputShown=true" in out

    async def _dump(self) -> uitree.ParsedTree | None:
        for attempt in range(3):
            try:
                xml = await self.device.dump_ui()
                return uitree.parse_xml(xml)
            except Exception:
                if attempt < 2:
                    await asyncio.sleep(0.5)
        return None

    def app_label(self, package: str) -> str:
        return self.labels.get(package) or package.rsplit(".", 1)[-1]

    # ------------------------------------------------------------- observe
    async def observe(self, image: str = "none", max_elements: int = 150) -> Observation:
        tree_t = asyncio.create_task(self._dump())
        fg_t = asyncio.create_task(self.device.foreground())
        kb_t = asyncio.create_task(self._keyboard_visible())
        tree = await tree_t
        try:
            package, activity = await fg_t
        except Exception:
            package, activity = "", ""
        keyboard = await kb_t

        dw, dh = self.device.display_size
        warnings: list[str] = []
        elements: list[UIElement] = []
        blocks: list[dict[str, Any]] = []
        truncated = 0
        if tree is None:
            warnings.append("TREE_EMPTY")
        else:
            elements, blocks, truncated = uitree.normalize(tree, dw or None, dh or None, max_elements)
            if not package and tree.nodes:
                package = tree.nodes[0].package
        frame = self.device.framebus.latest()
        secure = bool(frame is not None and vision.looks_black(frame.rgb) and elements)
        if secure:
            warnings.append("SECURE_SURFACE")
        all_text = " ".join([e.label for e in elements] + [str(b["text"]) for b in blocks]).lower()
        sensitive_screen = any(m.lower() in all_text for m in SENSITIVE_SCREEN_MARKERS)
        label = self.app_label(package)
        sensitive_app = self.policy.is_sensitive_app(package, label)

        focused = next((e.id for e in elements if e.focused and e.role == "text_field"), None)
        scrolls = [
            {"element": e.id, "bounds_norm": list(e.bounds_norm), "directions": ["up", "down"]}
            for e in elements
            if e.scrollable
        ]
        dialogs = self._dialogs(package, elements)
        prev = self.last
        diff: dict[str, Any] = {}
        if prev is not None:
            prev_keys = {e.stable_key for e in prev.elements}
            keys = {e.stable_key for e in elements}
            diff = {
                "previous_obs_id": prev.obs_id,
                "package_changed": prev.package != package,
                "new_elements": len(keys - prev_keys),
                "gone_elements": len(prev_keys - keys),
            }
        source = "ui_tree" if elements else ("image_only" if frame is not None else "none")
        if not elements and frame is not None and image == "none":
            image = "som"  # tree poor: hand over the picture (PRD 9.2)

        obs = Observation(
            obs_id=f"o-{next(_obs_ids):06d}",
            package=package,
            activity=activity,
            app_label=label,
            screen={"width": dw, "height": dh, "on": True},
            keyboard_visible=keyboard,
            focused_element=focused,
            secure_surface=secure,
            sensitive_app=sensitive_app,
            sensitive_screen=sensitive_screen,
            elements=elements,
            text_blocks=blocks,
            scroll_containers=scrolls,
            dialogs=dialogs,
            diff=diff,
            source=source,
            warnings=warnings,
            truncated=truncated,
            screen_hash=vision.screen_hash_hex(frame.rgb) if frame is not None else "",
        )
        if image != "none" and frame is not None:
            obs.image_jpeg = self._image(obs, frame.rgb, image)
        self._remember(obs)
        return obs

    def _remember(self, obs: Observation) -> None:
        self._cache[obs.obs_id] = obs
        while len(self._cache) > 3:
            self._cache.popitem(last=False)
        self.last = obs

    def get(self, obs_id: str) -> Observation | None:
        return self._cache.get(obs_id)

    def _dialogs(self, package: str, elements: list[UIElement]) -> list[dict[str, Any]]:
        dismiss = [e for e in elements if e.clickable and any(w == e.label.strip().lower() for w in DISMISS_WORDS)]
        if package in DIALOG_PACKAGES or (dismiss and len(elements) <= 8):
            return [
                {
                    "kind": "permission" if package in DIALOG_PACKAGES else "popup",
                    "dismiss_element": dismiss[0].id if dismiss else None,
                    "texts": [e.label for e in elements[:6]],
                }
            ]
        return []

    def image_blocked_reason(self, obs: Observation) -> str | None:
        if obs.sensitive_screen:
            return "sensitive screen (2-step verification / backup codes): image withheld"
        if obs.sensitive_app and not self.send_images_in_sensitive_apps:
            return "sensitive app: image withheld (settings > AI)"
        return None

    def _image(self, obs: Observation, rgb: Any, mode: str) -> bytes | None:
        reason = self.image_blocked_reason(obs)
        if reason:
            obs.warnings.append("IMAGE_WITHHELD")
            return None
        img = vision.to_image(rgb)
        dw, dh = self.device.display_size
        src = (dw or img.size[0], dh or img.size[1])
        mask = [e.bounds for e in obs.elements if e.password or self.redactor.contains_secret(e.text + e.label)]
        mask += [e.bounds for e in obs.elements if self.redactor.redact(e.label) != e.label]
        if mask:
            img = vision.mask_boxes(img, mask, src)
        img = vision.downscale(img, self.screenshot_max_edge)
        if mode == "som":
            img = vision.render_som(img, [(e.id, e.bounds) for e in obs.elements], src)
        return vision.encode(img, "jpeg", 80)

    async def screenshot(self, max_edge: int | None = None, fmt: str = "jpeg", region_norm: tuple[int, int, int, int] | None = None) -> tuple[bytes, tuple[int, int]]:
        frame = self.device.framebus.latest()
        if frame is None:
            raise PhonePilotError("CONTROL_UNAVAILABLE", "no video frame yet")
        obs = self.last
        if obs is None or time.monotonic() - obs.created > 2:
            obs = await self.observe()
        reason = self.image_blocked_reason(obs)
        if reason:
            raise PhonePilotError("POLICY_BLOCKED", reason)
        img = vision.to_image(frame.rgb)
        dw, dh = self.device.display_size
        mask = [e.bounds for e in obs.elements if e.password or self.redactor.contains_secret(e.text + e.label)]
        if mask:
            img = vision.mask_boxes(img, mask, (dw or img.size[0], dh or img.size[1]))
        if region_norm:
            img = vision.crop_norm(img, region_norm)
        img = vision.downscale(img, max_edge or self.screenshot_max_edge)
        return vision.encode(img, fmt), img.size

    async def wait_stable(self, timeout: float = 5.0, check_progress: bool = True) -> bool:
        tracker = vision.StabilityTracker()
        deadline = time.monotonic() + timeout
        seq = -1
        fed_at_seq = None
        while time.monotonic() < deadline:
            frame = self.device.framebus.latest()
            if frame is not None and frame.seq != fed_at_seq:
                fed_at_seq = frame.seq
            if frame is not None:
                stable = tracker.feed(frame.rgb)
                seq = frame.seq
                if stable:
                    if check_progress:
                        tree = await self._dump()
                        if tree is not None and uitree.has_progress(tree):
                            await asyncio.sleep(0.25)
                            continue
                    return True
            await asyncio.sleep(0.1)
        _ = seq
        return False
