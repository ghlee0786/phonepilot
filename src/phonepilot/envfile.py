"""Set one KEY=value line in the user's .env, keeping every other line (comments, order) as it is."""

from __future__ import annotations

import re
from pathlib import Path

KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


def set_env_value(path: Path, key: str, value: str) -> None:
    if not KEY_RE.match(key):
        raise ValueError(f"invalid key {key!r}")
    if "\n" in value or "\r" in value:
        raise ValueError("value must be a single line")
    lines = path.read_text(encoding="utf-8-sig").splitlines() if path.exists() else []
    line = f"{key}={value}"
    for i, old in enumerate(lines):
        if old.split("=", 1)[0].strip() == key and not old.lstrip().startswith("#"):
            lines[i] = line
            break
    else:
        lines.append(line)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
