"""Device backend interface. `RealDevice` (scrcpy + adb) and `FakeDevice` (tests) implement it."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from phonepilot.core.framebus import FrameBus


@dataclass
class DeviceInfo:
    serial_hash: str = ""
    serial_tail: str = ""
    manufacturer: str = ""
    model: str = ""
    sdk: int = 0
    release: str = ""
    display_w: int = 0
    display_h: int = 0
    density: int = 0
    ime: str = ""
    nav_mode: str = ""
    encoders: list[str] = field(default_factory=list)

    def public(self) -> dict[str, object]:
        """What MCP may see (PRD FR-DEV-022): no full serial, IMEI, phone number or accounts."""
        return {
            "serial_tail": self.serial_tail,
            "manufacturer": self.manufacturer,
            "model": self.model,
            "android": self.release,
            "sdk": self.sdk,
            "display": {"width": self.display_w, "height": self.display_h, "density": self.density},
            "ime": self.ime,
            "navigation_mode": self.nav_mode,
        }


class DeviceBackend(ABC):
    """Everything above the transport talks to the device through this interface."""

    framebus: FrameBus
    info: DeviceInfo

    @property
    @abstractmethod
    def display_size(self) -> tuple[int, int]:
        """Real display size in pixels (UI tree coordinate space)."""

    @property
    @abstractmethod
    def frame_size(self) -> tuple[int, int]:
        """Current video frame size (control message coordinate space)."""

    @abstractmethod
    async def send_control(self, msg: bytes) -> None:
        """Write one serialized scrcpy control message."""

    @abstractmethod
    async def dump_ui(self) -> str:
        """UIAutomator hierarchy XML."""

    @abstractmethod
    async def shell(self, cmd: str, timeout: float = 10.0) -> str: ...

    @abstractmethod
    async def foreground(self) -> tuple[str, str]:
        """(package, activity) of the resumed activity."""

    @abstractmethod
    async def push(self, local: str, remote: str) -> None: ...

    async def close(self) -> None:  # pragma: no cover - default no-op
        return None

    @property
    def connected(self) -> bool:
        return True

    def display_to_frame(self, x: float, y: float) -> tuple[int, int]:
        dw, dh = self.display_size
        fw, fh = self.frame_size
        if not dw or not dh:
            return int(x), int(y)
        return int(round(x * fw / dw)), int(round(y * fh / dh))

    def frame_to_display(self, x: float, y: float) -> tuple[int, int]:
        dw, dh = self.display_size
        fw, fh = self.frame_size
        if not fw or not fh:
            return int(x), int(y)
        return int(round(x * dw / fw)), int(round(y * dh / fh))
