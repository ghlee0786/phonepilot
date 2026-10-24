"""Tool implementations shared by the MCP server, the built-in agent and the skill engine (PRD 10).

Every AI action goes through `ToolService._act`, the single action path:
rate limit -> arbiter gate/lease -> target resolution -> policy -> approval -> execute
-> settle -> result + timeline. Read-only tools skip the policy/approval part.
"""

from __future__ import annotations

import asyncio
import shlex
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from phonepilot.core import gesture
from phonepilot.core.arbiter import InputArbiter
from phonepilot.core.calibration import GestureProfile
from phonepilot.core.coords import norm_to_px
from phonepilot.core.device import DeviceBackend
from phonepilot.core.scrcpy import protocol as p
from phonepilot.errors import PhonePilotError
from phonepilot.observe import uitree, vision
from phonepilot.observe.observer import Observation, Observer
from phonepilot.observe.uitree import UIElement
from phonepilot.security.approvals import ApprovalCenter, ApprovalRequest, HumanRequest
from phonepilot.security.limits import DailyCounters, RateLimiter
from phonepilot.security.policy import ActionContext, Level, PolicyDecision, PolicyEngine
from phonepilot.security.redactor import Redactor
from phonepilot.security.secrets import SCREEN_KINDS, SecretVault
from phonepilot.security.vault import RecoveryVault, extract_backup_codes
from phonepilot.agent.integration_tools import IntegrationMixin
from phonepilot.agent.timeline import Timeline, TimelineEvent

KEY_NAMES: dict[str, int] = {
    "enter": p.Keycode.ENTER, "back": p.Keycode.BACK, "home": p.Keycode.HOME, "app_switch": p.Keycode.APP_SWITCH,
    "tab": p.Keycode.TAB, "del": p.Keycode.DEL, "backspace": p.Keycode.DEL, "forward_del": p.Keycode.FORWARD_DEL,
    "dpad_up": p.Keycode.DPAD_UP, "dpad_down": p.Keycode.DPAD_DOWN, "dpad_left": p.Keycode.DPAD_LEFT,
    "dpad_right": p.Keycode.DPAD_RIGHT, "dpad_center": p.Keycode.DPAD_CENTER, "volume_up": p.Keycode.VOLUME_UP,
    "volume_down": p.Keycode.VOLUME_DOWN, "volume_mute": p.Keycode.VOLUME_MUTE, "power": p.Keycode.POWER,
    "search": p.Keycode.SEARCH, "menu": p.Keycode.MENU, "escape": p.Keycode.ESCAPE, "move_home": p.Keycode.MOVE_HOME,
    "move_end": p.Keycode.MOVE_END, "page_up": p.Keycode.PAGE_UP, "page_down": p.Keycode.PAGE_DOWN,
    "space": p.Keycode.SPACE, "copy": p.Keycode.COPY, "paste": p.Keycode.PASTE, "cut": p.Keycode.CUT,
}
META_NAMES = {"shift": p.META_SHIFT_ON, "alt": p.META_ALT_ON, "ctrl": p.META_CTRL_ON, "meta": p.META_META_ON}
PASTE_TIMEOUT_S = 3.0  # max wait for pasted text to appear before the clipboard is restored
PASTE_FALLBACK_S = 1.0  # wait when the focused field cannot be read (password, no focus)
TWO_SV_PHONE_MARKERS = ["음성 또는 문자 메시지", "문자 메시지", "voice or text message", "2단계 인증 전화번호", "2-step verification phone", "sms"]
TWO_SV_ALLOWED = {
    "authenticator": ["인증자", "인증 앱", "authenticator"],
    "backup_codes": ["백업 코드", "backup codes"],
    "google_prompt": ["google 메시지", "google prompts", "google prompt"],
    "passkey": ["패스키", "passkeys", "보안 키", "security key"],
}


@dataclass
class ToolConfig:
    step_budget: int = 300
    engagement_daily: int = 5
    in_app_purchase_daily_krw: int = 0
    settle_timeout_s: float = 5.0
    allowed_push_dirs: list[Path] = field(default_factory=list)
    device_tmp_dir: str = "/sdcard/Download/phonepilot_tmp"
    allow_headless_actions: bool = True
    project_root: Path | None = None
    auto_approve_permissions: bool = False  # safety.approval_mode == "auto"


@dataclass
class Target:
    x: int
    y: int
    element: UIElement | None
    coordinate: bool


def _hamming_hex(a: str, b: str) -> float:
    if not a or not b or len(a) != len(b):
        return 1.0
    return bin(int(a, 16) ^ int(b, 16)).count("1") / (len(a) * 4)


class ToolService(IntegrationMixin):
    def __init__(
        self,
        device: DeviceBackend,
        arbiter: InputArbiter,
        observer: Observer,
        policy: PolicyEngine,
        approvals: ApprovalCenter,
        secrets: SecretVault,
        redactor: Redactor,
        timeline: Timeline,
        counters: DailyCounters,
        recovery: RecoveryVault,
        config: ToolConfig | None = None,
    ) -> None:
        self.device = device
        self.arbiter = arbiter
        self.observer = observer
        self.policy = policy
        self.approvals = approvals
        self.secrets = secrets
        self.redactor = redactor
        self.timeline = timeline
        self.counters = counters
        self.recovery = recovery
        self.config = config or ToolConfig()
        self.rate = RateLimiter(per_second=5, per_minute=120)
        self.steps_used: dict[str, int] = {}
        self._recent_actions: deque[tuple[str, str, str]] = deque(maxlen=6)
        self.device_clipboard: str | None = None
        self.clipboard_used = False
        self.pushed_files: list[str] = []
        self.skills: Any = None  # SkillEngine, attached by the runtime
        self.health_report: Any = None  # CoreRuntime.health_report, attached by the runtime (PRD 25)
        self.integrations: dict[str, Any] = {}
        self._clip_seq = 1
        self.notifications: list[Callable[[str, str], None]] = []
        self.gesture_profile = GestureProfile()  # calibrated per device by the runtime (FR-INP-037)

    # ================================================================ helpers
    def _settings_for(self, actor: str) -> None:
        used = self.steps_used.get(actor, 0) + 1
        if used > self.config.step_budget:
            raise PhonePilotError("STEP_BUDGET_EXCEEDED", f"{actor} used {used - 1} actions")
        self.steps_used[actor] = used

    async def _fresh_obs(self, max_age_s: float = 1.5) -> Observation:
        obs = self.observer.last
        if obs is None or time.monotonic() - obs.created > max_age_s:
            obs = await self.observer.observe()
        return obs

    async def resolve_target(self, args: dict[str, Any]) -> Target | None:
        dw, dh = self.device.display_size
        if args.get("element") is not None:
            eid = int(args["element"])
            obs_id = args.get("obs_id")
            obs = self.observer.get(obs_id) if obs_id else self.observer.last
            if obs is None:
                raise PhonePilotError("ELEMENT_STALE", f"observation {obs_id} expired")
            el = obs.element(eid)
            if el is None:
                raise PhonePilotError("ELEMENT_STALE", f"element {eid} not in {obs.obs_id}")
            frame = self.device.framebus.latest()
            changed = bool(frame is not None and obs.screen_hash and _hamming_hex(vision.screen_hash_hex(frame.rgb), obs.screen_hash) > 0.02)
            if not changed:
                pkg, act = await self.device.foreground()
                changed = (pkg, act) != (obs.package, obs.activity)
            if changed or obs is not self.observer.last:
                cur = await self.observer.observe()
                same = next((e for e in cur.elements if e.stable_key == el.stable_key), None)
                if same is None:
                    raise PhonePilotError("ELEMENT_STALE", "the screen changed; observe again", obs_id=cur.obs_id)
                el = same
            x, y = el.center
            return Target(x, y, el, False)
        if args.get("selector"):
            obs = await self.observer.observe()
            hits = uitree.find(obs.elements, args["selector"])
            if not hits:
                raise PhonePilotError("ELEMENT_NOT_FOUND", f"no element matches {args['selector']}")
            el = hits[0]
            x, y = el.center
            return Target(x, y, el, False)
        if args.get("x_norm") is not None and args.get("y_norm") is not None:
            x, y = norm_to_px(float(args["x_norm"]), float(args["y_norm"]), dw, dh)
            obs = await self._fresh_obs()
            return Target(x, y, uitree.element_at(obs.elements, x, y), True)
        return None

    def _preview(self, target: Target | None) -> bytes | None:
        frame = self.device.framebus.latest()
        if frame is None:
            return None
        img = vision.to_image(frame.rgb)
        dw, dh = self.device.display_size
        if target and target.element:
            img = vision.render_som(img, [(0, target.element.bounds)], (dw or img.size[0], dh or img.size[1]))
        img = vision.downscale(img, 720)
        return vision.encode(img, "png")

    async def _gate(self, tool: str, actor: str, args: dict[str, Any], target: Target | None, reason: str, in_batch: bool = False) -> PolicyDecision:
        obs = self.observer.last or await self.observer.observe()
        ctx = ActionContext(
            tool=tool,
            package=obs.package,
            app_label=obs.app_label,
            target_label=(target.element.label if target and target.element else str(args.get("label_hint", ""))),
            target_role=(target.element.role if target and target.element else ""),
            screen_texts=obs.screen_texts(),
            secure_surface=obs.secure_surface,
            in_workflow=actor.startswith("workflow:"),
            in_batch=in_batch,
            coordinate_tap=bool(target and target.coordinate),
            args=args,
        )
        decision = self.policy.classify(ctx)
        if decision.level is Level.BLOCK:
            raise PhonePilotError("POLICY_BLOCKED", "; ".join(decision.reasons), category=decision.category)
        if decision.category == "engagement":
            self.counters.use_engagement(self.config.engagement_daily)
        if decision.permission_only and self.config.auto_approve_permissions:
            decision.auto_approved = True  # auto mode: only the system permission dialog's "allow", nothing else
            return decision
        if decision.needs_approval:
            label = ctx.target_label or tool
            summary = f"{obs.app_label}에서 '{label}' {tool}"
            if decision.amounts:
                summary += f" — 화면 금액 {decision.amounts[0]}"
            req = ApprovalRequest(
                title="돈 또는 계정 보안에 영향을 주는 행동입니다" if decision.level is Level.CONFIRM_HIGH else "AI가 다음 행동을 요청합니다",
                summary=self.redactor.redact(summary),
                decision=decision,
                client=actor,
                reason=self.redactor.redact(reason),
                details={"package": obs.package, "category": decision.category, "reasons": decision.reasons},
                preview_png=self._preview(target),
                screen_hash=obs.screen_hash,
                target_key=target.element.stable_key if target and target.element else "",
                expected_amount=decision.amounts[0] if decision.level is Level.CONFIRM_HIGH and decision.amounts else None,
            )
            resp = await self.approvals.request(req)
            if resp.stop_all:
                await self.arbiter.kill()
                raise PhonePilotError("KILL_SWITCH_ACTIVE", "user stopped everything")
            # FR-HIL-001: approval is bound to the screen and the target
            fresh = await self.observer.observe()
            if target and target.element:
                still = next((e for e in fresh.elements if e.stable_key == target.element.stable_key), None)
                if still is None:
                    raise PhonePilotError("APPROVAL_INVALIDATED", "target changed after approval")
                target.element = still
                if not target.coordinate:
                    target.x, target.y = still.center
            elif obs.screen_hash and fresh.screen_hash and _hamming_hex(obs.screen_hash, fresh.screen_hash) > 0.06:
                raise PhonePilotError("APPROVAL_INVALIDATED", "screen changed after approval")
        return decision

    async def _act(
        self,
        tool: str,
        actor: str,
        args: dict[str, Any],
        run: Callable[[Target | None], Awaitable[dict[str, Any] | None]],
        *,
        needs_target: bool = False,
        settle: bool = True,
        in_batch: bool = False,
    ) -> dict[str, Any]:
        t0 = time.monotonic()
        reason = str(args.get("reason", ""))
        level = ""
        target_label = ""
        try:
            self.rate.check()
            self.arbiter.check_ai_allowed()
            self.arbiter.acquire_lease(actor)
            self._settings_for(actor)
            target = await self.resolve_target(args)
            if needs_target and target is None:
                raise PhonePilotError("ELEMENT_NOT_FOUND", "give element+obs_id, selector or x_norm/y_norm")
            target_label = target.element.label if target and target.element else (f"({target.x},{target.y})" if target else "")
            before = self.observer.last
            before_hash = before.screen_hash if before else ""
            decision = await self._gate(tool, actor, args, target, reason, in_batch)
            level = decision.level.name
            extra = await run(target) or {}
            result: dict[str, Any] = {"ok": True, **extra}
            if decision.needs_approval:
                result["approval"] = {"level": level, "approved": True, "auto": decision.auto_approved}
            if settle and args.get("wait", True):
                stable = await self.observer.wait_stable(self.config.settle_timeout_s, check_progress=False)
                if not stable:
                    result.setdefault("warnings", []).append("UNSTABLE_SCREEN")
            frame = self.device.framebus.latest()
            after_hash = vision.screen_hash_hex(frame.rgb) if frame is not None else ""
            result["screen_changed"] = bool(after_hash and after_hash != before_hash)
            if args.get("return_observation") or True:
                obs = await self.observer.observe()
                if before is not None:
                    result["package_changed"] = obs.package != before.package
                result["new_dialogs"] = obs.dialogs
                if args.get("return_observation"):
                    result["observation"] = obs.to_dict(self.redactor)
            key = (before_hash, tool, target_label)
            self._recent_actions.append(key)
            if list(self._recent_actions).count(key) >= 3 and not result.get("screen_changed"):
                result.setdefault("warnings", []).append("LOOP_SUSPECTED")
            result["elapsed_ms"] = int((time.monotonic() - t0) * 1000)
            point: dict[str, Any] = {}
            if target is not None:
                dw, dh = self.device.display_size
                point = {"x_norm": round(target.x / max(dw, 1) * 1000), "y_norm": round(target.y / max(dh, 1) * 1000)}
            outcome = "auto_approved" if decision.auto_approved else "ok"
            self.timeline.add(TimelineEvent(actor, tool, target_label, level or "ALLOW", outcome, reason, result["elapsed_ms"], point))
            return result
        except PhonePilotError as e:
            self.timeline.add(TimelineEvent(actor, tool, target_label, level, e.code, reason, int((time.monotonic() - t0) * 1000)))
            raise

    def _note(self, actor: str, tool: str, target: str = "", detail: dict[str, Any] | None = None) -> None:
        self.timeline.add(TimelineEvent(actor, tool, target, "ALLOW", "ok", detail=detail or {}))

    # ============================================================ observation
    async def observe(self, actor: str = "agent", image: str = "none", max_elements: int = 150) -> dict[str, Any]:
        obs = await self.observer.observe(image=image, max_elements=max_elements)
        data = obs.to_dict(self.redactor)
        if obs.image_jpeg:
            data["_image"] = obs.image_jpeg
        elif image != "none":
            reason = self.observer.image_blocked_reason(obs)
            if reason:
                data["image_withheld"] = reason
        self._note(actor, "observe", obs.app_label)
        return data

    async def screenshot(self, actor: str = "agent", max_edge: int = 1280, format: str = "jpeg", region_norm: list[int] | None = None) -> dict[str, Any]:
        data, size = await self.observer.screenshot(max_edge, format, tuple(region_norm) if region_norm else None)  # type: ignore[arg-type]
        self._note(actor, "screenshot")
        return {"width": size[0], "height": size[1], "format": format, "_image": data, "_mime": f"image/{'png' if format == 'png' else 'jpeg'}"}

    async def zoom(self, actor: str = "agent", bounds_norm: list[int] | None = None, scale: float = 2) -> dict[str, Any]:
        if not bounds_norm or len(bounds_norm) != 4:
            raise PhonePilotError("ELEMENT_NOT_FOUND", "bounds_norm=[x0,y0,x1,y1] required")
        data, size = await self.observer.screenshot(max_edge=int(1280 * scale), fmt="png", region_norm=tuple(bounds_norm))  # type: ignore[arg-type]
        return {"width": size[0], "height": size[1], "_image": data, "_mime": "image/png"}

    async def ui_tree(self, actor: str = "agent", raw: bool = False) -> dict[str, Any]:
        xml = await self.device.dump_ui()
        tree = uitree.parse_xml(xml)
        nodes = []
        for n in tree.nodes:
            text = uitree.PASSWORD_MASK if (n.password and n.text) else n.text
            nodes.append(
                {
                    "depth": n.depth, "class": n.cls, "text": text, "desc": n.desc, "resource_id": n.resource_id,
                    "bounds": list(n.bounds), "clickable": n.clickable, "enabled": n.enabled, "focused": n.focused,
                    "scrollable": n.scrollable, "password": n.password,
                }
            )
        sensitive = self.observer.last.sensitive_screen if self.observer.last else False
        return {"nodes": self.redactor.redact_obj(nodes, sensitive), "count": len(nodes)}

    async def find_elements(self, actor: str = "agent", selector: dict[str, Any] | None = None) -> dict[str, Any]:
        obs = await self.observer.observe()
        hits = uitree.find(obs.elements, selector or {})
        return {"obs_id": obs.obs_id, "matches": self.redactor.redact_obj([e.to_dict() for e in hits], obs.sensitive_screen)}

    async def get_device_info(self, actor: str = "agent") -> dict[str, Any]:
        return self.device.info.public()

    async def get_foreground_app(self, actor: str = "agent") -> dict[str, Any]:
        pkg, act = await self.device.foreground()
        label = self.observer.app_label(pkg)
        return {"package": pkg, "activity": act, "label": label, "sensitive": self.policy.is_sensitive_app(pkg, label)}

    async def list_apps(self, actor: str = "agent", filter: str | None = None, include_system: bool = False) -> dict[str, Any]:
        out = await self.device.shell("pm list packages" + ("" if include_system else " -3"))
        pkgs = sorted(line.split(":", 1)[1].strip() for line in out.splitlines() if line.startswith("package:"))
        if filter:
            pkgs = [x for x in pkgs if filter.lower() in x.lower()]
        return {"packages": pkgs, "count": len(pkgs)}

    # ================================================================ actions
    async def tap(self, actor: str = "agent", duration_ms: float | None = None, **args: Any) -> dict[str, Any]:
        async def run(t: Target | None) -> None:
            assert t is not None
            await self.arbiter.ai_gesture(gesture.tap(t.x, t.y, duration_ms or self.gesture_profile.tap_ms))

        return await self._act("tap", actor, args, run, needs_target=True)

    async def double_tap(self, actor: str = "agent", **args: Any) -> dict[str, Any]:
        async def run(t: Target | None) -> None:
            assert t is not None
            await self.arbiter.ai_gesture(gesture.double_tap(t.x, t.y))

        return await self._act("double_tap", actor, args, run, needs_target=True)

    async def long_press(self, actor: str = "agent", duration_ms: float | None = None, **args: Any) -> dict[str, Any]:
        async def run(t: Target | None) -> None:
            assert t is not None
            await self.arbiter.ai_gesture(gesture.long_press(t.x, t.y, duration_ms or self.gesture_profile.long_press_ms))

        return await self._act("long_press", actor, args, run, needs_target=True)

    def _norm_point(self, pt: list[float] | tuple[float, float]) -> tuple[int, int]:
        dw, dh = self.device.display_size
        return norm_to_px(float(pt[0]), float(pt[1]), dw, dh)

    async def swipe(
        self, actor: str = "agent", start: list[float] | None = None, end: list[float] | None = None, duration_ms: float | None = None,
        curve: str = "ease", arc: float | None = None, hold_end: bool = False, **args: Any,
    ) -> dict[str, Any]:
        """hold_end: rest at the end point for the profile's settle time before lifting (drag-scroll, no fling)."""
        if not start or not end:
            raise PhonePilotError("ELEMENT_NOT_FOUND", "start=[x_norm,y_norm] and end=[x_norm,y_norm] required")
        x0, y0 = self._norm_point(start)
        x1, y1 = self._norm_point(end)

        async def run(_: Target | None) -> None:
            prof = self.gesture_profile
            dist = ((x1 - x0) ** 2 + (y1 - y0) ** 2) ** 0.5
            dur = duration_ms or prof.swipe_ms(dist)
            bend = prof.arc if arc is None else arc
            if hold_end:
                await self.arbiter.ai_gesture(gesture.precise_scroll(x0, y0, x1, y1, dur, prof.settle_ms, bend, ease=prof.ease))
            else:
                await self.arbiter.ai_gesture(gesture.swipe(x0, y0, x1, y1, dur, curve, bend, ease=prof.ease))

        return await self._act("swipe", actor, args, run)

    def _scroll_points(self, direction: str, bounds: tuple[int, int, int, int] | None, amount: float) -> tuple[int, int, int, int]:
        dw, dh = self.device.display_size
        x0, y0, x1, y1 = bounds or (0, int(dh * 0.15), dw, int(dh * 0.85))
        cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
        amount = min(max(amount, 0.1), 1.0)
        span_y = int((y1 - y0) * amount * 0.8)
        span_x = int((x1 - x0) * amount * 0.8)
        if direction == "down":  # content moves up: finger goes up
            return cx, cy + span_y // 2, cx, cy - span_y // 2
        if direction == "up":
            return cx, cy - span_y // 2, cx, cy + span_y // 2
        if direction == "right":
            return cx + span_x // 2, cy, cx - span_x // 2, cy
        if direction == "left":
            return cx - span_x // 2, cy, cx + span_x // 2, cy
        raise PhonePilotError("ELEMENT_NOT_FOUND", f"bad direction {direction}")

    async def scroll(self, actor: str = "agent", direction: str = "down", container: int | None = None, amount: float = 0.6, **args: Any) -> dict[str, Any]:
        bounds = None
        if container is not None:
            obs = self.observer.get(args.get("obs_id", "")) or self.observer.last
            el = obs.element(int(container)) if obs else None
            if el is None:
                raise PhonePilotError("ELEMENT_STALE", "container not found")
            bounds = el.bounds
        x0, y0, x1, y1 = self._scroll_points(direction, bounds, amount)
        before_keys: set[str] = {e.stable_key for e in self.observer.last.elements} if self.observer.last else set()

        async def run(_: Target | None) -> dict[str, Any]:
            prof = self.gesture_profile
            dist = ((x1 - x0) ** 2 + (y1 - y0) ** 2) ** 0.5
            await self.arbiter.ai_gesture(gesture.precise_scroll(x0, y0, x1, y1, prof.scroll_ms(dist), prof.settle_ms, prof.arc, ease=prof.ease))
            return {}

        result = await self._act("scroll", actor, {k: v for k, v in args.items() if k not in {"element"}}, run)
        after = self.observer.last
        if after is not None:
            result["reached_end"] = {e.stable_key for e in after.elements} == before_keys
        return result

    async def scroll_to(self, actor: str = "agent", selector: dict[str, Any] | None = None, direction: str = "down", max_swipes: int = 10, **args: Any) -> dict[str, Any]:
        if not selector:
            raise PhonePilotError("ELEMENT_NOT_FOUND", "selector required")
        for i in range(max_swipes + 1):
            obs = await self.observer.observe()
            hits = uitree.find(obs.elements, selector)
            if hits:
                return {"ok": True, "found": True, "obs_id": obs.obs_id, "element": hits[0].to_dict(), "swipes": i}
            if i == max_swipes:
                break
            res = await self.scroll(actor, direction=direction, amount=0.6, wait=True)
            if res.get("reached_end"):
                break
        raise PhonePilotError("ELEMENT_NOT_FOUND", f"{selector} not found after scrolling")

    async def fling(self, actor: str = "agent", direction: str = "down", container: int | None = None, **args: Any) -> dict[str, Any]:
        x0, y0, x1, y1 = self._scroll_points(direction, None, 0.9)

        async def run(_: Target | None) -> None:
            await self.arbiter.ai_gesture(gesture.fling(x0, y0, x1, y1))

        return await self._act("fling", actor, args, run)

    async def drag(self, actor: str = "agent", to: list[float] | None = None, hold_ms: float = 600, **args: Any) -> dict[str, Any]:
        if not to:
            raise PhonePilotError("ELEMENT_NOT_FOUND", "to=[x_norm,y_norm] required")
        x1, y1 = self._norm_point(to)

        async def run(t: Target | None) -> None:
            assert t is not None
            await self.arbiter.ai_gesture(gesture.drag(t.x, t.y, x1, y1, hold_ms, ease=self.gesture_profile.ease))

        return await self._act("drag", actor, args, run, needs_target=True)

    async def pinch(self, actor: str = "agent", center: list[float] | None = None, scale: float = 2.0, **args: Any) -> dict[str, Any]:
        dw, dh = self.device.display_size
        cx, cy = self._norm_point(center or [500, 500])
        base = min(dw, dh) * 0.25
        start, end = (base, base * scale) if scale >= 1 else (base / max(scale, 0.1), base)

        async def run(_: Target | None) -> None:
            await self.arbiter.ai_gesture(gesture.pinch(cx, cy, start, end))

        return await self._act("pinch", actor, args, run)

    async def _type_unicode(self, text: str) -> None:
        """Non-ASCII text: set clipboard + paste, then restore the previous clipboard (FR-INP-021).

        The app reads the clipboard only when it handles the paste key, which can be long after it was sent
        (e.g. queued behind the DEL keys of `clear`). Restoring too early pastes the old clipboard instead,
        so the restore waits until the text shows up in the focused field.
        """
        prev = self.device_clipboard
        self._clip_seq += 1
        await self.arbiter.ai_raw(p.set_clipboard(self._clip_seq, text, paste=True))
        self.clipboard_used = True
        await self._wait_for_paste(text)
        self._clip_seq += 1
        await self.arbiter.ai_raw(p.set_clipboard(self._clip_seq, prev or "", paste=False))

    async def _wait_for_paste(self, text: str) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + PASTE_TIMEOUT_S
        await asyncio.sleep(0.15)
        want = text.strip()  # some fields trim pasted whitespace
        while loop.time() < deadline:
            obs = await self.observer.observe()
            field_el = next((e for e in obs.elements if e.focused and e.role == "text_field"), None)
            if field_el is None or field_el.password:  # cannot see the field: give the app a fixed head start
                await asyncio.sleep(max(0.0, min(PASTE_FALLBACK_S, deadline - loop.time())))
                return
            if want in field_el.text:
                return
            await asyncio.sleep(0.2)

    async def _clear_field(self, el: UIElement | None) -> None:
        await self.arbiter.ai_key(p.Keycode.MOVE_END)
        n = len(el.text) if el and el.text and not el.password else 64
        for _ in range(min(n, 200)):
            await self.arbiter.ai_key(p.Keycode.DEL)

    async def type_text(self, actor: str = "agent", text: str = "", clear: bool = False, submit: bool = False, **args: Any) -> dict[str, Any]:
        if self.secrets.find_secret_in(text):
            raise PhonePilotError("USE_TYPE_SECRET", "this text contains a secret value")
        has_target = any(args.get(k) is not None for k in ("element", "selector", "x_norm"))

        async def run(t: Target | None) -> dict[str, Any]:
            if t is not None:
                await self.arbiter.ai_gesture(gesture.tap(t.x, t.y, self.gesture_profile.tap_ms))
                await asyncio.sleep(0.3)
            if clear:
                await self._clear_field(t.element if t else None)
            if text.isascii():
                await self.arbiter.ai_text(text)
            else:
                await self._type_unicode(text)
            if submit:
                await self.arbiter.ai_key(p.Keycode.ENTER)
            return {}

        result = await self._act("type_text", actor, args, run, needs_target=False)
        obs = self.observer.last
        if obs is not None and not submit:
            field_el = next((e for e in obs.elements if e.focused and e.role == "text_field"), None)
            if field_el is None and has_target:
                field_el = next((e for e in obs.elements if e.role == "text_field"), None)
            if field_el is not None:
                if field_el.password:
                    result["verified"] = None
                else:
                    actual = field_el.text
                    result["verified"] = actual.endswith(text) or actual == text
                    if not result["verified"]:
                        result["actual_text"] = self.redactor.redact(actual)
        return result

    async def press_key(self, actor: str = "agent", key: str = "enter", meta: list[str] | None = None, repeat: int = 1, **args: Any) -> dict[str, Any]:
        code = KEY_NAMES.get(key.lower())
        if code is None:
            raise PhonePilotError("ELEMENT_NOT_FOUND", f"unknown key {key}; one of {sorted(KEY_NAMES)}")
        meta_state = 0
        for m in meta or []:
            meta_state |= META_NAMES.get(m.lower(), 0)
        args = {**args, "label_hint": key}

        async def run(_: Target | None) -> None:
            for _i in range(max(1, min(repeat, 50))):
                await self.arbiter.ai_key(code, meta_state)

        return await self._act("press_key", actor, args, run)

    async def system_action(self, actor: str = "agent", action: str = "home", **args: Any) -> dict[str, Any]:
        msgs: dict[str, Callable[[], Awaitable[None]]] = {
            "home": lambda: self.arbiter.ai_key(p.Keycode.HOME),
            "back": lambda: self.arbiter.ai_key(p.Keycode.BACK),
            "recents": lambda: self.arbiter.ai_key(p.Keycode.APP_SWITCH),
            "notifications": lambda: self.arbiter.ai_raw(p.simple(p.ControlType.EXPAND_NOTIFICATION_PANEL)),
            "quick_settings": lambda: self.arbiter.ai_raw(p.simple(p.ControlType.EXPAND_SETTINGS_PANEL)),
            "collapse_panels": lambda: self.arbiter.ai_raw(p.simple(p.ControlType.COLLAPSE_PANELS)),
            "screen_on": lambda: self.arbiter.ai_raw(p.set_display_power(True)),
            "screen_off": lambda: self.arbiter.ai_raw(p.set_display_power(False)),
            "rotate": lambda: self.arbiter.ai_raw(p.simple(p.ControlType.ROTATE_DEVICE)),
        }
        fn = msgs.get(action)
        if fn is None:
            raise PhonePilotError("ELEMENT_NOT_FOUND", f"unknown action {action}; one of {sorted(msgs)}")

        async def run(_: Target | None) -> None:
            await fn()

        return await self._act("system_action", actor, {**args, "label_hint": action}, run)

    async def launch_app(self, actor: str = "agent", package: str | None = None, label: str | None = None, activity: str | None = None, **args: Any) -> dict[str, Any]:
        if not package and label:
            for pkg, lab in self.observer.labels.items():
                if lab.lower() == label.lower():
                    package = pkg
                    break
        if not package:
            raise PhonePilotError("ELEMENT_NOT_FOUND", "package (or a known label) required")
        if not all(c.isalnum() or c in "._" for c in package):
            raise PhonePilotError("POLICY_BLOCKED", "invalid package name")

        async def run(_: Target | None) -> None:
            self.arbiter.check_ai_allowed()
            if activity:
                if not all(c.isalnum() or c in "._/$" for c in activity):
                    raise PhonePilotError("POLICY_BLOCKED", "invalid activity name")
                await self.device.shell(f"am start -n {package}/{activity}")
            else:
                await self.device.shell(f"monkey -p {package} -c android.intent.category.LAUNCHER 1")

        result = await self._act("launch_app", actor, {**args, "label_hint": package}, run)
        if self.policy.is_sensitive_app(package, self.observer.app_label(package)):
            result["sensitive_app"] = True
        return result

    async def stop_app(self, actor: str = "agent", package: str = "", **args: Any) -> dict[str, Any]:
        if not package or not all(c.isalnum() or c in "._" for c in package):
            raise PhonePilotError("POLICY_BLOCKED", "invalid package name")

        async def run(_: Target | None) -> None:
            await self.device.shell(f"am force-stop {package}")

        return await self._act("stop_app", actor, {**args, "label_hint": package}, run)

    async def open_url(self, actor: str = "agent", url: str = "", package: str | None = None, **args: Any) -> dict[str, Any]:
        args = {**args, "url": url, "label_hint": url}

        async def run(_: Target | None) -> None:
            cmd = f"am start -a android.intent.action.VIEW -d {shlex.quote(url)}"
            if package:
                if not all(c.isalnum() or c in "._" for c in package):
                    raise PhonePilotError("POLICY_BLOCKED", "invalid package name")
                cmd += f" {package}"
            await self.device.shell(cmd)

        return await self._act("open_url", actor, args, run)

    def _push_allowed(self, path: Path) -> bool:
        rp = path.resolve()
        return any(str(rp).lower().startswith(str(d.resolve()).lower()) for d in self.config.allowed_push_dirs)

    async def push_file(self, actor: str = "agent", local_path: str = "", remote_dir: str | None = None, **args: Any) -> dict[str, Any]:
        path = Path(local_path)
        if not path.is_file() or not self._push_allowed(path):
            raise PhonePilotError("POLICY_BLOCKED", "local_path must be a file inside an allowed folder")
        remote_dir = remote_dir or self.config.device_tmp_dir
        remote = f"{remote_dir.rstrip('/')}/{path.name}"

        async def run(_: Target | None) -> dict[str, Any]:
            await self.device.shell(f"mkdir -p {shlex.quote(remote_dir)}")
            await self.device.push(str(path), remote)
            await self.device.shell(f"am broadcast -a android.intent.action.MEDIA_SCANNER_SCAN_FILE -d {shlex.quote('file://' + remote)}")
            self.pushed_files.append(remote)
            return {"remote_path": remote}

        return await self._act("push_file", actor, {**args, "label_hint": path.name}, run, settle=False)

    async def install_apk(self, actor: str = "agent", local_path: str = "", **args: Any) -> dict[str, Any]:
        path = Path(local_path)
        if not path.is_file() or path.suffix.lower() != ".apk" or not self._push_allowed(path):
            raise PhonePilotError("POLICY_BLOCKED", "local_path must be an .apk inside an allowed folder")
        remote = f"/data/local/tmp/pp_{int(time.time())}.apk"

        async def run(_: Target | None) -> dict[str, Any]:
            await self.device.push(str(path), remote)
            out = await self.device.shell(f"pm install -r {remote}", timeout=120)
            await self.device.shell(f"rm -f {remote}")
            return {"output": out.strip()[:200]}

        return await self._act("install_apk", actor, {**args, "label_hint": path.name}, run, settle=False)

    async def batch_actions(self, actor: str = "agent", actions: list[dict[str, Any]] | None = None, stop_on_error: bool = True, **args: Any) -> dict[str, Any]:
        actions = actions or []
        if len(actions) > 10:
            raise PhonePilotError("POLICY_BLOCKED", "at most 10 actions per batch")
        allowed = {"tap", "type_text", "press_key", "scroll", "swipe", "long_press", "double_tap"}
        # pre-classify everything first: a sensitive action anywhere rejects the whole batch (TC-HIL-03)
        obs = await self.observer.observe()
        for a in actions:
            if a.get("tool") not in allowed:
                raise PhonePilotError("POLICY_BLOCKED", f"tool {a.get('tool')} not allowed in a batch")
            target = await self.resolve_target({**a.get("args", {}), "obs_id": obs.obs_id} if a.get("args", {}).get("element") is not None else a.get("args", {}))
            ctx = ActionContext(
                tool=a["tool"], package=obs.package, app_label=obs.app_label,
                target_label=target.element.label if target and target.element else "", screen_texts=obs.screen_texts(),
                in_batch=True, in_workflow=actor.startswith("workflow:"), args=a.get("args", {}),
            )
            decision = self.policy.classify(ctx)
            if decision.level is not Level.ALLOW:
                raise PhonePilotError("POLICY_BLOCKED", f"batch contains a {decision.category} action; run it on its own", category=decision.category)
        results = []
        for a in actions:
            fn = getattr(self, a["tool"])
            try:
                results.append(await fn(actor, **{**a.get("args", {}), "wait": False}))
            except PhonePilotError as e:
                results.append({"ok": False, **e.to_dict()})
                if stop_on_error:
                    break
        await self.observer.wait_stable(self.config.settle_timeout_s, check_progress=False)
        return {"ok": all(r.get("ok") for r in results), "results": results}

    # ================================================================ waiting
    async def wait_for(self, actor: str = "agent", condition: dict[str, Any] | None = None, timeout_ms: int = 10000) -> dict[str, Any]:
        cond = condition or {}
        timeout = min(max(timeout_ms, 100), 120_000) / 1000
        deadline = time.monotonic() + timeout
        t0 = time.monotonic()
        obs: Observation | None = None
        while True:
            if cond.get("screen_stable"):
                ok = await self.observer.wait_stable(max(deadline - time.monotonic(), 0.1))
                return {"met": ok, "elapsed_ms": int((time.monotonic() - t0) * 1000)}
            obs = await self.observer.observe()
            texts = " \n ".join(obs.screen_texts())
            met = True
            if "text_appears" in cond:
                met &= str(cond["text_appears"]) in texts
            if "text_gone" in cond:
                met &= str(cond["text_gone"]) not in texts
            if "element" in cond:
                met &= bool(uitree.find(obs.elements, cond["element"]))
            if "package" in cond:
                met &= obs.package == cond["package"]
            if "activity_contains" in cond:
                met &= str(cond["activity_contains"]) in obs.activity
            if "keyboard_visible" in cond:
                met &= obs.keyboard_visible == bool(cond["keyboard_visible"])
            if met or time.monotonic() >= deadline:
                return {
                    "met": bool(met),
                    "elapsed_ms": int((time.monotonic() - t0) * 1000),
                    "obs_id": obs.obs_id,
                    "package": obs.package,
                }
            await asyncio.sleep(0.25)

    async def wait_stable(self, actor: str = "agent", timeout_ms: int = 5000) -> dict[str, Any]:
        ok = await self.observer.wait_stable(min(timeout_ms, 60000) / 1000)
        return {"stable": ok}

    async def sleep(self, actor: str = "agent", ms: int = 500) -> dict[str, Any]:
        await asyncio.sleep(min(max(ms, 0), 10_000) / 1000)
        return {"slept_ms": min(max(ms, 0), 10_000)}

    # ================================================================ secrets
    async def list_secret_refs(self, actor: str = "agent", scope: str | None = None) -> dict[str, Any]:
        return {"refs": self.secrets.refs(scope)}

    async def _secret_preconditions(self, ref: str, require_screen_kinds: set[str]) -> tuple[Any, Observation, UIElement | None]:
        secret = self.secrets.get(ref)
        if secret.kind not in require_screen_kinds:
            raise PhonePilotError("SECRET_SCOPE_DENIED", f"{ref} ({secret.kind}) may not be typed on screen")
        obs = await self.observer.observe()
        if secret.meta.allowed_packages and obs.package not in secret.meta.allowed_packages:
            raise PhonePilotError("SECRET_SCOPE_DENIED", f"{ref} is not allowed in {obs.package}")
        if obs.package == "com.android.chrome" and secret.meta.allowed_hosts:
            url_bar = next((e for e in obs.elements if e.resource_id.endswith("url_bar")), None)
            host = (url_bar.text if url_bar else "").split("/")[0].lower()
            if not host or not any(host == h or host.endswith("." + h) for h in secret.meta.allowed_hosts):
                raise PhonePilotError("SECRET_SCOPE_DENIED", "current site is not in allowed_hosts")
        focused = next((e for e in obs.elements if e.focused and e.role == "text_field"), None)
        return secret, obs, focused

    async def type_secret(self, actor: str = "agent", ref: str = "", submit: bool = False, **args: Any) -> dict[str, Any]:
        secret = self.secrets.get(ref)
        args = {**args, "kind": secret.kind, "label_hint": f"secret:{ref}"}

        async def run(t: Target | None) -> dict[str, Any]:
            if t is not None:
                await self.arbiter.ai_gesture(gesture.tap(t.x, t.y, self.gesture_profile.tap_ms))
                await asyncio.sleep(0.3)
            _, obs, focused = await self._secret_preconditions(ref, {"username", "password", "pin"})
            field_el = focused or (t.element if t else None)
            if field_el is None or field_el.role != "text_field":
                raise PhonePilotError("SECRET_SCOPE_DENIED", "no focused text field")
            if secret.meta.needs_password_field() and not field_el.password:
                raise PhonePilotError("SECRET_SCOPE_DENIED", f"{ref} may only go into a password field")
            # TOCTOU: re-check the foreground app right before injecting
            pkg, _ = await self.device.foreground()
            if pkg != obs.package:
                raise PhonePilotError("SECRET_SCOPE_DENIED", "the foreground app changed")
            if not secret.is_ascii():
                raise PhonePilotError("NON_ASCII_SECRET_UNSUPPORTED", "non-ASCII secret needs ADBKeyBoard")
            with secret.reveal() as buf:
                await self.arbiter.ai_secret_text(buf)
            if submit:
                await self.arbiter.ai_key(p.Keycode.ENTER)
            return {"typed": True, "length": secret.length, "field_is_password": field_el.password}

        return await self._act("type_secret", actor, args, run)

    async def type_profile(self, actor: str = "agent", ref: str = "", submit: bool = False, **args: Any) -> dict[str, Any]:
        secret = self.secrets.get(ref)
        if secret.kind not in {"profile", "username"}:
            raise PhonePilotError("SECRET_SCOPE_DENIED", "type_profile only takes profile/username refs")
        args = {**args, "label_hint": f"profile:{ref}"}

        async def run(t: Target | None) -> dict[str, Any]:
            if t is not None:
                await self.arbiter.ai_gesture(gesture.tap(t.x, t.y, self.gesture_profile.tap_ms))
                await asyncio.sleep(0.3)
            await self._secret_preconditions(ref, {"profile", "username"})
            with secret.reveal() as buf:
                if secret.is_ascii():
                    await self.arbiter.ai_secret_text(buf)
                else:
                    await self._type_unicode(bytes(buf).decode("utf-8"))
            if submit:
                await self.arbiter.ai_key(p.Keycode.ENTER)
            return {"typed": True, "length": secret.length}

        return await self._act("type_profile", actor, args, run)

    async def secret_matches_screen(self, actor: str = "agent", ref: str = "", element: int | None = None, obs_id: str | None = None) -> dict[str, Any]:
        secret = self.secrets.get(ref)
        if secret.kind in {"password", "pin", "api_key", "card"}:
            raise PhonePilotError("SECRET_SCOPE_DENIED", "only username/profile refs can be compared")
        obs = self.observer.get(obs_id or "") or await self.observer.observe()
        el = obs.element(int(element)) if element is not None else next((e for e in obs.elements if e.focused), None)
        if el is None:
            raise PhonePilotError("ELEMENT_STALE", "element not found")
        return {"matches": secret.matches(el.text.strip())}

    # ================================================================== human
    async def request_human(self, actor: str = "agent", kind: str = "manual_step", instructions: str = "", timeout_s: float = 600) -> dict[str, Any]:
        self.arbiter.pause_ai(f"human:{kind}")
        self.timeline.add(TimelineEvent(actor, "request_human", kind, "HUMAN", "waiting", instructions))
        resp = await self.approvals.human(HumanRequest(kind, self.redactor.redact(instructions), timeout_s))
        if resp.outcome == "continued":
            self.arbiter.resume_ai()
            self.arbiter.last_human_ts = -1e9
        self.timeline.add(TimelineEvent(actor, "request_human", kind, "HUMAN", resp.outcome))
        return {"outcome": resp.outcome, "note": self.redactor.redact(resp.note)}

    async def request_approval(self, actor: str = "agent", title: str = "", summary: str = "", details: dict[str, Any] | None = None, risk: str = "medium") -> dict[str, Any]:
        level = Level.CONFIRM_HIGH if risk == "high" else Level.CONFIRM
        req = ApprovalRequest(
            title=self.redactor.redact(title or "AI가 확인을 요청합니다"),
            summary=self.redactor.redact(summary),
            decision=PolicyDecision(level, "agent_request", ["requested by agent"]),
            client=actor,
            details=self.redactor.redact_obj(details or {}),
            preview_png=self._preview(None),
        )
        resp = await self.approvals.request(req)
        self.timeline.add(TimelineEvent(actor, "request_approval", title, level.name, "approved"))
        return {"approved": True, "note": resp.note}

    async def notify_user(self, actor: str = "agent", message: str = "", level: str = "info") -> dict[str, Any]:
        msg = self.redactor.redact(message)
        for cb in list(self.notifications):
            try:
                cb(level, msg)
            except Exception:
                pass
        return {"delivered": True}

    # ======================================================= google workflow helpers
    async def capture_backup_codes(self, actor: str = "agent", account_ref: str = "GOOGLE_MAIN", max_scrolls: int = 3) -> dict[str, Any]:
        """Read Google backup codes from the screen and store them in the vault. Codes never leave this method."""
        collected: list[str] = []
        for i in range(max_scrolls + 1):
            obs = await self.observer.observe()
            texts = [e.text or e.label for e in obs.elements] + [str(b["text"]) for b in obs.text_blocks]
            for code in extract_backup_codes(texts):
                if code not in collected:
                    collected.append(code)
            if len(collected) >= 10 or i == max_scrolls:
                break
            await self.scroll(actor, direction="down", amount=0.5)
        if len(collected) != 10:
            self.timeline.add(TimelineEvent(actor, "capture_backup_codes", account_ref, "", f"found {len(collected)}"))
            raise PhonePilotError("WORKFLOW_STEP_FAILED", f"expected 10 backup codes on screen, found {len(collected)}")
        email = ""
        if self.secrets.has(f"{account_ref}_EMAIL"):
            with self.secrets.get(f"{account_ref}_EMAIL").reveal() as buf:
                email = bytes(buf).decode("utf-8")
        path = self.recovery.save(account_ref.replace("GOOGLE_", ""), email, collected)
        collected.clear()
        email = ""
        hint = str(Path(*path.parts[-2:]))
        self.timeline.add(TimelineEvent(actor, "capture_backup_codes", account_ref, "ALLOW", "saved 10"))
        return {"saved": True, "count": 10, "path_hint": hint}

    async def verify_2sv_methods(self, actor: str = "agent") -> dict[str, Any]:
        obs = await self.observer.observe()
        texts = [t.lower() for t in obs.screen_texts()]
        joined = " \n ".join(texts)
        methods = [name for name, words in TWO_SV_ALLOWED.items() if any(w in joined for w in words)]
        phone_hits = [t for t in texts if any(m in t for m in TWO_SV_PHONE_MARKERS)]
        result = {
            "methods": methods,
            "phone_methods": len(phone_hits),
            "phone_markers": phone_hits,
            "two_step_page": any(k in joined for k in ("2단계 인증", "2-step verification")),
        }
        self.timeline.add(TimelineEvent(actor, "verify_2sv_methods", "", "ALLOW", f"phone={len(phone_hits)}"))
        return result

    # ========================================================= skills/workflows
    def _need_skills(self) -> Any:
        if self.skills is None:
            raise PhonePilotError("WORKFLOW_STEP_FAILED", "skill engine not available")
        return self.skills

    async def list_skills(self, actor: str = "agent", tag: str | None = None) -> dict[str, Any]:
        return {"skills": self._need_skills().list(tag)}

    async def get_skill(self, actor: str = "agent", name: str = "") -> dict[str, Any]:
        return self._need_skills().describe(name)

    async def run_workflow(self, actor: str = "agent", name: str = "", inputs: dict[str, Any] | None = None) -> dict[str, Any]:
        # the run acts as "workflow:<skill>"; the caller's own lease would block it (CONTROL_BUSY) until it expired.
        # The run takes the lease on its first action, so other clients are still kept out while it works.
        self.arbiter.release_lease(actor)
        return await self._need_skills().start(name, inputs or {}, requested_by=actor)

    async def workflow_status(self, actor: str = "agent", run_id: str = "") -> dict[str, Any]:
        return self._need_skills().status(run_id)

    async def workflow_control(self, actor: str = "agent", run_id: str = "", action: str = "pause") -> dict[str, Any]:
        return await self._need_skills().control(run_id, action)

    async def checkpoint(self, actor: str = "agent", run_id: str = "", step: str = "", evidence: str = "") -> dict[str, Any]:
        self.timeline.add(TimelineEvent(actor, "checkpoint", step, "ALLOW", "ok", detail={"run_id": run_id, "evidence": evidence[:500]}))
        return {"recorded": True}

    # ================================================================ session
    async def session_info(self, actor: str = "agent") -> dict[str, Any]:
        return {
            "connected": self.device.connected,
            "device": self.device.info.public(),
            "control_state": self.arbiter.state.value,
            "lease_owner": self.arbiter.lease_owner,
            "steps_used": self.steps_used.get(actor, 0),
            "step_budget": self.config.step_budget,
            "active_workflows": self.skills.active() if self.skills else [],
            "health": self.health_report() if self.health_report else None,
        }

    async def get_gesture_profile(self, actor: str = "agent") -> dict[str, Any]:
        """The calibrated gesture defaults in use (read-only; calibration is done by the human in the GUI)."""
        return self.gesture_profile.to_dict()

    async def release_control(self, actor: str = "agent") -> dict[str, Any]:
        self.arbiter.release_lease(actor)
        return {"released": True}
