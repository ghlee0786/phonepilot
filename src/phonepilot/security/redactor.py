"""Masking of secret values and sensitive patterns in any outgoing text (PRD 12.4)."""

from __future__ import annotations

import base64
import re
import urllib.parse
from typing import Any

from phonepilot.security.secrets import SecretVault

CARD = re.compile(r"\b(?:\d[ -]?){13,19}\b")
RRN = re.compile(r"\b\d{6}-?[1-4]\d{6}\b")
JWT = re.compile(r"\beyJ[\w-]{8,}\.[\w-]{8,}\.[\w-]{8,}\b")
OTPAUTH = re.compile(r"otpauth://[^\s\"']+", re.IGNORECASE)
TOTP_GROUPED = re.compile(r"\b(?:[a-zA-Z2-7]{4}[ -]){3,7}[a-zA-Z2-7]{4}\b")
TOTP_PLAIN = re.compile(r"\b[A-Z2-7]{16,32}\b")
# context-only patterns (applied on sensitive screens: 2SV pages, backup codes)
BACKUP_CODE = re.compile(r"\b\d{4}[ -]?\d{4}\b")
OTP_CODE = re.compile(r"\b\d{6}\b")


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


class Redactor:
    def __init__(self, vault: SecretVault | None = None) -> None:
        self._needles: list[tuple[bytes, str]] = []
        if vault is not None:
            self.refresh(vault)

    def refresh(self, vault: SecretVault) -> None:
        needles: list[tuple[bytes, str]] = []
        for s in vault.all_secrets():
            if s.kind == "profile":
                continue
            with s.reveal() as buf:
                raw = bytes(buf)
            if len(raw) < 4:
                continue
            variants = {
                raw,
                urllib.parse.quote(raw, safe="").encode(),
                urllib.parse.quote_plus(raw).encode(),
                base64.b64encode(raw).rstrip(b"="),
                base64.urlsafe_b64encode(raw).rstrip(b"="),
            }
            for v in variants:
                if len(v) >= 4:
                    needles.append((v, s.name))
            raw = b""
        needles.sort(key=lambda t: -len(t[0]))
        self._needles = needles

    def clear(self) -> None:
        self._needles = []

    def redact(self, text: str, sensitive_context: bool = False) -> str:
        if not text:
            return text
        data = text.encode("utf-8")
        for needle, name in self._needles:
            if needle in data:
                data = data.replace(needle, f"[REDACTED:{name}]".encode())
        out = data.decode("utf-8", errors="replace")
        out = OTPAUTH.sub("[REDACTED:otpauth]", out)
        out = JWT.sub("[REDACTED:jwt]", out)
        out = RRN.sub("[REDACTED:rrn]", out)
        out = CARD.sub(lambda m: "[REDACTED:card]" if _luhn_ok(re.sub(r"\D", "", m.group())) else m.group(), out)
        out = TOTP_GROUPED.sub(lambda m: "[REDACTED:totp_key]" if _looks_base32_key(m.group()) else m.group(), out)
        out = TOTP_PLAIN.sub("[REDACTED:totp_key]", out)
        if sensitive_context:
            out = BACKUP_CODE.sub("[REDACTED:code]", out)
            out = OTP_CODE.sub("[REDACTED:otp]", out)
        return out

    def redact_obj(self, obj: Any, sensitive_context: bool = False) -> Any:
        """Recursively redact strings inside dicts/lists."""
        if isinstance(obj, str):
            return self.redact(obj, sensitive_context)
        if isinstance(obj, dict):
            return {k: self.redact_obj(v, sensitive_context) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [self.redact_obj(v, sensitive_context) for v in obj]
        return obj

    def contains_secret(self, text: str) -> bool:
        data = text.encode("utf-8")
        return any(needle in data for needle, _ in self._needles)


def _looks_base32_key(s: str) -> bool:
    letters = re.sub(r"[ -]", "", s)
    # Real keys mix letters and digits 2-7; plain words separated by spaces rarely are 16+ chars of that alphabet
    return len(letters) >= 16 and any(c in "234567" for c in letters)
