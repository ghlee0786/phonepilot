"""Register PhonePilot's MCP relay with Claude Desktop (installer step, `phonepilot setup-claude`).

The existing config is merged, never replaced: other MCP servers and settings stay as they are, and the
original file is kept once as `.bak`.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

SERVER_NAME = "phonepilot"


def mcp_command() -> tuple[str, list[str]]:
    """How Claude starts the stdio relay: the console CLI of an install, or the venv entry point from source."""
    exe_dir = Path(sys.executable).parent
    if getattr(sys, "frozen", False):
        return str(exe_dir / "phonepilot-cli.exe"), ["mcp-stdio"]
    return str(exe_dir / ("phonepilot-mcp.exe" if sys.platform == "win32" else "phonepilot-mcp")), []


def desktop_config_path() -> Path:
    return Path(os.environ.get("APPDATA", Path.home() / ".config")) / "Claude" / "claude_desktop_config.json"


def register_desktop(command: str, args: list[str], path: Path | None = None) -> Path:
    path = path or desktop_config_path()
    data: dict[str, Any] = {}
    if path.exists():
        text = path.read_text(encoding="utf-8").strip()
        if text:
            data = json.loads(text)  # a broken file raises instead of being overwritten
        backup = path.with_suffix(path.suffix + ".bak")
        if not backup.exists():
            shutil.copyfile(path, backup)
    servers = data.setdefault("mcpServers", {})
    servers[SERVER_NAME] = {"command": command, "args": args}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
