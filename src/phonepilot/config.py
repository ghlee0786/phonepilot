"""Configuration model (PRD 21.2). Not secret; secrets live in .env only."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

from phonepilot.paths import appdata_dir


class VideoConfig(BaseModel):
    preset: Literal["low_latency", "balanced", "high_quality", "saver"] = "balanced"
    codec: Literal["h264", "h265", "av1"] = "h264"
    max_size: int = 1920
    bit_rate_mbps: float = 8
    max_fps: int = 60
    decoder: Literal["auto", "hw", "sw"] = "auto"
    encoder: str | None = None

    def apply_preset(self) -> VideoConfig:
        presets = {
            "low_latency": (1280, 4, 60),
            "balanced": (1920, 8, 60),
            "high_quality": (0, 16, 60),
            "saver": (1280, 2, 30),
        }
        size, rate, fps = presets[self.preset]
        return self.model_copy(update={"max_size": size, "bit_rate_mbps": rate, "max_fps": fps})


class InputConfig(BaseModel):
    mod_key: Literal["left_alt", "left_win", "right_alt"] = "left_alt"
    keyboard_mode: Literal["text", "raw", "uhid"] = "text"
    non_ascii_path: Literal["clipboard", "adbkeyboard"] = "clipboard"


class ConnectionConfig(BaseModel):
    adb_path: str | None = None
    remove_server_on_exit: bool = False
    wireless_auto_reconnect: bool = True
    wireless_bit_rate_mbps: float = 4
    wireless_max_size: int = 1600


class McpConfig(BaseModel):
    port: int = 47800
    token_fixed: bool = False
    headless_actions: bool = False


class AgentConfig(BaseModel):
    model: str = "claude-opus-5-5"
    planner_model: str = "claude-opus-5-5"
    session_budget_usd: float = 5
    step_budget: int = 300
    screenshot_max_edge: int = 1280
    send_images_in_sensitive_apps: bool = False


class LimitsConfig(BaseModel):
    in_app_purchase_daily_krw: int = 0
    order_max_krw: int = 100_000
    order_daily_krw: int = 300_000
    order_daily_count: int = 10
    engagement_daily: int = 5


class SafetyConfig(BaseModel):
    approval_timeout_s: float = 120
    sensitive_apps_file: str = "policy/sensitive_apps.yaml"
    keywords_file: str = "policy/keywords.yaml"
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    allowed_symbols: list[str] = Field(default_factory=list)


class PrivacyConfig(BaseModel):
    env_path: str = "%APPDATA%/PhonePilot/.env"
    clipboard_sync: bool = True
    clear_device_clipboard_on_exit: bool = True
    close_recents_on_exit: bool = False
    debug_file_log: bool = False
    curtain: Literal["always", "on_share", "never"] = "on_share"


class VaultConfig(BaseModel):
    dir: str = "%USERPROFILE%/Documents/PhonePilot-Recovery"
    format: Literal["plaintext_acl", "dpapi"] = "plaintext_acl"


class IntegrationToggle(BaseModel):
    enabled: bool = False
    env: Literal["mock", "real"] = "mock"


class IntegrationsConfig(BaseModel):
    youtube: IntegrationToggle = Field(default_factory=IntegrationToggle)
    upbit: IntegrationToggle = Field(default_factory=lambda: IntegrationToggle(env="real"))
    kis: IntegrationToggle = Field(default_factory=IntegrationToggle)


class Config(BaseModel):
    version: int = 1
    video: VideoConfig = Field(default_factory=VideoConfig)
    input: InputConfig = Field(default_factory=InputConfig)
    connection: ConnectionConfig = Field(default_factory=ConnectionConfig)
    mcp: McpConfig = Field(default_factory=McpConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    safety: SafetyConfig = Field(default_factory=SafetyConfig)
    privacy: PrivacyConfig = Field(default_factory=PrivacyConfig)
    recovery_vault: VaultConfig = Field(default_factory=VaultConfig)
    integrations: IntegrationsConfig = Field(default_factory=IntegrationsConfig)
    allowed_push_dirs: list[str] = Field(default_factory=lambda: ["%USERPROFILE%/Documents/PhonePilot-Assets"])


def config_path() -> Path:
    return appdata_dir() / "config.yaml"


def load_config(path: Path | None = None) -> Config:
    path = path or config_path()
    if path.exists():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return Config.model_validate(data)
    return Config()


def save_config(cfg: Config, path: Path | None = None) -> Path:
    path = path or config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(cfg.model_dump(), allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path
