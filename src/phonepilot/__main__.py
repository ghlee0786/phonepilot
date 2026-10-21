"""PhonePilot CLI.

  python -m phonepilot                 GUI (connects the first USB device)
  python -m phonepilot gui --demo      GUI with the built-in FakeDevice (no phone needed)
  python -m phonepilot headless --demo MCP server only (testing)
  python -m phonepilot doctor          environment checks
  python -m phonepilot init            create .env / secrets.meta.yaml / config.yaml templates
  python -m phonepilot skill validate <path.yaml>
  python -m phonepilot mcp-stdio       stdio relay for Claude Desktop
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import shutil
import sys
from pathlib import Path

from phonepilot.paths import appdata_dir, localdata_dir, repo_root, vendor_dir


def _setup_logging(debug: bool) -> None:
    from phonepilot.logging_setup import setup_logging

    setup_logging(debug)


def cmd_gui(args: argparse.Namespace) -> int:
    from phonepilot.config import load_config
    from phonepilot.gui.app import run_gui
    from phonepilot.runtime import CoreRuntime

    runtime = CoreRuntime(load_config(), demo=args.demo, serial=args.serial)
    return run_gui(runtime)


def cmd_headless(args: argparse.Namespace) -> int:
    from phonepilot.config import load_config
    from phonepilot.runtime import CoreRuntime

    runtime = CoreRuntime(load_config(), demo=args.demo, serial=args.serial)
    runtime.start()
    runtime.call_sync(runtime.connect(), timeout=60)
    url = runtime.call_sync(runtime.start_mcp())
    print(f"MCP: {url}  (token: {localdata_dir() / 'mcp.token'})")
    print("헤드리스 모드: 사람 승인 창이 없으므로 승인이 필요한 행동은 모두 거부됩니다. Ctrl+C로 종료.")
    try:
        runtime.thread.join()
    except KeyboardInterrupt:
        runtime.call_sync(runtime.shutdown(), timeout=30)
        runtime.stop_loop()
    return 0


def cmd_doctor(_args: argparse.Namespace) -> int:
    from phonepilot.config import config_path, load_config
    from phonepilot.paths import expand
    from phonepilot.security.secrets import SecretVault
    from phonepilot.skills.loader import SkillRegistry

    ok = True
    cfg = load_config()
    print(f"[config] {config_path()} {'(exists)' if config_path().exists() else '(defaults)'}")
    adb = vendor_dir() / "platform-tools" / "adb.exe"
    server = vendor_dir() / "scrcpy-server"
    print(f"[vendor] adb: {'ok' if adb.exists() else 'MISSING'}  scrcpy-server: {'ok' if server.exists() else 'MISSING'}")
    ok &= adb.exists() and server.exists()
    env = expand(cfg.privacy.env_path)
    vault = SecretVault.from_files(env)
    print(f"[.env] {env}: {len(vault.refs())} keys")
    for w in vault.warnings:
        print(f"  - {w}")
    for r in vault.refs():
        print(f"  {r['ref']} ({r['kind']})")
    vault.zero_all()
    reg = SkillRegistry(repo_root() / "skills", appdata_dir() / "skills", appdata_dir() / "approved_skills.json")
    reg.reload()
    print(f"[skills] {len(reg.skills)} loaded, {len(reg.errors)} errors, {len(reg.pending_user)} awaiting approval")
    for path, err in reg.errors.items():
        print(f"  ! {path}: {err[:200]}")
        ok = False
    if adb.exists():
        from phonepilot.core.adb import DeviceManager

        try:
            dm = DeviceManager()
            dm.start_server()
            devs = dm.devices()
            print(f"[adb] devices: {', '.join(f'{d.tail}:{d.state}' for d in devs) or 'none'}")
        except Exception as e:  # noqa: BLE001
            print(f"[adb] error: {e}")
    return 0 if ok else 1


def cmd_init(_args: argparse.Namespace) -> int:
    target = appdata_dir()
    target.mkdir(parents=True, exist_ok=True)
    root = repo_root()
    for src, dst in ((".env.example", ".env"), ("secrets.meta.example.yaml", "secrets.meta.yaml"), ("config.example.yaml", "config.yaml")):
        out = target / dst
        if out.exists():
            print(f"exists, kept: {out}")
            continue
        shutil.copyfile(root / src, out)
        print(f"created: {out}")
    from phonepilot.security.vault import _restrict_acl

    _restrict_acl(target / ".env")
    print("이제 .env 의 __set_me__ 값을 채우세요. 이 파일은 저장소 밖(사용자 폴더)에 있습니다.")
    return 0


def cmd_skill(args: argparse.Namespace) -> int:
    from phonepilot.skills.loader import SkillLoadError, parse

    path = Path(args.path)
    try:
        loaded = parse(path.read_text(encoding="utf-8"), path)
    except SkillLoadError as e:
        print(f"INVALID: {e}")
        return 1
    print(f"OK: {loaded.skill.name} v{loaded.skill.version} sha256={loaded.sha256[:16]}…")
    for w in loaded.warnings:
        print(f"  warning: {w}")
    return 0


def cmd_mcp_stdio(_args: argparse.Namespace) -> int:
    from phonepilot.agent.stdio_shim import amain

    asyncio.run(amain())
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="phonepilot")
    parser.add_argument("--debug", action="store_true", help="debug logging (masked, kept 3 days)")
    sub = parser.add_subparsers(dest="cmd")
    for name in ("gui", "headless"):
        p = sub.add_parser(name)
        p.add_argument("--demo", action="store_true", help="use the built-in FakeDevice")
        p.add_argument("--serial", default=None)
    sub.add_parser("doctor")
    sub.add_parser("init")
    sk = sub.add_parser("skill")
    sk.add_argument("action", choices=["validate"])
    sk.add_argument("path")
    sub.add_parser("mcp-stdio")
    args = parser.parse_args(argv)
    if args.cmd != "mcp-stdio":
        _setup_logging(args.debug)
    else:
        logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    handlers = {"gui": cmd_gui, "headless": cmd_headless, "doctor": cmd_doctor, "init": cmd_init, "skill": cmd_skill, "mcp-stdio": cmd_mcp_stdio}
    if args.cmd is None:
        args = parser.parse_args(["gui", *(argv or sys.argv[1:])])
    return handlers[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
