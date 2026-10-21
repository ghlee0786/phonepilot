"""scrcpy v4.1 wire protocol (byte-identical to v5.0.1 for stream and control; v5.0.1 aborts on some Samsung Android 15 devices): control messages (client -> server), device messages
(server -> client) and video stream headers. PRD appendix A.

Every integer is big-endian. Keep all version-dependent byte layouts in this file.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import IntEnum

SCRCPY_SERVER_VERSION = "4.1"

DEVICE_NAME_FIELD_LENGTH = 64
INJECT_TEXT_MAX_BYTES = 300
CLIPBOARD_TEXT_MAX_BYTES = (1 << 18) - 14

# Stream packet flags (Streamer.java)
PACKET_FLAG_SESSION = 1 << 63
PACKET_FLAG_CONFIG = 1 << 62
PACKET_FLAG_KEY_FRAME = 1 << 61
PTS_MASK = PACKET_FLAG_KEY_FRAME - 1

CODEC_IDS = {
    0x68323634: "h264",
    0x68323635: "h265",
    0x61763031: "av1",
    0x6F707573: "opus",
    0x00616163: "aac",
    0x666C6163: "flac",
    0x00726177: "raw",
}

# Special pointer ids understood by the server
POINTER_ID_MOUSE = -1
POINTER_ID_GENERIC_FINGER = -2
POINTER_ID_VIRTUAL_FINGER = -3


class ControlType(IntEnum):
    INJECT_KEYCODE = 0
    INJECT_TEXT = 1
    INJECT_TOUCH_EVENT = 2
    INJECT_SCROLL_EVENT = 3
    BACK_OR_SCREEN_ON = 4
    EXPAND_NOTIFICATION_PANEL = 5
    EXPAND_SETTINGS_PANEL = 6
    COLLAPSE_PANELS = 7
    GET_CLIPBOARD = 8
    SET_CLIPBOARD = 9
    SET_DISPLAY_POWER = 10
    ROTATE_DEVICE = 11
    UHID_CREATE = 12
    UHID_INPUT = 13
    UHID_DESTROY = 14
    OPEN_HARD_KEYBOARD_SETTINGS = 15
    START_APP = 16
    RESET_VIDEO = 17


class MotionAction(IntEnum):
    DOWN = 0
    UP = 1
    MOVE = 2
    CANCEL = 3


class KeyAction(IntEnum):
    DOWN = 0
    UP = 1


class CopyKey(IntEnum):
    NONE = 0
    COPY = 1
    CUT = 2


class DeviceMessageType(IntEnum):
    CLIPBOARD = 0
    ACK_CLIPBOARD = 1
    UHID_OUTPUT = 2


class Keycode(IntEnum):
    HOME = 3
    BACK = 4
    DPAD_UP = 19
    DPAD_DOWN = 20
    DPAD_LEFT = 21
    DPAD_RIGHT = 22
    DPAD_CENTER = 23
    VOLUME_UP = 24
    VOLUME_DOWN = 25
    POWER = 26
    TAB = 61
    SPACE = 62
    ENTER = 66
    DEL = 67
    MENU = 82
    SEARCH = 84
    PAGE_UP = 92
    PAGE_DOWN = 93
    ESCAPE = 111
    FORWARD_DEL = 112
    MOVE_HOME = 122
    MOVE_END = 123
    VOLUME_MUTE = 164
    APP_SWITCH = 187
    SLEEP = 223
    WAKEUP = 224
    CUT = 277
    COPY = 278
    PASTE = 279


META_SHIFT_ON = 0x1
META_ALT_ON = 0x2
META_CTRL_ON = 0x1000
META_META_ON = 0x10000

BUTTON_PRIMARY = 1


def _u16_fixed(value: float) -> int:
    """Float in [0, 1] -> u16 fixed point (1.0 -> 0xffff)."""
    value = min(max(value, 0.0), 1.0)
    return 0xFFFF if value >= 1.0 else int(value * 65536)


def _i16_fixed(value: float) -> int:
    """Float in [-1, 1] -> i16 fixed point."""
    value = min(max(value, -1.0), 1.0)
    if value >= 1.0:
        return 0x7FFF
    return int(value * 32768)


def _position(x: int, y: int, screen_w: int, screen_h: int) -> bytes:
    return struct.pack(">iiHH", int(x), int(y), int(screen_w), int(screen_h))


def inject_keycode(action: KeyAction, keycode: int, repeat: int = 0, meta_state: int = 0) -> bytes:
    return struct.pack(">BBiii", ControlType.INJECT_KEYCODE, int(action), int(keycode), int(repeat), int(meta_state))


def inject_text(text: str) -> bytes:
    raw = text.encode("utf-8")
    if len(raw) > INJECT_TEXT_MAX_BYTES:
        raise ValueError(f"inject_text payload too long ({len(raw)} > {INJECT_TEXT_MAX_BYTES} bytes)")
    return struct.pack(">BI", ControlType.INJECT_TEXT, len(raw)) + raw


def inject_text_bytes(raw: bytes | bytearray) -> bytes:
    """Same as inject_text but from bytes, so secret values never become a Python str."""
    if len(raw) > INJECT_TEXT_MAX_BYTES:
        raise ValueError("inject_text payload too long")
    return struct.pack(">BI", ControlType.INJECT_TEXT, len(raw)) + bytes(raw)


def inject_touch(
    action: MotionAction,
    pointer_id: int,
    x: int,
    y: int,
    screen_w: int,
    screen_h: int,
    pressure: float = 1.0,
    action_button: int = 0,
    buttons: int = 0,
) -> bytes:
    return (
        struct.pack(">BBq", ControlType.INJECT_TOUCH_EVENT, int(action), int(pointer_id))
        + _position(x, y, screen_w, screen_h)
        + struct.pack(">Hii", _u16_fixed(pressure), int(action_button), int(buttons))
    )


def inject_scroll(x: int, y: int, screen_w: int, screen_h: int, hscroll: float, vscroll: float, buttons: int = 0) -> bytes:
    # The server multiplies by 16 after decoding, so the wire range [-1, 1] maps to [-16, 16].
    return (
        struct.pack(">B", ControlType.INJECT_SCROLL_EVENT)
        + _position(x, y, screen_w, screen_h)
        + struct.pack(">hhi", _i16_fixed(hscroll / 16), _i16_fixed(vscroll / 16), int(buttons))
    )


def back_or_screen_on(action: KeyAction) -> bytes:
    return struct.pack(">BB", ControlType.BACK_OR_SCREEN_ON, int(action))


def simple(kind: ControlType) -> bytes:
    if kind not in {
        ControlType.EXPAND_NOTIFICATION_PANEL,
        ControlType.EXPAND_SETTINGS_PANEL,
        ControlType.COLLAPSE_PANELS,
        ControlType.ROTATE_DEVICE,
        ControlType.OPEN_HARD_KEYBOARD_SETTINGS,
        ControlType.RESET_VIDEO,
    }:
        raise ValueError(f"{kind!r} has a payload")
    return struct.pack(">B", kind)


def get_clipboard(copy_key: CopyKey = CopyKey.NONE) -> bytes:
    return struct.pack(">BB", ControlType.GET_CLIPBOARD, int(copy_key))


def set_clipboard(sequence: int, text: str, paste: bool) -> bytes:
    raw = text.encode("utf-8")
    if len(raw) > CLIPBOARD_TEXT_MAX_BYTES:
        raise ValueError("clipboard text too long")
    return struct.pack(">BqBI", ControlType.SET_CLIPBOARD, sequence, 1 if paste else 0, len(raw)) + raw


def set_display_power(on: bool) -> bytes:
    return struct.pack(">BB", ControlType.SET_DISPLAY_POWER, 1 if on else 0)


def start_app(name: str) -> bytes:
    raw = name.encode("utf-8")
    if len(raw) > 255:
        raise ValueError("app name too long")
    return struct.pack(">BB", ControlType.START_APP, len(raw)) + raw


# ---------------------------------------------------------------- stream side


@dataclass(frozen=True)
class SessionMeta:
    width: int
    height: int
    client_resize: bool


@dataclass(frozen=True)
class PacketHeader:
    config: bool
    key_frame: bool
    pts: int | None
    size: int


def parse_device_name(raw: bytes) -> str:
    if len(raw) != DEVICE_NAME_FIELD_LENGTH:
        raise ValueError("device meta must be 64 bytes")
    return raw.split(b"\x00", 1)[0].decode("utf-8", errors="replace")


def parse_codec_id(raw: bytes) -> str:
    (codec_id,) = struct.unpack(">I", raw)
    if codec_id == 0:
        raise ConnectionError("stream disabled by device")
    if codec_id == 1:
        raise ConnectionError("device reported a stream configuration error")
    return CODEC_IDS.get(codec_id, f"unknown-{codec_id:#x}")


def parse_stream_header(raw: bytes) -> SessionMeta | PacketHeader:
    """Parse a 12-byte header that is either a session meta or a frame meta."""
    if len(raw) != 12:
        raise ValueError("stream header must be 12 bytes")
    (first,) = struct.unpack(">Q", raw[:8])
    if first & PACKET_FLAG_SESSION:
        flags, width, height = struct.unpack(">Iii", raw)
        return SessionMeta(width=width, height=height, client_resize=bool(flags & 1))
    (size,) = struct.unpack(">I", raw[8:])
    if first & PACKET_FLAG_CONFIG:
        return PacketHeader(config=True, key_frame=False, pts=None, size=size)
    return PacketHeader(config=False, key_frame=bool(first & PACKET_FLAG_KEY_FRAME), pts=first & PTS_MASK, size=size)


def build_session_meta(width: int, height: int, client_resize: bool = False) -> bytes:
    flags = (PACKET_FLAG_SESSION >> 32) | (1 if client_resize else 0)
    return struct.pack(">Iii", flags, width, height)


def build_packet_header(size: int, pts: int | None, config: bool = False, key_frame: bool = False) -> bytes:
    if config:
        value = PACKET_FLAG_CONFIG
    else:
        value = (pts or 0) & PTS_MASK
        if key_frame:
            value |= PACKET_FLAG_KEY_FRAME
    return struct.pack(">QI", value, size)


@dataclass(frozen=True)
class DeviceClipboard:
    text: str


@dataclass(frozen=True)
class DeviceAckClipboard:
    sequence: int


@dataclass(frozen=True)
class DeviceUhidOutput:
    id: int
    data: bytes


DeviceMessage = DeviceClipboard | DeviceAckClipboard | DeviceUhidOutput


class DeviceMessageParser:
    """Incremental parser for the control socket's server -> client direction."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> list[DeviceMessage]:
        self._buf.extend(data)
        out: list[DeviceMessage] = []
        while True:
            msg, consumed = self._try_parse()
            if msg is None:
                break
            del self._buf[:consumed]
            out.append(msg)
        return out

    def _try_parse(self) -> tuple[DeviceMessage | None, int]:
        buf = self._buf
        if not buf:
            return None, 0
        kind = buf[0]
        if kind == DeviceMessageType.CLIPBOARD:
            if len(buf) < 5:
                return None, 0
            (length,) = struct.unpack(">I", buf[1:5])
            if len(buf) < 5 + length:
                return None, 0
            return DeviceClipboard(bytes(buf[5 : 5 + length]).decode("utf-8", errors="replace")), 5 + length
        if kind == DeviceMessageType.ACK_CLIPBOARD:
            if len(buf) < 9:
                return None, 0
            (seq,) = struct.unpack(">q", buf[1:9])
            return DeviceAckClipboard(seq), 9
        if kind == DeviceMessageType.UHID_OUTPUT:
            if len(buf) < 5:
                return None, 0
            uid, size = struct.unpack(">HH", buf[1:5])
            if len(buf) < 5 + size:
                return None, 0
            return DeviceUhidOutput(uid, bytes(buf[5 : 5 + size])), 5 + size
        raise ValueError(f"unknown device message type {kind}")


def decode_control(msg: bytes) -> dict[str, object]:
    """Inverse of the serializers above, mirroring the server's ControlMessageReader (tests, FakeDevice)."""
    kind = ControlType(msg[0])
    body = msg[1:]
    if kind == ControlType.INJECT_KEYCODE:
        action, keycode, repeat, meta = struct.unpack(">Biii", body)
        return {"type": kind, "action": KeyAction(action), "keycode": keycode, "repeat": repeat, "meta": meta}
    if kind == ControlType.INJECT_TEXT:
        (length,) = struct.unpack(">I", body[:4])
        return {"type": kind, "text": body[4 : 4 + length].decode("utf-8")}
    if kind == ControlType.INJECT_TOUCH_EVENT:
        action, pid, x, y, w, h, pressure, ab, buttons = struct.unpack(">BqiiHHHii", body)
        return {
            "type": kind, "action": MotionAction(action), "pointer_id": pid, "x": x, "y": y, "screen_w": w,
            "screen_h": h, "pressure": pressure / 0xFFFF if pressure < 0xFFFF else 1.0, "action_button": ab, "buttons": buttons,
        }
    if kind == ControlType.INJECT_SCROLL_EVENT:
        x, y, w, h, hs, vs, buttons = struct.unpack(">iiHHhhi", body)
        return {"type": kind, "x": x, "y": y, "screen_w": w, "screen_h": h, "hscroll": hs / 32768 * 16, "vscroll": vs / 32768 * 16}
    if kind == ControlType.BACK_OR_SCREEN_ON:
        return {"type": kind, "action": KeyAction(body[0])}
    if kind == ControlType.GET_CLIPBOARD:
        return {"type": kind, "copy_key": CopyKey(body[0])}
    if kind == ControlType.SET_CLIPBOARD:
        seq, paste, length = struct.unpack(">qBI", body[:13])
        return {"type": kind, "sequence": seq, "paste": bool(paste), "text": body[13 : 13 + length].decode("utf-8")}
    if kind == ControlType.SET_DISPLAY_POWER:
        return {"type": kind, "on": bool(body[0])}
    if kind == ControlType.START_APP:
        return {"type": kind, "name": body[1 : 1 + body[0]].decode("utf-8")}
    return {"type": kind}


def server_args(
    scid: int,
    *,
    video_codec: str = "h264",
    max_size: int = 1920,
    bit_rate: int = 8_000_000,
    max_fps: int = 60,
    audio: bool = False,
    control: bool = True,
    video_encoder: str | None = None,
    display_id: int = 0,
    stay_awake: bool = False,
    cleanup: bool = True,
) -> list[str]:
    """Arguments after `app_process / com.genymobile.scrcpy.Server`.

    Only values that differ from the server defaults (Options.java) are passed: some devices
    (Samsung, Android 15) abort app_process with "stack corruption detected" when it receives more
    than ~13 arguments. Required non-defaults: tunnel_forward, audio=false, log_level.
    """
    args = [SCRCPY_SERVER_VERSION, f"scid={scid:08x}", "log_level=info", "tunnel_forward=true"]
    if not audio:
        args.append("audio=false")
    if not control:
        args.append("control=false")
    if video_codec != "h264":
        args.append(f"video_codec={video_codec}")
    if max_size:
        args.append(f"max_size={max_size}")
    if bit_rate != 8_000_000:
        args.append(f"video_bit_rate={bit_rate}")
    if max_fps:
        args.append(f"max_fps={max_fps}")
    if display_id:
        args.append(f"display_id={display_id}")
    if stay_awake:
        args.append("stay_awake=true")
    if not cleanup:
        args.append("cleanup=false")
    if video_encoder:
        args.append(f"video_encoder={video_encoder}")
    # defaults relied upon: video, control, send_device/stream/frame_meta, send_dummy_byte,
    # clipboard_autosync, downsize_on_error = true; power_off_on_close = false
    return args


def socket_name(scid: int) -> str:
    return f"scrcpy_{scid:08x}"
