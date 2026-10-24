"""TC-MIR-03/04: scrcpy v5 video socket -> reader -> decoder -> FrameBus, including a rotation."""

import asyncio
import struct
import time
from fractions import Fraction

import av
import numpy as np
import pytest

from phonepilot.core.decoder import QUEUE_PACKETS, Packet, VideoDecoder
from phonepilot.core.framebus import FrameBus
from phonepilot.core.scrcpy import protocol as p
from phonepilot.core.scrcpy.server import pump, read_preamble


def encode(width: int, height: int, n: int, shade0: int = 0) -> list[tuple[bytes, bool]]:
    enc = av.CodecContext.create("libx264", "w")
    enc.width, enc.height, enc.pix_fmt = width, height, "yuv420p"
    enc.time_base = Fraction(1, 60)
    enc.options = {"tune": "zerolatency", "preset": "ultrafast", "g": "30"}
    out = []
    for i in range(n):
        arr = np.full((height, width, 3), (shade0 + i * 7) % 255, np.uint8)
        arr[: height // 4, : width // 4] = 255
        f = av.VideoFrame.from_ndarray(arr, format="rgb24")
        f.pts = i
        out += [(bytes(pk), bool(pk.is_keyframe)) for pk in enc.encode(f)]
    out += [(bytes(pk), bool(pk.is_keyframe)) for pk in enc.encode(None)]
    return out


def socket_bytes() -> bytes:
    data = b"\x00" + "FakePhone".encode().ljust(64, b"\x00") + struct.pack(">I", 0x68323634)
    data += p.build_session_meta(320, 640)
    for i, (pkt, key) in enumerate(encode(320, 640, 30)):
        data += p.build_packet_header(len(pkt), i * 16666, key_frame=key) + pkt
    data += p.build_session_meta(640, 320)  # device rotated
    for i, (pkt, key) in enumerate(encode(640, 320, 30, 100)):
        data += p.build_packet_header(len(pkt), 600000 + i * 16666, key_frame=key) + pkt
    return data


async def test_stream_decode_and_rotation_tc_mir_03_04():
    reader = asyncio.StreamReader()
    reader.feed_data(socket_bytes())
    reader.feed_eof()
    start = await read_preamble(reader)
    assert (start.device_name, start.codec) == ("FakePhone", "h264")
    bus = FrameBus()
    sizes: list[tuple[int, int]] = []
    sessions: list[tuple[int, int]] = []
    dec = VideoDecoder("h264", bus, hw="sw", on_size=lambda w, h: sizes.append((w, h)))
    dec.start()
    seen: list[tuple[int, int]] = []
    bus.subscribe(lambda f: seen.append((f.width, f.height)))
    try:
        def on_packet(h, payload):
            # feed slowly enough that the bounded queue never drops (dropping is tested separately)
            while dec.queue.qsize() >= 6:
                time.sleep(0.002)
            dec.push(Packet(payload, h.pts, h.key_frame, h.config))

        await pump(reader, lambda m: sessions.append((m.width, m.height)), on_packet)
        for _ in range(200):
            if dec.decoded >= 60 or dec.queue.empty() and dec.decoded > 0 and len(seen) >= 55:
                break
            await asyncio.sleep(0.02)
    finally:
        dec.stop()
    assert sessions == [(320, 640), (640, 320)]
    assert sizes == [(320, 640), (640, 320)]
    assert dec.decoded >= 55 and dec.errors == 0
    assert seen[0] == (320, 640) and seen[-1] == (640, 320)
    assert bus.latest().rgb.shape == (320, 640, 3)


def test_decoder_drops_to_next_keyframe_when_full():
    bus = FrameBus()
    dec = VideoDecoder("h264", bus, hw="sw")  # not started: queue fills up
    pkts = encode(160, 320, QUEUE_PACKETS + 20)
    for i, (pkt, key) in enumerate(pkts):
        dec.push(Packet(pkt, i, key, False))
    assert dec.dropped > 0
    assert dec.queue.qsize() <= QUEUE_PACKETS


def test_decoder_converts_only_the_newest_frame_of_a_burst():
    """A fast scroll: decode everything (reference chain) but convert to RGB only when nothing newer is waiting."""
    bus = FrameBus()
    dec = VideoDecoder("h264", bus, hw="sw")
    dec._open()  # noqa: SLF001
    pkts = encode(160, 320, 30)
    for i, (pkt, key) in enumerate(pkts):
        dec.push(Packet(pkt, i, key, False))
    while not dec.queue.empty():
        dec._decode(dec.queue.get())  # noqa: SLF001
    assert dec.waiting_key_since is None and dec.dropped == 0
    assert dec.skipped > 0 and bus.latest() is not None
    assert dec.decoded == len(pkts) or dec.decoded >= len(pkts) - 2  # codec delay may hold the last frame or two


@pytest.mark.parametrize("hw", ["auto"])
def test_hw_decoder_open(hw):
    dec = VideoDecoder("h264", FrameBus(), hw=hw)
    dec._open()
    assert dec.ctx is not None
