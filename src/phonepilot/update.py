"""Release check and self-update from the public releases repository (GitHub Releases).

A release is a tag `vX.Y.Z` with two assets: `PhonePilot-X.Y.Z-setup.exe` (Inno Setup) and
`PhonePilot-X.Y.Z-setup.exe.sha256`. The installer is only run after its SHA-256 matches.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from packaging.version import InvalidVersion, Version

from phonepilot import __version__
from phonepilot.errors import PhonePilotError
from phonepilot.paths import localdata_dir

API = "https://api.github.com/repos/{repo}/releases/latest"
INSTALLER_SUFFIX = "-setup.exe"
STATE_FILE = "update_state.json"


@dataclass(frozen=True)
class Release:
    version: str
    tag: str
    notes: str
    page_url: str
    installer_url: str
    installer_name: str
    installer_size: int
    checksum_url: str  # "" when the release has no .sha256 asset


def parse_version(text: str) -> Version | None:
    try:
        return Version(text.strip().lstrip("vV"))
    except InvalidVersion:
        return None


def is_newer(remote: str, local: str = __version__) -> bool:
    r, l = parse_version(remote), parse_version(local)
    return r is not None and l is not None and r > l and not r.is_prerelease


def parse_release(data: dict[str, Any]) -> Release | None:
    """GitHub `releases/latest` JSON -> Release; None when the release has no installer asset."""
    if data.get("draft") or data.get("prerelease"):
        return None
    assets = {a.get("name", ""): a for a in data.get("assets") or []}
    installer = next((a for name, a in assets.items() if name.lower().endswith(INSTALLER_SUFFIX)), None)
    if installer is None:
        return None
    checksum = assets.get(installer["name"] + ".sha256")
    tag = str(data.get("tag_name", ""))
    return Release(
        version=tag.lstrip("vV"),
        tag=tag,
        notes=str(data.get("body") or ""),
        page_url=str(data.get("html_url", "")),
        installer_url=str(installer.get("browser_download_url", "")),
        installer_name=str(installer["name"]),
        installer_size=int(installer.get("size") or 0),
        checksum_url=str(checksum.get("browser_download_url", "")) if checksum else "",
    )


async def fetch_latest(repo: str, client: httpx.AsyncClient | None = None) -> Release | None:
    """Latest published release, or None when there is none yet (404) or it has no installer."""
    own = client is None
    client = client or httpx.AsyncClient(timeout=15, follow_redirects=True)
    try:
        res = await client.get(API.format(repo=repo), headers={"Accept": "application/vnd.github+json", "User-Agent": f"PhonePilot/{__version__}"})
        if res.status_code == 404:
            return None
        res.raise_for_status()
        return parse_release(res.json())
    except httpx.HTTPError as e:
        raise PhonePilotError("UPDATE_CHECK_FAILED", str(e)[:200]) from e
    finally:
        if own:
            await client.aclose()


async def download(
    release: Release,
    dest_dir: Path,
    on_progress: Callable[[int, int], None] | None = None,
    client: httpx.AsyncClient | None = None,
) -> Path:
    """Download the installer and verify its SHA-256. A mismatch deletes the file and raises."""
    own = client is None
    client = client or httpx.AsyncClient(timeout=60, follow_redirects=True)
    dest_dir.mkdir(parents=True, exist_ok=True)
    path = dest_dir / release.installer_name
    digest = hashlib.sha256()
    try:
        expected = ""
        if release.checksum_url:
            res = await client.get(release.checksum_url)
            res.raise_for_status()
            expected = res.text.split()[0].lower() if res.text.strip() else ""
        done = 0
        async with client.stream("GET", release.installer_url) as res:
            res.raise_for_status()
            total = int(res.headers.get("content-length") or release.installer_size or 0)
            with path.open("wb") as f:
                async for chunk in res.aiter_bytes(1 << 16):
                    f.write(chunk)
                    digest.update(chunk)
                    done += len(chunk)
                    if on_progress is not None:
                        on_progress(done, total)
    except httpx.HTTPError as e:
        path.unlink(missing_ok=True)
        raise PhonePilotError("UPDATE_DOWNLOAD_FAILED", str(e)[:200]) from e
    finally:
        if own:
            await client.aclose()
    if not expected:
        path.unlink(missing_ok=True)
        raise PhonePilotError("UPDATE_CHECKSUM_MISMATCH", "release has no .sha256 asset")
    if digest.hexdigest() != expected:
        path.unlink(missing_ok=True)
        raise PhonePilotError("UPDATE_CHECKSUM_MISMATCH", f"expected {expected[:12]}…, got {digest.hexdigest()[:12]}…")
    return path


def can_self_install() -> bool:
    """Only an installed (PyInstaller) build replaces itself; a source checkout just links to the release."""
    return bool(getattr(sys, "frozen", False)) and sys.platform == "win32"


def launch_installer(path: Path) -> None:
    # Inno Setup: progress only, close the running app; the installer's [Run] entry starts the new version
    subprocess.Popen([str(path), "/SILENT", "/CLOSEAPPLICATIONS", "/NORESTART"], close_fds=True)


# ------------------------------------------------------------------ throttle
class UpdateState:
    """When we last checked and which version the user chose to skip (LOCALAPPDATA, not secret)."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or localdata_dir() / STATE_FILE
        self.last_check = 0.0
        self.skipped = ""
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.last_check = float(data.get("last_check", 0))
            self.skipped = str(data.get("skipped", ""))
        except (OSError, ValueError):
            pass

    def due(self, interval_hours: float, now: float | None = None) -> bool:
        return (now or time.time()) - self.last_check >= interval_hours * 3600

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"last_check": self.last_check, "skipped": self.skipped}), encoding="utf-8")
