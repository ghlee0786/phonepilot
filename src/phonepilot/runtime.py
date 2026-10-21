"""CoreRuntime: owns the device session, tools, workflows and the MCP server on a background asyncio thread
(PRD 5.3). The GUI talks to it through `call()` / `submit()` and listener callbacks."""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import threading
from collections.abc import Awaitable, Callable, Coroutine
from enum import Enum
from pathlib import Path
from typing import Any, TypeVar

from phonepilot.agent.tools import ToolConfig
from phonepilot.config import Config, load_config
from phonepilot.core.device import DeviceBackend
from phonepilot.core.scrcpy import protocol as p
from phonepilot.errors import PhonePilotError
from phonepilot.paths import appdata_dir, expand, localdata_dir, repo_root, temp_root
from phonepilot.security.cleanup import DEVICE_TMP_DIR, CleanupReport, SessionCleanup, sweep_temp_root
from phonepilot.security.policy import PolicyEngine
from phonepilot.security.secrets import SecretVault
from phonepilot.testing.harness import Stack, build_stack

log = logging.getLogger(__name__)
T = TypeVar("T")


class ConnState(str, Enum):
    DISCONNECTED = "DISCONNECTED"
    DETECTED = "DETECTED"
    UNAUTHORIZED = "UNAUTHORIZED"
    STARTING = "STARTING"
    STREAMING = "STREAMING"
    RECONNECTING = "RECONNECTING"
    FAILED = "FAILED"
    STOPPING = "STOPPING"


class CoreRuntime:
    def __init__(self, config: Config | None = None, *, demo: bool = False, serial: str | None = None) -> None:
        self.config = config or load_config()
        self.demo = demo
        self.serial = serial
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run_loop, name="pp-core", daemon=True)
        self.stack: Stack | None = None
        self.device: DeviceBackend | None = None
        self.manager: Any = None
        self.state = ConnState.DISCONNECTED
        self.state_detail = ""
        self.mcp_token: str | None = None
        self.mcp_server: Any = None
        self.agent: Any = None
        self.mcp_url = f"http://127.0.0.1:{self.config.mcp.port}/mcp"
        self._uvicorn: Any = None
        self.listeners: list[Callable[[str, dict[str, Any]], None]] = []
        self.secrets = SecretVault()
        self.preflight_rows: list[dict[str, str]] = []
        from phonepilot.core.calibration import CalibrationRecorder, ProfileStore

        self.recorder = CalibrationRecorder()
        self.profiles = ProfileStore(appdata_dir() / "gesture_profiles.yaml")
        self.session_temp = temp_root() / f"session-{id(self):x}"
        self._reconnect_attempt = 0
        self._stopping = False
        self.wireless = False
        self._pairing: Any = None

    # ------------------------------------------------------------ threading
    def _run_loop(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def start(self) -> None:
        if not self.thread.is_alive():
            self.thread.start()

    def call(self, coro: Coroutine[Any, Any, T]) -> concurrent.futures.Future[T]:
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def call_sync(self, coro: Coroutine[Any, Any, T], timeout: float | None = 30) -> T:
        return self.call(coro).result(timeout)

    def emit(self, event: str, data: dict[str, Any] | None = None) -> None:
        for cb in list(self.listeners):
            try:
                cb(event, data or {})
            except Exception:  # noqa: BLE001
                pass

    def _set_state(self, state: ConnState, detail: str = "") -> None:
        self.state = state
        self.state_detail = detail
        self.emit("conn_state", {"state": state.value, "detail": detail})

    # -------------------------------------------------------------- secrets
    def env_path(self) -> Path:
        return expand(self.config.privacy.env_path)

    def load_secrets(self) -> list[str]:
        self.secrets.load(self.env_path())
        if self.stack is not None:
            self.stack.redactor.refresh(self.secrets)
        return self.secrets.warnings

    # -------------------------------------------------------------- connect
    async def connect(self) -> None:
        sweep_temp_root(temp_root())
        self.session_temp.mkdir(parents=True, exist_ok=True)
        self._set_state(ConnState.STARTING)
        try:
            if self.demo:
                from phonepilot.testing.fake_device import FakeDevice, demo_screens

                device: DeviceBackend = FakeDevice(demo_screens(), "home")
            else:
                device = await self._connect_real()
        except PhonePilotError as e:
            self._set_state(ConnState.UNAUTHORIZED if e.code == "DEVICE_UNAUTHORIZED" else ConnState.FAILED, e.code)
            raise
        self.device = device
        if self.stack is None:
            self._build_stack(device)
        else:
            self._rebind(device)
        self._apply_gesture_profile()
        self._set_state(ConnState.STREAMING, getattr(device, "device_name", "") or device.info.model)
        self._reconnect_attempt = 0

    async def _connect_real(self) -> DeviceBackend:
        from phonepilot.core.adb import DeviceManager, preflight
        from phonepilot.core.real_device import RealDevice, VideoSettings

        from phonepilot.core.wireless import is_wireless_serial

        if self.manager is None:
            self.manager = DeviceManager(self.config.connection.adb_path)
            await asyncio.to_thread(self.manager.start_server)
        try:
            adb_dev = await asyncio.to_thread(self.manager.device, self.serial)
        except PhonePilotError as e:
            # FR-DEV-010: no USB device -> look for paired phones on the Wi-Fi (their port changes every time)
            if e.code != "DEVICE_NOT_FOUND" or not self.config.connection.wireless_auto_reconnect:
                raise
            found = await self.manager.wireless().reconnect_known()
            if not found:
                raise
            adb_dev = await asyncio.to_thread(self.manager.device, self.serial)
        v = self.config.video.apply_preset() if self.config.video.preset else self.config.video
        bit_rate, max_size = v.bit_rate_mbps, v.max_size
        self.wireless = is_wireless_serial(adb_dev.serial)
        if self.wireless:  # FR-MIR-012: Wi-Fi defaults to a lighter stream
            bit_rate = min(bit_rate, self.config.connection.wireless_bit_rate_mbps)
            max_size = min(max_size or 99999, self.config.connection.wireless_max_size)
        dev = RealDevice(adb_dev, VideoSettings(v.codec, max_size, bit_rate, v.max_fps, v.decoder, v.encoder))
        self.preflight_rows = await asyncio.to_thread(preflight, adb_dev, dev.info)
        self.emit("preflight", {"rows": self.preflight_rows})
        sizes = [max_size, 1600, 1280, 1024, 800]
        last: Exception | None = None
        for size in sizes:  # FR-MIR-019: step down when the encoder refuses a size
            try:
                await dev.start(max_size=size)
                break
            except PhonePilotError as e:
                last = e
                await dev.close()
                if e.code != "SERVER_START_TIMEOUT":
                    raise
        else:
            raise last or PhonePilotError("ENCODER_FAILED", "could not start video")
        dev.on_disconnect = lambda: self.loop.call_soon_threadsafe(lambda: asyncio.ensure_future(self._on_disconnect()))
        dev.on_size = lambda w, h: self.emit("frame_size", {"width": w, "height": h})
        return dev

    # -------------------------------------------------------------- wireless
    def _wireless_adb(self) -> Any:
        from phonepilot.core.adb import DeviceManager

        if self.manager is None:
            self.manager = DeviceManager(self.config.connection.adb_path)
            self.manager.start_server()
        return self.manager.wireless()

    async def wireless_pair_qr(self, service: str, password: str, timeout: float = 90) -> dict[str, Any]:
        """PRD 8.2 QR pairing. The password stays in memory only."""
        w = await asyncio.to_thread(self._wireless_adb)
        self._pairing = w
        res = await w.pair_qr(service, password, timeout, on_status=lambda m: self.emit("pairing", {"status": m}))
        self._pairing = None
        self.emit("pairing", {"status": res.message, "done": True, "ok": res.ok, "address": res.address})
        return {"ok": res.ok, "message": res.message, "address": res.address}

    async def wireless_pair_code(self, host_port: str, code: str) -> dict[str, Any]:
        w = await asyncio.to_thread(self._wireless_adb)
        self._pairing = w
        try:
            res = await w.pair_code(host_port, code, on_status=lambda m: self.emit("pairing", {"status": m}))
        finally:
            self._pairing = None
        self.emit("pairing", {"status": res.message, "done": True, "ok": res.ok, "address": res.address})
        return {"ok": res.ok, "message": res.message, "address": res.address}

    async def wireless_reconnect(self) -> list[str]:
        w = await asyncio.to_thread(self._wireless_adb)
        return await w.reconnect_known()

    def cancel_pairing(self) -> None:
        if self._pairing is not None:
            self.loop.call_soon_threadsafe(self._pairing.cancel)

    async def _on_disconnect(self) -> None:
        if self._stopping or self.demo:
            return
        stack = self.stack
        if stack is not None:
            stack.arbiter.pause_ai("disconnected")
            for run in stack.engine.runs.values():
                if run.state.value in {"RUNNING", "WAITING_HUMAN", "WAITING_APPROVAL"}:
                    from phonepilot.skills.engine import RunState

                    run.state = RunState.PAUSED_DISCONNECTED
        for delay in (0.5, 1, 2, 4, 8):
            self._reconnect_attempt += 1
            self._set_state(ConnState.RECONNECTING, f"{self._reconnect_attempt}/5")
            await asyncio.sleep(delay)
            if self.wireless and self.manager is not None and self.config.connection.wireless_auto_reconnect:
                try:
                    await self.manager.wireless().reconnect_known()
                except Exception:  # noqa: BLE001
                    pass
            try:
                await self.connect()
                return
            except PhonePilotError:
                continue
        self._set_state(ConnState.FAILED, "DEVICE_OFFLINE")

    def _build_stack(self, device: DeviceBackend) -> None:
        cfg = self.config
        root = repo_root()
        try:
            self.load_secrets()
        except Exception as e:  # noqa: BLE001
            log.warning("could not load .env: %s", e)
        policy = PolicyEngine.from_files(root / cfg.safety.keywords_file, root / cfg.safety.sensitive_apps_file)
        tool_cfg = ToolConfig(
            step_budget=cfg.agent.step_budget,
            engagement_daily=cfg.safety.limits.engagement_daily,
            in_app_purchase_daily_krw=cfg.safety.limits.in_app_purchase_daily_krw,
            allowed_push_dirs=[expand(d) for d in cfg.allowed_push_dirs],
        )
        stack = build_stack(
            device,
            self.secrets,
            state_dir=localdata_dir(),
            vault_dir=expand(cfg.recovery_vault.dir),
            skills_dir=root / "skills",
            config=tool_cfg,
            policy=policy,
        )
        stack.recovery.fmt = cfg.recovery_vault.format
        stack.approvals.timeout_s = cfg.safety.approval_timeout_s
        stack.approvals.on_flood = lambda: stack.arbiter.pause_ai("approval_flood")
        stack.approvals.on_stop_all = self.kill
        stack.observer.send_images_in_sensitive_apps = cfg.agent.send_images_in_sensitive_apps
        stack.observer.screenshot_max_edge = cfg.agent.screenshot_max_edge
        stack.registry.user_dir = appdata_dir() / "skills"
        stack.registry.approved_file = appdata_dir() / "approved_skills.json"
        stack.registry.reload()
        stack.timeline.listeners.append(lambda ev: self.emit("timeline", {"event": ev}))
        stack.arbiter.listeners.append(lambda ev, d: self.emit("control", {"event": ev, **d, "state": stack.arbiter.state.value}))
        stack.engine.listeners.append(lambda run: self.emit("workflow", run.to_dict()))
        stack.tools.notifications.append(lambda level, msg: self.emit("notify", {"level": level, "message": msg}))
        self.stack = stack
        from phonepilot.logging_setup import set_redactor

        set_redactor(stack.redactor)
        self._bind_device_callbacks(device)
        self._setup_integrations()

    # ---------------------------------------------------------- calibration
    def _device_key(self) -> str:
        return self.device.info.serial_hash if self.device is not None else ""

    def _apply_gesture_profile(self) -> None:
        if self.stack is None or self.device is None:
            return
        self.stack.arbiter.recorder = self.recorder
        self.stack.tools.gesture_profile = self.profiles.get(self._device_key())
        self.emit("gesture_profile", self.stack.tools.gesture_profile.to_dict())

    def start_calibration(self) -> None:
        """FR-INP-035: record the human's own gestures in the mirror window."""
        self.recorder.start()
        self.emit("calibration", {"active": True, "counts": self.recorder.counts()})

    def calibration_counts(self) -> dict[str, int]:
        return self.recorder.counts()

    def finish_calibration(self, save: bool = True) -> dict[str, Any]:
        """FR-INP-036/037: build the median profile and (optionally) store it for this device."""
        from phonepilot.core.calibration import GestureProfile, build_profile

        samples = self.recorder.stop()
        base = self.stack.tools.gesture_profile if self.stack is not None else GestureProfile()
        profile = build_profile(samples, base)
        if save and profile.calibrated and self.device is not None:
            self.profiles.save(self._device_key(), profile)
            if self.stack is not None:
                self.stack.tools.gesture_profile = profile
        self.emit("calibration", {"active": False, "profile": profile.to_dict(), "saved": save and profile.calibrated})
        return profile.to_dict()

    def reset_calibration(self) -> None:
        from phonepilot.core.calibration import GestureProfile

        if self.device is not None:
            self.profiles.reset(self._device_key())
        if self.stack is not None:
            self.stack.tools.gesture_profile = GestureProfile()
        self.emit("gesture_profile", GestureProfile().to_dict())

    def _rebind(self, device: DeviceBackend) -> None:
        assert self.stack is not None
        s = self.stack
        for obj in (s, s.arbiter, s.observer, s.tools):
            obj.device = device  # type: ignore[attr-defined]
        self._bind_device_callbacks(device)

    def _bind_device_callbacks(self, device: DeviceBackend) -> None:
        stack = self.stack
        assert stack is not None

        def on_clip(text: str) -> None:
            stack.tools.device_clipboard = text
            obs = stack.observer.last
            sensitive = bool(obs and (obs.sensitive_app or obs.sensitive_screen))
            if self.config.privacy.clipboard_sync and not sensitive:
                self.emit("device_clipboard", {"text": text})

        if hasattr(device, "on_clipboard"):
            device.on_clipboard = on_clip  # type: ignore[attr-defined]

    def _setup_integrations(self) -> None:
        assert self.stack is not None
        cfg = self.config.integrations
        integ: dict[str, Any] = {}
        try:
            if cfg.youtube.enabled and self.secrets.has("YOUTUBE_API_KEY"):
                from phonepilot.integrations.youtube import YouTubeClient

                integ["youtube"] = YouTubeClient(self.secrets.get("YOUTUBE_API_KEY"))
            market: dict[str, Any] = {}
            if cfg.upbit.enabled or cfg.kis.enabled:
                from phonepilot.integrations.orders import LimitsLedger, OrderGate, OrderLimits

                lim = self.config.safety.limits
                from decimal import Decimal

                gate = OrderGate(
                    OrderLimits(
                        order_max_krw=Decimal(lim.order_max_krw),
                        order_daily_krw=Decimal(lim.order_daily_krw),
                        order_daily_count=lim.order_daily_count,
                        allowed_symbols=list(self.config.safety.allowed_symbols),
                    ),
                    LimitsLedger(localdata_dir() / "order_ledger.json"),
                )
                if cfg.upbit.enabled and self.secrets.has("UPBIT_MAIN_ACCESS_KEY"):
                    from phonepilot.integrations.upbit import UpbitClient

                    market["upbit"] = UpbitClient(self.secrets.get("UPBIT_MAIN_ACCESS_KEY"), self.secrets.get("UPBIT_MAIN_SECRET_KEY"), gate)
                if cfg.kis.enabled and self.secrets.has("KIS_MAIN_APP_KEY"):
                    from phonepilot.integrations.kis import KisClient

                    market["kis"] = KisClient(
                        self.secrets.get("KIS_MAIN_APP_KEY"), self.secrets.get("KIS_MAIN_APP_SECRET"), self.secrets.get("KIS_MAIN_ACCOUNT_NO"), gate, env=cfg.kis.env
                    )
            if market:
                integ["market"] = market
        except Exception as e:  # noqa: BLE001
            log.warning("integration setup failed: %s", e)
        self.stack.tools.integrations = integ

    # ---------------------------------------------------------------- mcp
    @property
    def mcp_started(self) -> bool:
        return self._uvicorn is not None

    async def start_mcp(self) -> str:
        import uvicorn

        from phonepilot.agent.mcp_server import build_server, http_app, load_or_create_token

        assert self.stack is not None
        server = build_server(self.stack.tools, end_session=self.end_session)
        self.mcp_server = server
        self._setup_agent()
        self.mcp_token = load_or_create_token(localdata_dir() / "mcp.token", self.config.mcp.token_fixed)
        _, app = http_app(server, self.mcp_token)
        config = uvicorn.Config(app, host="127.0.0.1", port=self.config.mcp.port, log_level="warning", lifespan="on")
        self._uvicorn = uvicorn.Server(config)
        self.loop.create_task(self._uvicorn.serve())
        self.emit("mcp", {"url": self.mcp_url})
        return self.mcp_url

    def _setup_agent(self) -> None:
        """Built-in agent + workflow agent_step runner, when Claude credentials exist (PRD 9.8)."""
        import os

        assert self.stack is not None
        if not (self.secrets.has("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
            self.agent = None
            return
        from phonepilot.agent.builtin_agent import BuiltinAgent

        self.agent = BuiltinAgent(self.stack.tools, self.mcp_server, model=self.config.agent.model, budget_usd=self.config.agent.session_budget_usd)
        self.agent.listeners.append(lambda ev: self.emit("agent", {"kind": ev.kind, **ev.data}))
        self.stack.engine.agent_runner = self.agent.workflow_runner()

    async def ask_agent(self, goal: str) -> str:
        if getattr(self, "agent", None) is None:
            raise PhonePilotError("INTEGRATION_AUTH_FAILED", "ANTHROPIC_API_KEY 가 .env 또는 환경 변수에 없습니다")
        return await self.agent.run(goal)

    # ---------------------------------------------------------------- safety
    async def kill(self) -> None:
        if self.stack is None:
            return
        await self.stack.arbiter.kill()
        self.stack.engine.abort_all()
        self.stack.approvals.deny_pending()
        for d in list(self.stack.tools.integrations.get("_drafts", {})):
            self.stack.tools.integrations["_drafts"].pop(d, None)
        self.emit("killed", {})

    # ---------------------------------------------------------------- cleanup
    async def end_session(self) -> CleanupReport:
        stack, device = self.stack, self.device
        cleanup = SessionCleanup()
        if stack is not None:

            async def stop_ai() -> None:
                await stack.arbiter.kill()
                stack.engine.abort_all()

            cleanup.add("AI 중지·포인터 닫기", stop_ai)
        if device is not None and stack is not None:
            if self.config.privacy.clear_device_clipboard_on_exit and stack.tools.clipboard_used:

                async def clear_clip() -> None:
                    await device.send_control(p.set_clipboard(999_999, "", paste=False))

                cleanup.add("폰 클립보드 비우기", clear_clip)

            async def rm_tmp() -> None:
                await device.shell(f"rm -rf {DEVICE_TMP_DIR}")

            cleanup.add("폰 임시 파일 삭제", rm_tmp)
            if self.config.privacy.close_recents_on_exit:

                async def home() -> None:
                    await device.send_control(p.inject_keycode(p.KeyAction.DOWN, p.Keycode.HOME))
                    await device.send_control(p.inject_keycode(p.KeyAction.UP, p.Keycode.HOME))

                cleanup.add("홈으로", home)
            if self.config.connection.remove_server_on_exit:

                async def rm_server() -> None:
                    await device.shell("rm -f /data/local/tmp/phonepilot-server.jar")

                cleanup.add("서버 파일 삭제", rm_server)

        async def close_dev() -> None:
            self._stopping = True
            if device is not None:
                await device.close()

        cleanup.add("포워딩·서버 종료", close_dev)

        async def wipe_memory() -> None:
            if stack is not None:
                stack.timeline.clear()
                stack.device.framebus.clear()
                stack.observer.last = None
                stack.redactor.clear()
            self.secrets.zero_all()

        cleanup.add("메모리 정리·비밀값 제로화", wipe_memory)

        async def wipe_temp() -> None:
            import shutil

            shutil.rmtree(self.session_temp, ignore_errors=True)

        cleanup.add("PC 임시 폴더 삭제", wipe_temp)
        report = await cleanup.run()
        self.emit("cleanup", {"summary": report.summary, "done": report.done, "failed": report.failed})
        self._set_state(ConnState.DISCONNECTED)
        return report

    async def shutdown(self) -> None:
        if self.device is not None or self.stack is not None:
            await self.end_session()
        if self._uvicorn is not None:
            self._uvicorn.should_exit = True
        if self.manager is not None:
            self.manager.stop()

    def stop_loop(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)


def run_coro(fn: Callable[[], Awaitable[T]]) -> T:
    return asyncio.run(fn())  # type: ignore[arg-type]
