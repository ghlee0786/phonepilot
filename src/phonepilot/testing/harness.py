"""Assemble the full PhonePilot core on top of any DeviceBackend (FakeDevice in tests and demo mode)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from phonepilot.agent.timeline import Timeline
from phonepilot.agent.tools import ToolConfig, ToolService
from phonepilot.core.arbiter import InputArbiter
from phonepilot.core.device import DeviceBackend
from phonepilot.observe.observer import Observer
from phonepilot.security.approvals import ApprovalCenter
from phonepilot.security.limits import DailyCounters
from phonepilot.security.policy import PolicyEngine
from phonepilot.security.redactor import Redactor
from phonepilot.security.secrets import SecretVault
from phonepilot.security.vault import RecoveryVault
from phonepilot.skills.engine import WorkflowEngine
from phonepilot.skills.loader import SkillRegistry

REPO_ROOT = Path(__file__).resolve().parents[3]


@dataclass
class Stack:
    device: DeviceBackend
    arbiter: InputArbiter
    observer: Observer
    policy: PolicyEngine
    approvals: ApprovalCenter
    secrets: SecretVault
    redactor: Redactor
    timeline: Timeline
    counters: DailyCounters
    recovery: RecoveryVault
    tools: ToolService
    registry: SkillRegistry
    engine: WorkflowEngine


def build_stack(
    device: DeviceBackend,
    secrets: SecretVault | None = None,
    *,
    state_dir: Path | None = None,
    vault_dir: Path | None = None,
    skills_dir: Path | None = REPO_ROOT / "skills",
    config: ToolConfig | None = None,
    policy: PolicyEngine | None = None,
) -> Stack:
    secrets = secrets or SecretVault()
    redactor = Redactor(secrets)
    policy = policy or PolicyEngine()
    arbiter = InputArbiter(device)
    observer = Observer(device, redactor, policy)
    approvals = ApprovalCenter()
    timeline = Timeline(redactor)
    counters = DailyCounters(state_dir / "limits_state.json" if state_dir else None)
    recovery = RecoveryVault(vault_dir or (state_dir or Path.cwd()) / "recovery")
    tools = ToolService(device, arbiter, observer, policy, approvals, secrets, redactor, timeline, counters, recovery, config or ToolConfig(settle_timeout_s=1.0))
    registry = SkillRegistry(skills_dir, None)
    registry.reload()
    engine = WorkflowEngine(tools, registry)
    engine.on_account_created = counters.record_account_creation
    tools.skills = engine
    return Stack(device, arbiter, observer, policy, approvals, secrets, redactor, timeline, counters, recovery, tools, registry, engine)
