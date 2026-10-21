"""Download the device-side binaries PhonePilot bundles (PRD 19.6 NFR-DEP-003).

- Android platform-tools (adb.exe + DLLs) from Google's repository.
- scrcpy-server from the pinned Genymobile release, verified against its SHA-256.

Usage: python scripts/fetch_vendor.py [--force]
"""

from __future__ import annotations

import argparse
import hashlib
import io
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VENDOR = ROOT / "vendor"

SCRCPY_VERSION = "4.1"
SCRCPY_SERVER_URL = (
    f"https://github.com/Genymobile/scrcpy/releases/download/v{SCRCPY_VERSION}/scrcpy-server-v{SCRCPY_VERSION}"
)
SCRCPY_SERVER_SHA256 = "deacb991ed2509715160ffdc7907e47b4160eb30d1566217e9047fd5b8850cae"
PLATFORM_TOOLS_URL = "https://dl.google.com/android/repository/platform-tools-latest-windows.zip"


def _download(url: str) -> bytes:
    print(f"downloading {url}")
    with urllib.request.urlopen(url, timeout=120) as resp:  # noqa: S310 - fixed https URLs
        return resp.read()


def fetch_scrcpy_server(force: bool) -> Path:
    target = VENDOR / "scrcpy-server"
    if target.exists() and not force:
        if hashlib.sha256(target.read_bytes()).hexdigest() == SCRCPY_SERVER_SHA256:
            print(f"scrcpy-server {SCRCPY_VERSION} already present")
            return target
    data = _download(SCRCPY_SERVER_URL)
    digest = hashlib.sha256(data).hexdigest()
    if digest != SCRCPY_SERVER_SHA256:
        raise SystemExit(f"scrcpy-server hash mismatch: {digest}")
    target.write_bytes(data)
    (VENDOR / "scrcpy-server.version").write_text(SCRCPY_VERSION, encoding="ascii")
    print(f"scrcpy-server {SCRCPY_VERSION} ok ({digest[:12]}...)")
    return target


def fetch_platform_tools(force: bool) -> Path:
    target_dir = VENDOR / "platform-tools"
    adb = target_dir / "adb.exe"
    if adb.exists() and not force:
        print("platform-tools already present")
        return adb
    data = _download(PLATFORM_TOOLS_URL)
    print(f"platform-tools sha256 {hashlib.sha256(data).hexdigest()}")
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for name in zf.namelist():
            base = name.split("/", 1)[-1]
            if base in {"adb.exe", "AdbWinApi.dll", "AdbWinUsbApi.dll", "NOTICE.txt", "source.properties"}:
                (target_dir / base).parent.mkdir(parents=True, exist_ok=True)
                (target_dir / base).write_bytes(zf.read(name))
    return adb


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    VENDOR.mkdir(exist_ok=True)
    fetch_scrcpy_server(args.force)
    if sys.platform == "win32":
        fetch_platform_tools(args.force)
    else:
        print("non-Windows host: install platform-tools from your package manager")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
