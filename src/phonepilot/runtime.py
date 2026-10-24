"""CoreRuntime: owns the device session, tools, workflows and the MCP server on a background asyncio thread
(PRD 5.3). The GUI talks to it through `call()` / `submit()` and listener callbacks."""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import threading
import time
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


def recordings_dir() -> Path:
    """Task recordings live in the program's local data folder: never in the repo, never roamed (FR-SKL-014)."""
    return localdata_dir() / "recordings"


def _local_data_file(name: str) -> Path:
    """Personal measurements (gesture profiles) live in %LOCALAPPDATA%; move a copy left in %APPDATA% by older versions."""
    new, old = localdata_dir() / name, appdata_dir() / name
    if not new.exists() and old.exists():
        try:
            new.parent.mkdir(parents=True, exist_ok=True)
            old.replace(new)
        except OSError:
            log.warning("could not move %s to %s", old, new)
            return old
    return new
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


AUTO_CONNECT_STATES = {ConnState.DISCONNECTED, ConnState.FAILED, ConnState.UNAUTHORIZED}


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
        self.last_calibration_samples: list[Any] = []
        self.task_recorder: Any = None
        self.profiles = ProfileStore(_local_data_file("gesture_profiles.yaml"))
        self.session_temp = temp_root() / f"session-{id(self):x}"
        self._reconnect_attempt = 0
        self._tracking = False
        self._stopping = False
        self.wireless = False
        self._pairing: Any = None
        from phonepilot.core.health import IncidentLog

        self.incidents = IncidentLog(localdata_dir() / "incidents.jsonl")
        self.health: Any = None  # HealthMonitor, created with the stack
        self._reconnecting = False

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
        self._start_health()

    async def _connect_real(self) -> DeviceBackend:
        from phonepilot.core.adb import DeviceManager, preflight
        from phonepilot.core.real_device import RealDevice, VideoSettings

        from phonepilot.core.wireless import is_wireless_serial

        if self.manager is None:
            self.manager = DeviceManager(self.config.connection.adb_path)
            await asyncio.to_thread(self.manager.start_server)
        self._ensure_tracking()
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
        dev.on_disconnect = lambda d=dev: self.loop.call_soon_threadsafe(lambda: asyncio.ensure_future(self._on_disconnect(d)))
        dev.on_size = lambda w, h: self.emit("frame_size", {"width": w, "height": h})
        return dev

    # --------------------------------------------------------- auto connect
    def _ensure_tracking(self) -> None:
        """FR-DEV-003: watch adb so a phone that shows up again is connected without a click."""
        if self._tracking or self.manager is None or self.demo:
            return
        self._tracking = True
        self.manager.track(lambda entries: self.loop.call_soon_threadsafe(self._on_devices_changed, entries))

    def _on_devices_changed(self, entries: list[Any]) -> None:
        """Core loop. Plugged back in, or USB debugging just allowed: reconnect if we are not streaming."""
        if self._stopping or self.state not in AUTO_CONNECT_STATES:
            return
        if any(e.state == "device" and (self.serial is None or e.serial == self.serial) for e in entries):
            asyncio.ensure_future(self._auto_connect())

    async def _auto_connect(self) -> None:
        if self.state not in AUTO_CONNECT_STATES:  # another event already started a connect
            return
        try:
            await self.connect()  # sets STARTING before its first await, so concurrent events see it
        except PhonePilotError:
            pass

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
        from phonepilot.core.wireless import validate_code, validate_host_port

        # reject a bad or public address before starting adb (FR-DEV-011)
        host_port, code = validate_host_port(host_port), validate_code(code)
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

    async def _on_disconnect(self, dev: DeviceBackend | None = None) -> None:
        if self._stopping or self.demo:
            return
        if dev is not None and dev is not self.device:
            return  # an old session we closed ourselves while reconnecting
        if self._reconnecting or (self.health is not None and self.health.recovering):
            return  # the HealthMonitor is already on it
        self._reconnecting = True
        inc = self.incidents.open("DISCONNECTED")
        try:
            ok = await self._reconnect_after_drop(inc)
        finally:
            self._reconnecting = False
            self.incidents.close(inc, ok)
            self.emit("health", self.health_report())

    async def _reconnect_after_drop(self, inc: Any) -> bool:
        old = self.device
        if old is not None:
            await self._close_quietly(old)
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
            inc.actions.append("reconnect")
            try:
                await self.connect()
                return True
            except PhonePilotError:
                continue
        inc.actions.append("gave_up")
        self._set_state(ConnState.FAILED, "DEVICE_OFFLINE")
        return False

    # ------------------------------------------------------- health (PRD 25)
    def _start_health(self) -> None:
        from phonepilot.core.health import HealthConfig, HealthMonitor

        hs = self.config.health
        if not hs.self_heal or self.stack is None:
            return
        if self.health is None:
            stack = self.stack
            self.health = HealthMonitor(
                get_device=lambda: self.device if self.state == ConnState.STREAMING else None,
                get_last_input=lambda: stack.arbiter.last_input_ts,
                reconnect=self.reconnect_session,
                restart_adb=self._restart_adb_and_reconnect,
                incidents=self.incidents,
                cfg=HealthConfig(interval=hs.interval_s, deep_every=hs.deep_check_s, heartbeat_every=hs.heartbeat_s),
                on_change=lambda: self.emit("health", self.health_report()),
            )
        self.health.start()

    def health_report(self) -> dict[str, Any]:
        rep = self.health.report() if self.health is not None else {
            "ok": self.state == ConnState.STREAMING, "recovering": False, "problems": [], "seconds_since_check": None,
            "recent_incidents": [i.summary() for i in self.incidents.recent(5)],
        }
        rep["state"] = self.state.value
        return rep

    async def check_health(self) -> dict[str, Any]:
        """[지금 점검]: one full round including the adb-level checks."""
        if self.health is not None:
            await self.health.check(deep=True)
        return self.health_report()

    async def _close_quietly(self, dev: DeviceBackend) -> None:
        try:
            await asyncio.wait_for(dev.close(), 10)
        except Exception as e:  # noqa: BLE001 - it is being replaced anyway
            log.warning("closing the old session failed: %s", e)

    async def reconnect_session(self, reason: str = "manual") -> bool:
        """Close the current session (server, forward, decoder) and start a fresh one. Used by the HealthMonitor and
        by [다시 연결]; a frozen-but-connected session would otherwise never be replaced."""
        if self._reconnecting:
            return False
        self._reconnecting = True
        try:
            old = self.device
            self._set_state(ConnState.RECONNECTING, reason)
            if old is not None:
                await self._close_quietly(old)
            try:
                await self.connect()
                return True
            except PhonePilotError as e:
                self._set_state(ConnState.FAILED, e.code)
                return False
        finally:
            self._reconnecting = False

    async def _restart_adb_and_reconnect(self) -> bool:
        if self.demo or self.manager is None:
            return False
        try:
            await asyncio.to_thread(self.manager.restart_server)
        except Exception as e:  # noqa: BLE001
            log.warning("adb restart failed: %s", e)
            return False
        await asyncio.sleep(1.0)
        return await self.reconnect_session("ADB_RESTART")

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
            auto_approve_permissions=cfg.safety.approval_mode == "auto",
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
        stack.registry.recorded_dir = recordings_dir()
        stack.tools.health_report = self.health_report
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
        self.last_calibration_samples = samples
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

    def fidelity_report(self) -> dict[str, Any]:
        """Human vs AI gesture similarity for the last calibration recording (FR-INP-035..039)."""
        from phonepilot.core import fidelity

        prof = self.stack.tools.gesture_profile if self.stack is not None else None
        if prof is None:
            from phonepilot.core.calibration import GestureProfile

            prof = GestureProfile()
        rep = fidelity.report(self.last_calibration_samples, prof)
        rep["text"] = fidelity.format_report(rep)
        return rep

    # ------------------------------------------------------ task recording
    def _refresh_observation(self) -> None:
        """After each recorded step, observe the new screen so the next tap resolves to the right element."""
        if self.stack is None:
            return
        obs = self.stack.observer

        rec = self.task_recorder

        async def refresh() -> None:
            try:
                await obs.wait_stable(timeout=2.0)
                await obs.observe()
                if rec is not None:
                    rec.mark_fresh()
            except Exception:  # recording falls back to coordinates for this step
                log.debug("observation refresh during recording failed", exc_info=True)

        asyncio.run_coroutine_threadsafe(refresh(), self.loop)

    def start_task_recording(self) -> None:
        """FR-SKL-014: record the human's demonstration in the mirror window."""
        from phonepilot.skills.recorder import TaskRecorder

        if self.stack is None or self.device is None:
            raise PhonePilotError("DEVICE_NOT_FOUND", "기기가 연결되어 있지 않습니다")
        stack, device = self.stack, self.device
        rec = TaskRecorder(observation=lambda: stack.observer.last, display_size=lambda: device.display_size)
        rec.on_event = self._refresh_observation
        rec.start()
        self.task_recorder = rec  # before the first refresh, which marks this recorder fresh
        stack.arbiter.task_recorder = rec
        self._refresh_observation()
        self.emit("task_recording", {"active": True, "counts": {}})

    def task_recording_counts(self) -> dict[str, int]:
        return self.task_recorder.counts() if self.task_recorder is not None else {}

    def finish_task_recording(self, name: str, description: str = "", save: bool = True) -> dict[str, Any]:
        """Turn the recording into a user skill, approve it (the user made it) and reload the registry."""
        from phonepilot.skills import loader
        from phonepilot.skills.recorder import dump, safe_skill_name

        rec = self.task_recorder
        if self.stack is not None:
            self.stack.arbiter.task_recorder = None
        self.task_recorder = None
        if rec is None:
            return {"saved": False, "steps": 0}
        events = rec.stop()
        if not save or not events:
            self.emit("task_recording", {"active": False, "saved": False})
            return {"saved": False, "steps": len(events)}
        skill_name = safe_skill_name(name)
        text = dump(rec.to_skill(skill_name, description))
        loaded = loader.parse(text)
        # the folder the registry loads recordings from, so the new skill shows up right away
        rec_dir = self.stack.registry.recorded_dir if self.stack is not None and self.stack.registry.recorded_dir else recordings_dir()
        path = rec_dir / f"{skill_name.split('.', 1)[1]}.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        if self.stack is not None:
            self.stack.registry.approve(loaded.sha256)
        result = {
            "saved": True,
            "name": skill_name,
            "path": str(path),
            "steps": len(loaded.skill.steps),
            "inputs": sorted(loaded.skill.inputs),
        }
        self.emit("task_recording", {"active": False, **result})
        return result

    # ------------------------------------------------------- file drop (FR-INP-053/054)
    def set_drop_dir(self, remote_dir: str) -> str:
        """Change where dropped files go. Shared storage only, so a drop can never write into app or system folders."""
        from phonepilot.config import save_config

        d = "/" + remote_dir.strip().strip("/")
        if not (d == "/sdcard" or d.startswith(("/sdcard/", "/storage/"))) or ".." in d.split("/"):
            raise PhonePilotError("POLICY_BLOCKED", "폰의 공용 저장소(/sdcard/...) 안의 폴더만 지정할 수 있습니다")
        self.config.transfer.drop_dir = d
        save_config(self.config)
        return d

    def set_approval_mode(self, mode: str) -> str:
        """Manual / auto approval. Auto only skips the system permission dialog's "allow" (PolicyDecision.permission_only)."""
        from phonepilot.config import save_config

        if mode not in ("manual", "auto"):
            raise PhonePilotError("POLICY_BLOCKED", f"unknown approval mode {mode!r}")
        self.config.safety.approval_mode = mode  # type: ignore[assignment]
        save_config(self.config)
        if self.stack is not None:
            self.stack.tools.config.auto_approve_permissions = mode == "auto"
        self.emit("approval_mode", {"mode": mode})
        return mode

    @staticmethod
    def _drop_items(paths: list[str]) -> list[tuple[Path, str]]:
        """(local file, path relative to the drop folder). A dropped folder keeps its own name and subfolders."""
        out: list[tuple[Path, str]] = []
        for raw in paths:
            p = Path(raw)
            if p.is_file():
                out.append((p, p.name))
            elif p.is_dir():
                out += [(f, f.relative_to(p.parent).as_posix()) for f in sorted(p.rglob("*")) if f.is_file()]
        return out

    async def push_dropped(self, paths: list[str]) -> dict[str, Any]:
        """Copy files the person dropped on the mirror into the drop folder. They are the person's files, so unlike
        AI pushes they are kept when the session ends. Existing names get a ' (n)' suffix instead of being overwritten."""
        import shlex

        from phonepilot.agent.timeline import TimelineEvent

        if self.device is None or self.stack is None:
            raise PhonePilotError("DEVICE_NOT_FOUND", "기기가 연결되어 있지 않습니다")
        items = self._drop_items(paths)
        if not items:
            raise PhonePilotError("ELEMENT_NOT_FOUND", "보낼 파일이 없습니다")
        base = self.config.transfer.drop_dir.rstrip("/")
        done: list[str] = []
        for i, (local, rel) in enumerate(items, 1):
            self.emit("transfer", {"index": i, "total": len(items), "name": rel, "done": False})
            remote = await self._free_remote_path(f"{base}/{rel}")
            parent = remote.rsplit("/", 1)[0]
            t0 = time.monotonic()
            await self.device.shell(f"mkdir -p {shlex.quote(parent)}")
            await self.device.push(str(local), remote)
            await self.device.shell(f"am broadcast -a android.intent.action.MEDIA_SCANNER_SCAN_FILE -d {shlex.quote('file://' + remote)}")
            self.stack.timeline.add(TimelineEvent("human", "push_file", local.name, "ALLOW", "ok", base, int((time.monotonic() - t0) * 1000)))
            done.append(remote)
        result = {"count": len(done), "remote_dir": base, "files": done}
        self.emit("transfer", {"index": len(done), "total": len(items), "done": True, **result})
        return result

    async def _free_remote_path(self, remote: str) -> str:
        import shlex

        stem, dot, ext = remote.rpartition(".") if "." in remote.rsplit("/", 1)[-1] else (remote, "", "")
        candidate, n = remote, 1
        while (await self.device.shell(f"[ -e {shlex.quote(candidate)} ] && echo y || echo n")).strip() == "y":
            candidate = f"{stem} ({n}){dot}{ext}"
            n += 1
        return candidate

    async def install_dropped_apk(self, path: str) -> dict[str, Any]:
        """FR-INP-054: the person confirmed in the PhonePilot window; the AI's install_apk tool keeps its approval gate."""
        from phonepilot.agent.timeline import TimelineEvent

        p = Path(path)
        if self.device is None or self.stack is None:
            raise PhonePilotError("DEVICE_NOT_FOUND", "기기가 연결되어 있지 않습니다")
        if not p.is_file() or p.suffix.lower() != ".apk":
            raise PhonePilotError("POLICY_BLOCKED", "APK 파일이 아닙니다")
        remote = f"/data/local/tmp/pp_drop_{int(time.time())}.apk"
        await self.device.push(str(p), remote)
        try:
            out = await self.device.shell(f"pm install -r {remote}", timeout=180)
        finally:
            await self.device.shell(f"rm -f {remote}")
        ok = "Success" in out
        self.stack.timeline.add(TimelineEvent("human", "install_apk", p.name, "ALLOW", "ok" if ok else "failed", out.strip()[:120]))
        if not ok:
            raise PhonePilotError("DEVICE_OFFLINE", f"설치 실패: {out.strip()[:200]}")
        return {"installed": p.name}

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
        # log_config=None: uvicorn's default config writes to sys.stderr, which a windowed (no console) build lacks;
        # its loggers go through our redacting ring handler instead
        config = uvicorn.Config(app, host="127.0.0.1", port=self.config.mcp.port, log_level="warning", lifespan="on", log_config=None)
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
                    # another PhonePilot window (or an installed copy) may still be streaming from the same jar
                    own = getattr(getattr(device, "_server", None), "scid", None)
                    others = await device.shell("ps -A -o ARGS | grep 'com.genymobile.scrcpy.Server' | grep -v grep")
                    if not [ln for ln in others.splitlines() if own is None or f"scid={own:08x}" not in ln]:
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
        if self.health is not None:
            self.health.stop()
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
