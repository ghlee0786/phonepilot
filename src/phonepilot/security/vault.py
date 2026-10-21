"""Recovery-code vault (PRD 12.10). The only place PhonePilot persists a secret, by explicit user request."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from phonepilot.errors import PhonePilotError

CODE_RE = re.compile(r"^\s*(\d{4})[\s-]?(\d{4})\s*$")


def extract_backup_codes(texts: list[str]) -> list[str]:
    """Return normalized 8-digit codes (`1234 5678`) in order, without duplicates."""
    seen: list[str] = []
    for t in texts:
        for part in re.split(r"[\n\r]+", t):
            m = CODE_RE.match(part)
            if m:
                code = f"{m.group(1)} {m.group(2)}"
                if code not in seen:
                    seen.append(code)
    return seen


def _restrict_acl(path: Path) -> None:
    """Best effort: make the file readable only by the current user (Windows icacls)."""
    if sys.platform != "win32":
        os.chmod(path, 0o600)
        return
    user = os.environ.get("USERNAME")
    if not user:
        return
    subprocess.run(
        ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:(R,W)"],
        capture_output=True,
        check=False,
    )


def _dpapi_protect(data: bytes) -> bytes:
    import win32crypt  # type: ignore[import-not-found]

    return win32crypt.CryptProtectData(data, "PhonePilot recovery codes", None, None, None, 0)


class RecoveryVault:
    def __init__(self, directory: Path, fmt: str = "plaintext_acl") -> None:
        self.directory = directory
        self.fmt = fmt

    def check_location(self, project_root: Path | None = None) -> list[str]:
        warnings = []
        d = str(self.directory.resolve()).lower()
        if "onedrive" in d or "dropbox" in d or "google drive" in d:
            warnings.append("vault folder is inside a cloud-synced folder")
        if project_root and str(project_root.resolve()).lower() in d:
            warnings.append("vault folder is inside the project folder")
        return warnings

    def save(self, account_alias: str, account_email: str, codes: list[str], now: datetime | None = None) -> Path:
        if len(codes) != 10 or len(set(codes)) != 10 or not all(re.fullmatch(r"\d{4} \d{4}", c) for c in codes):
            raise PhonePilotError("WORKFLOW_STEP_FAILED", f"expected 10 unique 8-digit codes, got {len(codes)}")
        now = now or datetime.now()
        self.directory.mkdir(parents=True, exist_ok=True)
        alias = re.sub(r"[^A-Za-z0-9_-]", "_", account_alias) or "account"
        for old in self.directory.glob(f"google_{alias}_backup-codes_*"):
            if "_superseded" not in old.name:
                old.rename(old.with_name(old.stem + "_superseded" + old.suffix))
        stamp = now.strftime("%Y%m%d-%H%M")
        body = "\n".join(
            [
                f"Google backup codes - {account_email}",
                f"created: {now.isoformat(timespec='minutes')}",
                "",
                *codes,
                "",
                "각 코드는 한 번만 사용할 수 있습니다. 새 코드를 발급하면 이 목록은 무효가 됩니다.",
                "인쇄하거나 오프라인 매체로 옮긴 뒤 이 파일을 지우는 것을 권장합니다.",
            ]
        ).encode("utf-8")
        if self.fmt == "dpapi":
            path = self.directory / f"google_{alias}_backup-codes_{stamp}.txt.dpapi"
            path.write_bytes(_dpapi_protect(body))
        else:
            path = self.directory / f"google_{alias}_backup-codes_{stamp}.txt"
            path.write_bytes(body)
        _restrict_acl(path)
        if self.fmt != "dpapi":
            reread = extract_backup_codes(path.read_text(encoding="utf-8").splitlines())
            if reread != codes:
                raise PhonePilotError("WORKFLOW_STEP_FAILED", "vault verification failed")
        return path
