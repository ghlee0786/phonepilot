"""Secret handling (PRD 12.2-12.5, SEC-001..007).

Values live only in `bytearray` buffers inside `Secret` objects. They cannot be printed,
formatted, JSON-encoded or pickled. Access goes through `Secret.reveal()`, which callers
must use only at the injection point (control channel) or in an integration signer.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import dotenv_values

from phonepilot.errors import PhonePilotError

SCREEN_KINDS = {"username", "password", "profile", "pin"}
ALL_KINDS = SCREEN_KINDS | {"api_key", "card"}
PLACEHOLDERS = {"", "__set_me__", "changeme", "CHANGE_ME"}


@dataclass
class SecretMeta:
    kind: str = "password"
    description: str = ""
    allowed_packages: list[str] = field(default_factory=list)
    allowed_hosts: list[str] = field(default_factory=list)
    require_password_field: bool | None = None

    def needs_password_field(self) -> bool:
        if self.require_password_field is not None:
            return self.require_password_field
        return self.kind in {"password", "pin"}


def _guess_kind(name: str) -> str:
    upper = name.upper()
    if upper.endswith(("_PASSWORD", "_PASS", "_PW")):
        return "password"
    if upper.endswith("_PIN"):
        return "pin"
    if upper.endswith(("_KEY", "_SECRET", "_TOKEN", "_APP_KEY", "_APP_SECRET")):
        return "api_key"
    if upper.endswith(("_EMAIL", "_USERNAME", "_ID", "_LOGIN")):
        return "username"
    if upper.startswith("PROFILE_"):
        return "profile"
    return "password"


class Secret:
    """A named secret value that refuses to be serialized."""

    __slots__ = ("name", "meta", "_buf")

    def __init__(self, name: str, value: bytes | bytearray, meta: SecretMeta) -> None:
        self.name = name
        self.meta = meta
        self._buf = bytearray(value)

    @property
    def kind(self) -> str:
        return self.meta.kind

    @property
    def length(self) -> int:
        return len(self._buf.decode("utf-8", errors="replace")) if self._buf else 0

    def is_ascii(self) -> bool:
        return all(b < 0x80 for b in self._buf)

    @contextlib.contextmanager
    def reveal(self) -> Iterator[bytearray]:
        """Yield a temporary copy of the value; the copy is zeroed afterwards."""
        copy = bytearray(self._buf)
        try:
            yield copy
        finally:
            for i in range(len(copy)):
                copy[i] = 0

    def matches(self, text: str) -> bool:
        """Constant-ish comparison against a candidate string (used to refuse type_text with a secret)."""
        import hmac

        return bool(self._buf) and hmac.compare_digest(bytes(self._buf), text.encode("utf-8"))

    def contained_in(self, text: str) -> bool:
        return len(self._buf) >= 4 and bytes(self._buf) in text.encode("utf-8")

    def zero(self) -> None:
        for i in range(len(self._buf)):
            self._buf[i] = 0
        self._buf = bytearray()

    def __repr__(self) -> str:
        return f"Secret({self.name}, ***)"

    __str__ = __repr__

    def __format__(self, spec: str) -> str:
        return repr(self)

    def __reduce__(self) -> Any:
        raise TypeError("Secret objects cannot be pickled")

    def __getstate__(self) -> Any:
        raise TypeError("Secret objects cannot be serialized")

    def __iter__(self) -> Any:
        raise TypeError("Secret objects are not iterable")

    def __bool__(self) -> bool:
        return bool(self._buf)


class SecretVault:
    """Loads `.env` + `secrets.meta.yaml` into memory. Never writes values anywhere."""

    def __init__(self) -> None:
        self._secrets: dict[str, Secret] = {}
        self.env_path: Path | None = None
        self.warnings: list[str] = []

    @classmethod
    def from_files(cls, env_path: Path | str, meta_path: Path | str | None = None) -> SecretVault:
        vault = cls()
        vault.load(Path(env_path), Path(meta_path) if meta_path else None)
        return vault

    @classmethod
    def from_mapping(cls, values: dict[str, str], meta: dict[str, dict[str, Any]] | None = None) -> SecretVault:
        vault = cls()
        vault._ingest(values, meta or {})
        return vault

    def load(self, env_path: Path, meta_path: Path | None = None) -> None:
        self.zero_all()
        self.warnings = []
        self.env_path = env_path
        if not env_path.exists():
            self.warnings.append(f".env not found at {env_path}")
            return
        values = {k: v for k, v in dotenv_values(env_path).items() if v is not None}
        meta_path = meta_path or env_path.with_name("secrets.meta.yaml")
        meta: dict[str, dict[str, Any]] = {}
        if meta_path.exists():
            meta = yaml.safe_load(meta_path.read_text(encoding="utf-8")) or {}
        self._ingest(values, meta)
        values.clear()

    def _ingest(self, values: dict[str, str], meta: dict[str, dict[str, Any]]) -> None:
        for name, value in values.items():
            m = meta.get(name) or {}
            sm = SecretMeta(
                kind=m.get("kind") or _guess_kind(name),
                description=m.get("description", ""),
                allowed_packages=list(m.get("allowed_packages") or []),
                allowed_hosts=list(m.get("allowed_hosts") or []),
                require_password_field=m.get("require_password_field"),
            )
            if sm.kind not in ALL_KINDS:
                self.warnings.append(f"{name}: unknown kind {sm.kind!r}")
            if value in PLACEHOLDERS:
                self.warnings.append(f"{name}: placeholder or empty value")
            self._secrets[name] = Secret(name, value.encode("utf-8"), sm)

    def get(self, name: str) -> Secret:
        secret = self._secrets.get(name)
        if secret is None or not secret:
            raise PhonePilotError("SECRET_NOT_FOUND", f"secret ref {name} not found")
        return secret

    def has(self, name: str) -> bool:
        return name in self._secrets and bool(self._secrets[name])

    def refs(self, prefix: str | None = None) -> list[dict[str, Any]]:
        out = []
        for name, s in sorted(self._secrets.items()):
            if prefix and not name.upper().startswith(prefix.upper()):
                continue
            out.append(
                {
                    "ref": name,
                    "kind": s.kind,
                    "description": s.meta.description,
                    "allowed_packages": s.meta.allowed_packages,
                    "allowed_hosts": s.meta.allowed_hosts,
                    "set": bool(s),
                }
            )
        return out

    def all_secrets(self) -> list[Secret]:
        return list(self._secrets.values())

    def find_secret_in(self, text: str) -> Secret | None:
        for s in self._secrets.values():
            if s.kind in {"profile"}:
                continue
            if s.matches(text) or s.contained_in(text):
                return s
        return None

    def zero_all(self) -> None:
        for s in self._secrets.values():
            s.zero()
        self._secrets.clear()


def default_env_path() -> Path:
    return Path(os.environ.get("APPDATA", str(Path.home()))) / "PhonePilot" / ".env"
