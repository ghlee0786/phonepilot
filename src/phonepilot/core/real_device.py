"""RealDevice: DeviceBackend over adb + scrcpy-server (PRD 6, 8.5)."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass

import adbutils

from phonepilot.core.adb import collect_profile
from phonepilot.core.decoder import Packet, VideoDecoder
from phonepilot.core.device import DeviceBackend
from phonepilot.core.framebus import FrameBus
from phonepilot.core.scrcpy import protocol as p
from phonepilot.core.scrcpy.server import ScrcpyServer, pump
from phonepilot.errors import PhonePilotError
from phonepilot.paths import vendor_dir

log = logging.getLogger(__name__)
FOREGROUND_RE = re.compile(r"(?:topResumedActivity|mResumedActivity|ResumedActivity)[^{]*\{[^ ]+ [^ ]+ ([\w.]+)/([\w.$]+)")


@dataclass
class VideoSettings:
    codec: str = "h264"
    max_size: int = 1920
    bit_rate_mbps: float = 8
    max_fps: int = 60
    decoder: str = "auto"
    encoder: str | None = None


class RealDevice(DeviceBackend):
    def __init__(self, adb_device: adbutils.AdbDevice, settings: VideoSettings | None = None) -> None:
        self.adb = adb_device
        self.settings = settings or VideoSettings()
        self.framebus = FrameBus()
        self.info = collect_profile(adb_device)
        self._frame = (0, 0)
        self._server: ScrcpyServer | None = None
        self._ctrl_writer: asyncio.StreamWriter | None = None
        self._ctrl_lock = asyncio.Lock()
        self._tasks: list[asyncio.Task[None]] = []
        self._decoder: VideoDecoder | None = None
        self._connected = False
        self.device_name = ""
        self.codec = ""
        self.on_clipboard: Callable[[str], None] | None = None
        self.on_disconnect: Callable[[], None] | None = None
        self.on_size: Callable[[int, int], None] | None = None

    # --------------------------------------------------------------- props
    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def frame_size(self) -> tuple[int, int]:
        return self._frame

    @property
    def display_size(self) -> tuple[int, int]:
        w, h = self.info.display_w, self.info.display_h
        fw, fh = self._frame
        if fw and fh and (fw > fh) != (w > h):
            return h, w  # rotated
        return w, h

    # ------------------------------------------------------------ lifecycle
    async def start(self, max_size: int | None = None) -> None:
        s = self.settings
        server = ScrcpyServer(self.adb, vendor_dir() / "scrcpy-server")
        await asyncio.to_thread(server.push)
        await asyncio.to_thread(server.forward)
        await asyncio.to_thread(
            server.start,
            video_codec=s.codec,
            max_size=max_size or s.max_size,
            bit_rate=int(s.bit_rate_mbps * 1_000_000),
            max_fps=s.max_fps,
            video_encoder=s.encoder,
        )
        vr, vw, cr, cw, start = await server.connect()
        self._server = server
        self.device_name = start.device_name
        self.codec = start.codec
        self._ctrl_writer = cw
        self._decoder = VideoDecoder(start.codec, self.framebus, s.decoder, on_size=self._on_decoded_size)
        self._decoder.start()
        self._connected = True
        self._tasks = [
            asyncio.create_task(self._video_loop(vr, vw)),
            asyncio.create_task(self._control_reader(cr)),
        ]

    def _on_decoded_size(self, w: int, h: int) -> None:
        if self._frame == (0, 0):
            self._frame = (w, h)

    def _on_session(self, meta: p.SessionMeta) -> None:
        self._frame = (meta.width, meta.height)
        if self.on_size:
            self.on_size(meta.width, meta.height)

    def _on_packet(self, header: p.PacketHeader, payload: bytes) -> None:
        if self._decoder is not None:
            self._decoder.push(Packet(payload, header.pts, header.key_frame, header.config))

    async def _video_loop(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await pump(reader, self._on_session, self._on_packet)
        except Exception as e:  # noqa: BLE001
            log.warning("video stream ended: %s", e)
        finally:
            writer.close()
            self._connected = False
            if self.on_disconnect:
                self.on_disconnect()

    async def _control_reader(self, reader: asyncio.StreamReader) -> None:
        parser = p.DeviceMessageParser()
        try:
            while True:
                data = await reader.read(4096)
                if not data:
                    return
                for msg in parser.feed(data):
                    if isinstance(msg, p.DeviceClipboard) and self.on_clipboard:
                        self.on_clipboard(msg.text)
        except Exception:  # noqa: BLE001
            return

    async def close(self) -> None:
        self._connected = False
        for t in self._tasks:
            t.cancel()
        if self._ctrl_writer is not None:
            self._ctrl_writer.close()
        if self._decoder is not None:
            await asyncio.to_thread(self._decoder.stop)
        if self._server is not None:
            await asyncio.to_thread(self._server.stop)

    # -------------------------------------------------------------- backend
    async def send_control(self, msg: bytes) -> None:
        if self._ctrl_writer is None or not self._connected:
            raise PhonePilotError("CONTROL_UNAVAILABLE", "control socket not connected")
        async with self._ctrl_lock:
            self._ctrl_writer.write(msg)
            await self._ctrl_writer.drain()

    async def shell(self, cmd: str, timeout: float = 10.0) -> str:
        try:
            return str(await asyncio.to_thread(self.adb.shell, cmd, timeout=timeout))
        except Exception as e:  # noqa: BLE001
            raise PhonePilotError("DEVICE_OFFLINE", str(e)) from e

    async def dump_ui(self) -> str:
        out = await self.shell("uiautomator dump /sdcard/pp_ui.xml >/dev/null 2>&1; cat /sdcard/pp_ui.xml; rm -f /sdcard/pp_ui.xml", timeout=15)
        if "<hierarchy" not in out:
            raise PhonePilotError("UI_TREE_UNAVAILABLE", out[:200])
        return out

    async def foreground(self) -> tuple[str, str]:
        out = await self.shell("dumpsys activity activities | grep -E 'topResumedActivity|mResumedActivity|ResumedActivity' | head -n 3", timeout=8)
        m = FOREGROUND_RE.search(out)
        if not m:
            return "", ""
        return m.group(1), m.group(2)

    async def push(self, local: str, remote: str) -> None:
        await asyncio.to_thread(self.adb.sync.push, local, remote)
