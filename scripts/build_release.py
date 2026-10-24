"""Build a release: PyInstaller folder -> installer screenshots -> Inno Setup installer -> SHA-256 file.

usage: python scripts/build_release.py [--skip-exe] [--skip-assets] [--iscc PATH]
Output: dist/installer/PhonePilot-<version>-setup.exe and .sha256 (upload both to the releases repo).
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from phonepilot import __version__  # noqa: E402

ISCC_CANDIDATES = [
    os.environ.get("ISCC", ""),
    r"F:\DevTools\InnoSetup6\ISCC.exe",
    r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
    r"C:\Program Files\Inno Setup 6\ISCC.exe",
    str(Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Inno Setup 6" / "ISCC.exe"),
]

VERSION_INFO = """VSVersionInfo(
  ffi=FixedFileInfo(filevers={t}, prodvers={t}, mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('041204B0', [
      StringStruct('CompanyName', 'PhonePilot'),
      StringStruct('FileDescription', 'PhonePilot - Android 미러링·AI 제어'),
      StringStruct('FileVersion', '{v}'),
      StringStruct('ProductName', 'PhonePilot'),
      StringStruct('ProductVersion', '{v}'),
      StringStruct('LegalCopyright', 'PhonePilot')])]),
    VarFileInfo([VarStruct('Translation', [0x0412, 1200])])
  ]
)
"""


def step(msg: str) -> None:
    print(f"\n== {msg}", flush=True)


def run(cmd: list[str]) -> None:
    print("  $", " ".join(cmd), flush=True)
    subprocess.run(cmd, cwd=ROOT, check=True)


def version_tuple(v: str) -> tuple[int, int, int, int]:
    nums = [int("".join(ch for ch in part if ch.isdigit()) or 0) for part in v.split(".")[:3]]
    return (*nums, *([0] * (4 - len(nums))))  # type: ignore[return-value]


def build_exe() -> Path:
    step(f"PyInstaller (PhonePilot {__version__})")
    if not (ROOT / "vendor" / "platform-tools" / "adb.exe").exists() or not (ROOT / "vendor" / "scrcpy-server").exists():
        run([sys.executable, "scripts/fetch_vendor.py"])
    (ROOT / "build").mkdir(exist_ok=True)
    (ROOT / "build" / "version_info.txt").write_text(VERSION_INFO.format(t=version_tuple(__version__), v=__version__), encoding="utf-8")
    run([sys.executable, "-m", "PyInstaller", "packaging/phonepilot.spec", "--noconfirm", "--clean",
         "--distpath", "dist", "--workpath", "build/pyinstaller"])
    out = ROOT / "dist" / "PhonePilot"
    size = sum(f.stat().st_size for f in out.rglob("*") if f.is_file()) / 1e6
    print(f"  -> {out} ({size:.0f} MB)")
    return out


def build_assets() -> None:
    step("설치 마법사 스크린샷")
    run([sys.executable, "-I", "installer/make_installer_assets.py", "installer/assets"])


def find_iscc(explicit: str | None) -> str:
    for c in [explicit or "", *ISCC_CANDIDATES]:
        if c and Path(c).exists():
            return c
    found = shutil.which("iscc")
    if found:
        return found
    raise SystemExit("Inno Setup(ISCC.exe)을 찾지 못했습니다. --iscc 로 경로를 지정하세요")


def build_installer(iscc: str) -> Path:
    step("Inno Setup")
    run([iscc, f"/DAppVersion={__version__}", f"/DSourceDir={ROOT / 'dist' / 'PhonePilot'}", "installer/phonepilot.iss"])
    setup = ROOT / "dist" / "installer" / f"PhonePilot-{__version__}-setup.exe"
    digest = hashlib.sha256(setup.read_bytes()).hexdigest()
    sha_file = setup.with_name(setup.name + ".sha256")
    sha_file.write_text(f"{digest}  {setup.name}\n", encoding="utf-8")
    print(f"  -> {setup} ({setup.stat().st_size / 1e6:.0f} MB)\n  -> {sha_file.name}: {digest}")
    return setup


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-exe", action="store_true", help="reuse dist/PhonePilot")
    ap.add_argument("--skip-assets", action="store_true", help="reuse installer/assets")
    ap.add_argument("--exe-only", action="store_true", help="stop after PyInstaller")
    ap.add_argument("--iscc")
    args = ap.parse_args()
    if not args.skip_exe:
        build_exe()
    if args.exe_only:
        return
    if not args.skip_assets:
        build_assets()
    build_installer(find_iscc(args.iscc))


if __name__ == "__main__":
    main()
