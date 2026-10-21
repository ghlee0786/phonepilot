"""Well-known file locations (PRD 21.5)."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def repo_root() -> Path:
    if getattr(sys, "frozen", False):  # PyInstaller
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parents[2]


def vendor_dir() -> Path:
    return repo_root() / "vendor"


def appdata_dir() -> Path:
    return Path(os.environ.get("APPDATA", Path.home() / ".config")) / "PhonePilot"


def localdata_dir() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local" / "share")) / "PhonePilot"


def temp_root() -> Path:
    return Path(os.environ.get("TEMP", "/tmp")) / "PhonePilot"


def expand(path: str) -> Path:
    return Path(os.path.expandvars(path)).expanduser()
