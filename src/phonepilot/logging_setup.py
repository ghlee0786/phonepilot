"""Logging: in-memory ring buffer by default, masked file log only in debug mode (PRD 12.1, SEC-012)."""

from __future__ import annotations

import logging
import logging.handlers
import time
from collections import deque

from phonepilot.paths import localdata_dir

RING: deque[str] = deque(maxlen=5000)
_redactor = None


def set_redactor(redactor: object) -> None:
    global _redactor
    _redactor = redactor


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if _redactor is not None:
            msg = record.getMessage()
            record.msg = _redactor.redact(msg)  # type: ignore[attr-defined]
            record.args = ()
        return True


class RingHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        RING.append(self.format(record))


def setup_logging(debug: bool = False) -> None:
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if debug else logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    ring = RingHandler()
    ring.setFormatter(fmt)
    ring.addFilter(RedactingFilter())
    root.addHandler(ring)
    if debug:
        logdir = localdata_dir() / "logs"
        logdir.mkdir(parents=True, exist_ok=True)
        cutoff = time.time() - 3 * 86400
        for old in logdir.glob("*.log*"):
            if old.stat().st_mtime < cutoff:
                old.unlink(missing_ok=True)
        fh = logging.handlers.TimedRotatingFileHandler(logdir / "phonepilot.log", when="D", backupCount=3, encoding="utf-8")
        fh.setFormatter(fmt)
        fh.addFilter(RedactingFilter())
        root.addHandler(fh)
