"""Video decoding thread: scrcpy packets -> RGB frames on the FrameBus (PRD 5.4, FR-MIR-091)."""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass

import av

from phonepilot.core.framebus import FrameBus

log = logging.getLogger(__name__)

CODEC_NAMES = {"h264": "h264", "h265": "hevc", "av1": "av1"}


@dataclass
class Packet:
    data: bytes
    pts: int | None
    key_frame: bool
    config: bool


class VideoDecoder:
    """Decodes on its own thread. Only the newest frame matters, so a full queue drops non-key packets."""

    def __init__(self, codec: str, framebus: FrameBus, hw: str = "auto", on_size: Callable[[int, int], None] | None = None) -> None:
        self.codec = codec
        self.framebus = framebus
        self.hw = hw
        self.on_size = on_size
        self.queue: queue.Queue[Packet | None] = queue.Queue(maxsize=8)
        self.thread = threading.Thread(target=self._loop, name="pp-decoder", daemon=True)
        self.ctx: av.codec.CodecContext | None = None
        self.hw_active = False
        self.decoded = 0
        self.dropped = 0
        self.errors = 0
        self._config: bytes = b""
        self._size: tuple[int, int] | None = None
        self._waiting_key = False

    def _open(self) -> None:
        name = CODEC_NAMES.get(self.codec, self.codec)
        if self.hw in ("auto", "hw"):
            try:
                hwaccel = av.codec.hwaccel.HWAccel(device_type="d3d11va", allow_software_fallback=True)
                self.ctx = av.CodecContext.create(name, "r", hwaccel=hwaccel)
                self._low_latency()
                self.hw_active = True
                return
            except Exception as e:  # noqa: BLE001 - fall back to software decoding
                log.info("hardware decoding unavailable (%s), using software", e)
        self.ctx = av.CodecContext.create(name, "r")
        self._low_latency()
        self.hw_active = False

    def _low_latency(self) -> None:
        """Slice threading + LOW_DELAY: frame threading would hold back several frames (K1 latency)."""
        assert self.ctx is not None
        self.ctx.thread_type = "SLICE"
        self.ctx.flags |= av.codec.context.Flags.low_delay

    def start(self) -> None:
        self._open()
        self.thread.start()

    def stop(self) -> None:
        try:
            self.queue.put_nowait(None)
        except queue.Full:
            try:
                self.queue.get_nowait()
            except queue.Empty:
                pass
            self.queue.put_nowait(None)
        self.thread.join(timeout=2)

    def push(self, pkt: Packet) -> None:
        if pkt.config:
            self._config = pkt.data  # merged with the next frame (FR-MIR-007)
            return
        if self._config:
            pkt = Packet(self._config + pkt.data, pkt.pts, pkt.key_frame, False)
            self._config = b""
        try:
            self.queue.put_nowait(pkt)
        except queue.Full:
            # latest-frame-wins: drop everything until the next key frame
            self.dropped += self.queue.qsize()
            while True:
                try:
                    self.queue.get_nowait()
                except queue.Empty:
                    break
            self._waiting_key = not pkt.key_frame
            if pkt.key_frame:
                self.queue.put_nowait(pkt)

    def _loop(self) -> None:
        while True:
            pkt = self.queue.get()
            if pkt is None:
                return
            if self._waiting_key and not pkt.key_frame:
                self.dropped += 1
                continue
            self._waiting_key = False
            try:
                self._decode(pkt)
            except Exception as e:  # noqa: BLE001
                self.errors += 1
                log.warning("decode error: %s", e)
                if self.hw_active and self.errors > 3:
                    self.hw = "sw"
                    self._open()
                    self._waiting_key = True

    def _decode(self, pkt: Packet) -> None:
        assert self.ctx is not None
        packet = av.Packet(pkt.data)
        if pkt.pts is not None:
            packet.pts = pkt.pts
        for frame in self.ctx.decode(packet):
            rgb = frame.to_ndarray(format="rgb24")
            h, w = rgb.shape[:2]
            if self._size != (w, h):
                self._size = (w, h)
                if self.on_size:
                    self.on_size(w, h)
            self.framebus.publish(rgb)
            self.decoded += 1
