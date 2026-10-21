"""TC-MIR-01/02 and protocol round-trips against the scrcpy v5 server layout (PRD appendix A)."""

import struct

import pytest

from phonepilot.core.scrcpy import protocol as p


def test_touch_event_layout_tc_mir_01():
    msg = p.inject_touch(p.MotionAction.DOWN, 7, 100, 200, 1080, 2400, pressure=1.0, buttons=p.BUTTON_PRIMARY)
    assert len(msg) == 32
    assert msg[0] == p.ControlType.INJECT_TOUCH_EVENT
    assert msg[1] == p.MotionAction.DOWN
    assert struct.unpack(">q", msg[2:10])[0] == 7
    assert struct.unpack(">iiHH", msg[10:22]) == (100, 200, 1080, 2400)
    assert struct.unpack(">H", msg[22:24])[0] == 0xFFFF
    assert struct.unpack(">ii", msg[24:32]) == (0, p.BUTTON_PRIMARY)


def test_negative_pointer_ids_roundtrip():
    msg = p.inject_touch(p.MotionAction.MOVE, p.POINTER_ID_GENERIC_FINGER, 1, 2, 3, 4)
    assert p.decode_control(msg)["pointer_id"] == -2


def test_keycode_layout():
    msg = p.inject_keycode(p.KeyAction.UP, p.Keycode.HOME, 1, p.META_CTRL_ON)
    assert len(msg) == 14
    d = p.decode_control(msg)
    assert d["action"] == p.KeyAction.UP and d["keycode"] == 3 and d["repeat"] == 1 and d["meta"] == p.META_CTRL_ON


def test_inject_text_limit_and_utf8():
    assert p.decode_control(p.inject_text("hello"))["text"] == "hello"
    with pytest.raises(ValueError):
        p.inject_text("x" * 301)


def test_scroll_fixed_point_scaling():
    msg = p.inject_scroll(10, 20, 100, 200, 0.0, -1.0)
    assert len(msg) == 21
    d = p.decode_control(msg)
    assert d["vscroll"] == pytest.approx(-1.0, abs=1e-3)


def test_set_clipboard_layout():
    msg = p.set_clipboard(42, "안녕", paste=True)
    assert msg[0] == p.ControlType.SET_CLIPBOARD
    d = p.decode_control(msg)
    assert d == {"type": p.ControlType.SET_CLIPBOARD, "sequence": 42, "paste": True, "text": "안녕"}


def test_simple_messages_have_no_payload():
    assert p.simple(p.ControlType.EXPAND_NOTIFICATION_PANEL) == b"\x05"
    assert p.set_display_power(False) == b"\x0a\x00"
    with pytest.raises(ValueError):
        p.simple(p.ControlType.INJECT_TEXT)


def test_stream_headers_tc_mir_02():
    s = p.parse_stream_header(p.build_session_meta(1080, 2400))
    assert isinstance(s, p.SessionMeta) and (s.width, s.height) == (1080, 2400)
    cfg = p.parse_stream_header(p.build_packet_header(30, None, config=True))
    assert isinstance(cfg, p.PacketHeader) and cfg.config and cfg.pts is None and cfg.size == 30
    key = p.parse_stream_header(p.build_packet_header(1234, 987654321, key_frame=True))
    assert key.key_frame and key.pts == 987654321 and not key.config
    plain = p.parse_stream_header(p.build_packet_header(5, 1))
    assert not plain.key_frame and plain.pts == 1


def test_codec_and_device_meta():
    assert p.parse_codec_id(struct.pack(">I", 0x68323634)) == "h264"
    with pytest.raises(ConnectionError):
        p.parse_codec_id(b"\x00\x00\x00\x01")
    raw = "Galaxy S24".encode() + b"\x00" * (64 - 10)
    assert p.parse_device_name(raw) == "Galaxy S24"


def test_device_message_parser_incremental():
    parser = p.DeviceMessageParser()
    payload = "복사됨".encode()
    msg = bytes([0]) + struct.pack(">I", len(payload)) + payload + bytes([1]) + struct.pack(">q", 9)
    out = parser.feed(msg[:3])
    assert out == []
    out = parser.feed(msg[3:])
    assert out == [p.DeviceClipboard("복사됨"), p.DeviceAckClipboard(9)]


def test_server_args_contains_version_and_scid():
    args = p.server_args(0x1A2B3C4D)
    assert args[0] == p.SCRCPY_SERVER_VERSION
    assert "scid=1a2b3c4d" in args and "tunnel_forward=true" in args and "audio=false" in args
    # keep the argument count low: some Samsung Android 15 devices abort app_process above ~13 args
    assert len(args) <= 10
    full = p.server_args(1, video_codec="h265", max_size=1280, bit_rate=4_000_000, max_fps=30, display_id=2, stay_awake=True, cleanup=False, video_encoder="c2.x")
    assert len(full) <= 13
    assert not any(a.startswith(("send_", "video=", "clipboard_autosync", "power_off")) for a in args)
    assert p.socket_name(0x1A2B3C4D) == "scrcpy_1a2b3c4d"
