"""Wireless debugging (Android 11+): QR / pairing-code pairing and mDNS reconnect (PRD 8.2, FR-DEV-010..013).

QR flow (same as Android Studio):
  1. PC shows `WIFI:T:ADB;S:<service>;P:<password>;;` as a QR code.
  2. Phone: Developer options > Wireless debugging > "Pair device with QR code" scans it and
     advertises `<service>._adb-tls-pairing._tcp` over mDNS.
  3. PC finds that service with `adb mdns services`, runs `adb pair host:port password`,
     then connects to the phone's `_adb-tls-connect._tcp` service.
The password and pairing codes live only in memory and are never logged (FR-DEV-012).
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import secrets
import string
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from phonepilot.errors import PhonePilotError

PAIRING_SERVICE = "_adb-tls-pairing._tcp"
CONNECT_SERVICE = "_adb-tls-connect._tcp"
HOST_PORT_RE = re.compile(r"^(?P<host>[0-9a-fA-F:.]+|\[[0-9a-fA-F:]+\]):(?P<port>\d{1,5})$")
CODE_RE = re.compile(r"^\d{6}$")

# runner(args, timeout) -> (returncode, combined output); args exclude the adb executable
Runner = Callable[[list[str], float], Awaitable[tuple[int, str]]]


@dataclass(frozen=True)
class MdnsService:
    instance: str
    service: str
    address: str  # host:port


@dataclass
class PairingResult:
    ok: bool
    message: str
    address: str = ""  # connect address (host:port) when connected


def qr_payload(service: str | None = None, password: str | None = None) -> tuple[str, str, str]:
    """Return (service_name, password, payload). Random values unless given (tests)."""
    alphabet = string.ascii_letters + string.digits
    service = service or "phonepilot-" + "".join(secrets.choice(alphabet) for _ in range(8))
    password = password or "".join(secrets.choice(alphabet) for _ in range(12))
    if not re.fullmatch(r"[A-Za-z0-9-]{1,40}", service) or not re.fullmatch(r"[A-Za-z0-9]{6,32}", password):
        raise ValueError("service/password contain characters the QR format does not allow")
    return service, password, f"WIFI:T:ADB;S:{service};P:{password};;"


def qr_png(payload: str, box_size: int = 8) -> bytes:
    import io

    import qrcode

    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=box_size, border=3)
    qr.add_data(payload)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def parse_mdns_services(output: str) -> list[MdnsService]:
    out = []
    for line in output.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[1].startswith("_adb") and ":" in parts[2]:
            out.append(MdnsService(parts[0], parts[1].rstrip("."), parts[2]))
    return out


def validate_host_port(value: str) -> str:
    value = value.strip()
    m = HOST_PORT_RE.match(value)
    if not m:
        raise PhonePilotError("DEVICE_NOT_FOUND", "IP:포트 형식이 아닙니다 (예: 192.168.0.12:37123)")
    host = m.group("host").strip("[]")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        raise PhonePilotError("DEVICE_NOT_FOUND", "IP 주소가 올바르지 않습니다") from None
    port = int(m.group("port"))
    if not 1 <= port <= 65535:
        raise PhonePilotError("DEVICE_NOT_FOUND", "포트 번호가 올바르지 않습니다")
    if not (ip.is_private or ip.is_link_local or ip.is_loopback):
        # FR-DEV-011: ADB over the internet / a public network is never intended
        raise PhonePilotError("POLICY_BLOCKED", "사설 네트워크(같은 Wi-Fi)의 주소만 허용합니다")
    return value


def validate_code(code: str) -> str:
    code = code.strip()
    if not CODE_RE.match(code):
        raise PhonePilotError("DEVICE_NOT_FOUND", "페어링 코드는 숫자 6자리입니다")
    return code


def _paired_ok(output: str) -> bool:
    return "successfully paired" in output.lower()


def _connected_ok(output: str) -> bool:
    low = output.lower()
    return ("connected to" in low or "already connected" in low) and "failed" not in low and "cannot" not in low


class WirelessADB:
    def __init__(self, runner: Runner, poll_interval: float = 1.0) -> None:
        self.run = runner
        self.poll_interval = poll_interval
        self._cancel = asyncio.Event()

    def cancel(self) -> None:
        self._cancel.set()

    async def services(self) -> list[MdnsService]:
        rc, out = await self.run(["mdns", "services"], 10)
        return parse_mdns_services(out) if rc == 0 else []

    async def _wait_service(self, predicate: Callable[[MdnsService], bool], timeout: float) -> MdnsService | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not self._cancel.is_set():
            for svc in await self.services():
                if predicate(svc):
                    return svc
            try:
                await asyncio.wait_for(self._cancel.wait(), timeout=self.poll_interval)
            except TimeoutError:
                pass
        return None

    async def connect(self, address: str) -> PairingResult:
        rc, out = await self.run(["connect", address], 15)
        ok = rc == 0 and _connected_ok(out)
        return PairingResult(ok, "연결됨" if ok else f"연결 실패: {out.strip()[:120]}", address if ok else "")

    async def _connect_after_pair(self, host: str, timeout: float) -> PairingResult:
        """After pairing the phone advertises its connect service; the adb server may also auto-connect."""
        svc = await self._wait_service(lambda s: s.service == CONNECT_SERVICE and s.address.rsplit(":", 1)[0] == host, timeout)
        if svc is None:
            return PairingResult(True, "페어링은 됐지만 연결 서비스를 찾지 못했습니다. 폰의 무선 디버깅 화면에 표시된 IP:포트로 연결하세요")
        return await self.connect(svc.address)

    async def pair_qr(self, service: str, password: str, timeout: float = 90, on_status: Callable[[str], None] | None = None) -> PairingResult:
        self._cancel.clear()
        status = on_status or (lambda _m: None)
        status("폰에서 QR 코드를 스캔하기를 기다리는 중…")
        svc = await self._wait_service(lambda s: s.service == PAIRING_SERVICE and s.instance == service, timeout)
        if svc is None:
            return PairingResult(False, "취소됨" if self._cancel.is_set() else "시간 초과: 폰이 QR을 스캔하지 않았거나 같은 Wi-Fi가 아닙니다")
        status("페어링 중…")
        rc, out = await self.run(["pair", svc.address, password], 30)
        if rc != 0 or not _paired_ok(out):
            return PairingResult(False, "페어링 실패: " + out.strip()[:120])
        status("연결 중…")
        return await self._connect_after_pair(svc.address.rsplit(":", 1)[0], 20)

    async def pair_code(self, host_port: str, code: str, on_status: Callable[[str], None] | None = None) -> PairingResult:
        self._cancel.clear()
        host_port = validate_host_port(host_port)
        code = validate_code(code)
        status = on_status or (lambda _m: None)
        status("페어링 중…")
        rc, out = await self.run(["pair", host_port, code], 30)
        if rc != 0 or not _paired_ok(out):
            return PairingResult(False, "페어링 실패: " + out.strip()[:120])
        status("연결 중…")
        return await self._connect_after_pair(host_port.rsplit(":", 1)[0].strip("[]"), 20)

    async def reconnect_known(self) -> list[str]:
        """FR-DEV-010: connect every paired phone the network advertises (ports change, mDNS finds them)."""
        connected = []
        for svc in await self.services():
            if svc.service == CONNECT_SERVICE:
                res = await self.connect(svc.address)
                if res.ok:
                    connected.append(svc.address)
        return connected


def is_wireless_serial(serial: str) -> bool:
    return CONNECT_SERVICE in serial or bool(HOST_PORT_RE.match(serial))
