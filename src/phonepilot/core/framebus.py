"""Latest-frame holder shared by renderer, observer and recorder (PRD 5.1 'latest frame wins')."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class Frame:
    width: int
    height: int
    rgb: np.ndarray  # (h, w, 3) uint8
    seq: int
    ts: float  # time.monotonic() when published


class FrameBus:
    def __init__(self) -> None:
        self._lock = threading.Condition()
        self._frame: Frame | None = None
        self._seq = 0
        self._subscribers: list[Callable[[Frame], None]] = []

    def publish(self, rgb: np.ndarray) -> Frame:
        h, w = rgb.shape[:2]
        with self._lock:
            self._seq += 1
            frame = Frame(w, h, rgb, self._seq, time.monotonic())
            self._frame = frame
            subs = list(self._subscribers)
            self._lock.notify_all()
        for cb in subs:
            try:
                cb(frame)
            except Exception:  # a broken subscriber must not stop the stream
                pass
        return frame

    def latest(self) -> Frame | None:
        with self._lock:
            return self._frame

    def wait_newer(self, seq: int, timeout: float) -> Frame | None:
        deadline = time.monotonic() + timeout
        with self._lock:
            while self._frame is None or self._frame.seq <= seq:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._lock.wait(remaining)
            return self._frame

    def subscribe(self, cb: Callable[[Frame], None]) -> Callable[[], None]:
        with self._lock:
            self._subscribers.append(cb)

        def unsubscribe() -> None:
            with self._lock:
                if cb in self._subscribers:
                    self._subscribers.remove(cb)

        return unsubscribe

    def clear(self) -> None:
        with self._lock:
            self._frame = None
