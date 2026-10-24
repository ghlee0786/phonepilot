"""ADB runtime: bundled adb server, device tracking and device profile (PRD 8.1, 8.4)."""

from __future__ import annotations

import hashlib
import logging
import re
import subprocess
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import adbutils

from phonepilot.core.device import DeviceInfo
from phonepilot.errors import PhonePilotError
from phonepilot.paths import vendor_dir

log = logging.getLogger(__name__)
CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


@dataclass
class DeviceEntry:
    serial: str
    state: str  # device | unauthorized | offline | ...

    @property
    def tail(self) -> str:
        return self.serial[-4:]


def find_adb(configured: str | None = None) -> Path:
    candidates = []
    if configured:
        candidates.append(Path(configured))
    exe = "adb.exe" if sys.platform == "win32" else "adb"
    candidates.append(vendor_dir() / "platform-tools" / exe)
    for c in candidates:
        if c.exists():
            return c
    raise PhonePilotError("ADB_NOT_FOUND", f"adb not found (looked in {', '.join(str(c) for c in candidates)})")


class DeviceManager:
    def __init__(self, adb_path: str | None = None, host: str = "127.0.0.1", port: int = 5037) -> None:
        self.adb_path = find_adb(adb_path)
        self.host = host
        self.port = port
        self.client = adbutils.AdbClient(host=host, port=port)
        self._track_thread: threading.Thread | None = None
        self._stop = threading.Event()

    def _run(self, *args: str, timeout: float = 30) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(self.adb_path), *args], capture_output=True, text=True, timeout=timeout, creationflags=CREATE_NO_WINDOW
        )

    def start_server(self) -> None:
        res = self._run("start-server")
        if res.returncode != 0:
            raise PhonePilotError("ADB_SERVER_CONFLICT", res.stderr.strip()[:200])

    def restart_server(self) -> None:
        """Last-resort recovery: a wedged adb server is replaced. Other adb tools reconnect on their own."""
        self._run("kill-server", timeout=15)
        self.start_server()

    def devices(self) -> list[DeviceEntry]:
        try:
            out = []
            for info in self.client.list(extended=False):
                out.append(DeviceEntry(info.serial, info.state))
            return out
        except Exception as e:  # noqa: BLE001
            raise PhonePilotError("DEVICE_NOT_FOUND", str(e)) from e

    def device(self, serial: str | None = None) -> adbutils.AdbDevice:
        ready = [d for d in self.devices() if d.state == "device"]
        if serial:
            if not any(d.serial == serial for d in ready):
                raise PhonePilotError("DEVICE_NOT_FOUND", f"device {serial[-4:]} not ready")
            return self.client.device(serial)
        if not ready:
            unauthorized = [d for d in self.devices() if d.state == "unauthorized"]
            if unauthorized:
                raise PhonePilotError("DEVICE_UNAUTHORIZED", "USB debugging not authorized on the phone")
            raise PhonePilotError("DEVICE_NOT_FOUND", "no device connected")
        return self.client.device(ready[0].serial)

    def track(self, on_change: Callable[[list[DeviceEntry]], None]) -> None:
        """Push-based tracking via host:track-devices (FR-DEV-003)."""

        def loop() -> None:
            while not self._stop.is_set():
                try:
                    for _event in self.client.track_devices():
                        if self._stop.is_set():
                            return
                        try:
                            on_change(self.devices())
                        except Exception:  # noqa: BLE001
                            pass
                except Exception:  # noqa: BLE001 - adb server restarted; retry
                    self._stop.wait(1.0)

        self._track_thread = threading.Thread(target=loop, name="pp-adb-track", daemon=True)
        self._track_thread.start()

    def stop(self) -> None:
        self._stop.set()

    # --------------------------------------------------------------- wireless
    async def run_async(self, args: list[str], timeout: float) -> tuple[int, str]:
        """Runner for core.wireless.WirelessADB (argument list, never a shell string)."""
        import asyncio

        try:
            res = await asyncio.to_thread(self._run, *args, timeout=timeout)
        except subprocess.TimeoutExpired:
            return 124, "timeout"
        return res.returncode, (res.stdout + res.stderr)

    def wireless(self) -> Any:
        from phonepilot.core.wireless import WirelessADB

        return WirelessADB(self.run_async)


def _prop(dev: adbutils.AdbDevice, name: str) -> str:
    return str(dev.shell(f"getprop {name}", timeout=5)).strip()


def collect_profile(dev: adbutils.AdbDevice) -> DeviceInfo:
    # adb's serial is "ip:port" over Wi-Fi; hash the hardware serial so USB and wireless share a profile
    serial = _prop(dev, "ro.serialno") or _prop(dev, "ro.boot.serialno") or dev.serial
    info = DeviceInfo(
        serial_hash=hashlib.sha256(serial.encode()).hexdigest(),
        serial_tail=serial[-4:],
        manufacturer=_prop(dev, "ro.product.manufacturer"),
        model=_prop(dev, "ro.product.model"),
        release=_prop(dev, "ro.build.version.release"),
    )
    try:
        info.sdk = int(_prop(dev, "ro.build.version.sdk") or 0)
    except ValueError:
        info.sdk = 0
    size = str(dev.shell("wm size", timeout=5))
    m = re.findall(r"(\d+)x(\d+)", size)
    if m:
        w, h = map(int, m[-1])  # override size wins when present
        info.display_w, info.display_h = w, h
    dens = re.findall(r"(\d+)", str(dev.shell("wm density", timeout=5)))
    info.density = int(dens[-1]) if dens else 0
    info.ime = str(dev.shell("settings get secure default_input_method", timeout=5)).strip()
    nav = str(dev.shell("settings get secure navigation_mode", timeout=5)).strip()
    info.nav_mode = {"0": "3-button", "1": "2-button", "2": "gesture"}.get(nav, nav)
    return info


def preflight(dev: adbutils.AdbDevice, info: DeviceInfo) -> list[dict[str, str]]:
    """PRD 8.3 checklist. Returns rows of {item, status(ok|warn|fail|info), detail}."""
    rows = []
    rows.append({"item": "Android 8.0+", "status": "ok" if info.sdk >= 26 else "fail", "detail": f"SDK {info.sdk}"})
    manu = info.manufacturer.lower()
    if manu in {"xiaomi", "redmi", "poco"}:
        rows.append({"item": "입력 주입 권한", "status": "warn", "detail": "개발자 옵션 → 'USB 디버깅(보안 설정)'을 켜야 합니다"})
    if manu in {"oppo", "realme", "oneplus"}:
        rows.append({"item": "입력 주입 권한", "status": "warn", "detail": "'권한 모니터링 비활성화'가 필요할 수 있습니다"})
    rows.append({"item": "오디오 포워딩", "status": "ok" if info.sdk >= 30 else "info", "detail": "Android 11+" if info.sdk >= 30 else "Android 10 이하 미지원"})
    lock = str(dev.shell("dumpsys window | grep -E 'mDreamingLockscreen|isKeyguardShowing|mShowingLockscreen'", timeout=5))
    locked = "true" in lock.lower()
    rows.append({"item": "화면 잠금", "status": "warn" if locked else "ok", "detail": "폰 잠금을 해제하세요" if locked else "해제됨"})
    anim = str(dev.shell("settings get global window_animation_scale", timeout=5)).strip()
    rows.append({"item": "애니메이션 배율", "status": "info", "detail": f"{anim} (AI 작업에는 0.5x 권장)"})
    rows.append({"item": "키보드 앱", "status": "info", "detail": info.ime or "-"})
    return rows
