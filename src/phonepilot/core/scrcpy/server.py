"""scrcpy-server lifecycle and sockets (PRD 6.1, appendix A)."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import random
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import adbutils

from phonepilot.core.scrcpy import protocol as p
from phonepilot.errors import PhonePilotError

log = logging.getLogger(__name__)
REMOTE_JAR = "/data/local/tmp/phonepilot-server.jar"


@dataclass
class StreamStart:
    device_name: str
    codec: str


async def read_preamble(reader: asyncio.StreamReader, *, expect_dummy: bool = True) -> StreamStart:
    if expect_dummy:
        await reader.readexactly(1)
    name = p.parse_device_name(await reader.readexactly(p.DEVICE_NAME_FIELD_LENGTH))
    codec = p.parse_codec_id(await reader.readexactly(4))
    return StreamStart(name, codec)


async def pump(
    reader: asyncio.StreamReader,
    on_session: Callable[[p.SessionMeta], None],
    on_packet: Callable[[p.PacketHeader, bytes], None],
) -> None:
    while True:
        try:
            header = p.parse_stream_header(await reader.readexactly(12))
        except asyncio.IncompleteReadError:
            return
        if isinstance(header, p.SessionMeta):
            on_session(header)
            continue
        payload = await reader.readexactly(header.size)
        on_packet(header, payload)


class ScrcpyServer:
    """Pushes and runs scrcpy-server through adb, then opens the video and control sockets."""

    def __init__(self, device: adbutils.AdbDevice, jar_path: Path, *, version: str = p.SCRCPY_SERVER_VERSION) -> None:
        self.device = device
        self.jar_path = jar_path
        self.version = version
        self.scid = random.getrandbits(31)
        self.port: int | None = None
        self._conn: adbutils.AdbConnection | None = None
        self._log_thread: threading.Thread | None = None
        self.server_log: list[str] = []

    def push(self) -> None:
        if not self.jar_path.exists():
            raise PhonePilotError("SERVER_PUSH_FAILED", f"{self.jar_path} missing; run scripts/fetch_vendor.py")
        local_hash = hashlib.sha256(self.jar_path.read_bytes()).hexdigest()
        try:
            remote = str(self.device.shell(f"sha256sum {REMOTE_JAR} 2>/dev/null", timeout=10)).split(" ")[0].strip()
        except Exception:  # noqa: BLE001
            remote = ""
        if remote == local_hash:
            return
        try:
            self.device.sync.push(str(self.jar_path), REMOTE_JAR)
        except Exception as e:  # noqa: BLE001
            raise PhonePilotError("SERVER_PUSH_FAILED", str(e)) from e

    def forward(self) -> int:
        for port in range(27183, 27300):
            try:
                # norebind: never take over a port another PhonePilot or scrcpy session is using
                self.device.forward(f"tcp:{port}", f"localabstract:{p.socket_name(self.scid)}", norebind=True)
                self.port = port
                return port
            except Exception:  # noqa: BLE001 - port in use, try the next
                continue
        raise PhonePilotError("SERVER_START_TIMEOUT", "no free forward port")

    def start(self, **server_kwargs: object) -> None:
        args = " ".join(p.server_args(self.scid, **server_kwargs))  # type: ignore[arg-type]
        cmd = f"CLASSPATH={REMOTE_JAR} app_process / com.genymobile.scrcpy.Server {args}"
        self._conn = self.device.shell(cmd, stream=True)
        self._log_thread = threading.Thread(target=self._read_log, name="pp-server-log", daemon=True)
        self._log_thread.start()

    def _read_log(self) -> None:
        conn = self._conn
        if conn is None:
            return
        buf = b""
        try:
            while True:
                chunk = conn.read(4096)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    text = line.decode("utf-8", errors="replace").strip()
                    if text:
                        self.server_log.append(text)
                        del self.server_log[:-200]
                        log.info("server: %s", text)
        except Exception:  # noqa: BLE001
            pass

    async def connect(self, attempts: int = 100) -> tuple[asyncio.StreamReader, asyncio.StreamWriter, asyncio.StreamReader, asyncio.StreamWriter, StreamStart]:
        assert self.port is not None
        video: tuple[asyncio.StreamReader, asyncio.StreamWriter] | None = None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + attempts * 0.1 + 10
        while video is None and loop.time() < deadline:
            # adb accepts the TCP connection even before the server listens, then closes it (EOF) -> retry.
            # Once the server has accepted, it IS the video socket: wait for the dummy byte instead of
            # reconnecting, otherwise the server takes the next connection as the control socket.
            try:
                r, w = await asyncio.open_connection("127.0.0.1", self.port)
            except OSError:
                await asyncio.sleep(0.1)
                continue
            try:
                await asyncio.wait_for(r.readexactly(1), timeout=max(deadline - loop.time(), 0.1))
                video = (r, w)
            except (OSError, asyncio.IncompleteReadError, TimeoutError):
                w.close()
                await asyncio.sleep(0.1)
        if video is None:
            raise PhonePilotError("SERVER_START_TIMEOUT", "scrcpy-server did not accept the connection: " + " | ".join(self.server_log[-3:]))
        cr, cw = await asyncio.open_connection("127.0.0.1", self.port)
        start = await read_preamble(video[0], expect_dummy=False)
        return video[0], video[1], cr, cw, start

    def stop(self) -> None:
        if self.port is not None:
            try:
                self.device.forward_remove(f"tcp:{self.port}")
            except Exception:  # noqa: BLE001
                pass
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001
                pass
            self._conn = None
