"""Skill loading and validation including the security rules SEC-SKL-01..06 (PRD 11.4)."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from phonepilot.security.policy import DEFAULT_KEYWORDS
from phonepilot.skills.schema import Skill, Step

TEMPLATE = re.compile(r"\{\{\s*inputs\.([A-Za-z0-9_]+)\s*\}\}")
FORBIDDEN_AGENT_TOOLS = {"install_apk", "order_submit", "order_cancel", "push_file"}


@dataclass
class LoadedSkill:
    skill: Skill
    path: Path | None
    sha256: str
    builtin: bool
    warnings: list[str] = field(default_factory=list)


class SkillLoadError(Exception):
    pass


def _walk(steps: list[Step]) -> list[Step]:
    out: list[Step] = []
    for s in steps:
        out.append(s)
        out.extend(_walk(s.steps))
    return out


def _strings(obj: Any) -> list[str]:
    if isinstance(obj, str):
        return [obj]
    if isinstance(obj, dict):
        return [x for v in obj.values() for x in _strings(v)]
    if isinstance(obj, list):
        return [x for v in obj for x in _strings(v)]
    return []


def _mentions_engagement(step: Step) -> bool:
    words = [w.lower() for w in DEFAULT_KEYWORDS["engagement"]]
    texts = [t.lower() for t in _strings(step.args) + _strings(step.target or {})]
    return any(w == t or w in t for w in words for t in texts)


def validate(skill: Skill) -> list[str]:
    """Raise SkillLoadError for security violations; return non-fatal warnings."""
    warnings: list[str] = []
    secret_inputs = {k for k, v in skill.inputs.items() if v.type == "secret_ref"}
    ids: set[str] = set()
    for step in _walk(skill.steps):
        if step.id in ids:
            raise SkillLoadError(f"duplicate step id {step.id}")
        ids.add(step.id)
        # SEC-SKL-01: secret refs can't be interpolated into templates
        for text in _strings(step.args) + _strings(step.inputs) + [step.instructions, step.summary, step.goal, step.message]:
            for name in TEMPLATE.findall(text):
                if name in secret_inputs:
                    raise SkillLoadError(f"step {step.id}: secret input '{name}' cannot be used in a template")
        if step.type == "action" and step.expect is None and not step.optional:
            warnings.append(f"step {step.id}: action without expect")
        if step.type == "secret_input" and not step.ref:
            raise SkillLoadError(f"step {step.id}: secret_input needs ref")
        if step.type == "agent_step":
            tools = set(step.allowed_tools or [])
            if tools & FORBIDDEN_AGENT_TOOLS:
                raise SkillLoadError(f"step {step.id}: agent_step may not use {sorted(tools & FORBIDDEN_AGENT_TOOLS)}")
            if step.success is None:
                warnings.append(f"step {step.id}: agent_step without success condition")
        if step.type == "loop":
            # SEC-SKL-05: no engagement taps or account creation inside loops
            for inner in _walk(step.steps):
                if inner.type == "action" and inner.action in {"tap", "double_tap", "batch_actions"} and _mentions_engagement(inner):
                    raise SkillLoadError(f"loop {step.id}: repeated engagement actions are not allowed (PRD 3.3)")
                if inner.type == "call" and inner.skill and ("account" in inner.skill and "add" in inner.skill):
                    raise SkillLoadError(f"loop {step.id}: account creation cannot be repeated (PRD 3.3)")
    if skill.account_creation:
        for step in _walk(skill.steps):
            if step.type == "loop" and any(s.type in {"secret_input", "call"} for s in _walk(step.steps)):
                raise SkillLoadError("account creation skills cannot loop over account setup")
    return warnings


def parse(text: str, path: Path | None = None, builtin: bool = False) -> LoadedSkill:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise SkillLoadError(f"YAML error: {e}") from e
    if not isinstance(data, dict):
        raise SkillLoadError("skill must be a mapping")
    try:
        skill = Skill.model_validate(data)
    except ValidationError as e:
        raise SkillLoadError(str(e)) from e
    warnings = validate(skill)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return LoadedSkill(skill, path, digest, builtin, warnings)


class SkillRegistry:
    def __init__(self, builtin_dir: Path | None, user_dir: Path | None = None, approved_hashes_file: Path | None = None) -> None:
        self.builtin_dir = builtin_dir
        self.user_dir = user_dir
        self.approved_file = approved_hashes_file
        self.skills: dict[str, LoadedSkill] = {}
        self.errors: dict[str, str] = {}
        self.pending_user: dict[str, LoadedSkill] = {}

    def _approved(self) -> set[str]:
        if self.approved_file and self.approved_file.exists():
            return set(json.loads(self.approved_file.read_text(encoding="utf-8")))
        return set()

    def approve(self, sha256: str) -> None:
        """SEC-SKL-04: user skills load only after the user approved their exact content."""
        approved = self._approved() | {sha256}
        if self.approved_file:
            self.approved_file.parent.mkdir(parents=True, exist_ok=True)
            self.approved_file.write_text(json.dumps(sorted(approved)), encoding="utf-8")
        self.reload()

    def reload(self) -> None:
        self.skills.clear()
        self.errors.clear()
        self.pending_user.clear()
        for base, builtin in ((self.builtin_dir, True), (self.user_dir, False)):
            if not base or not base.exists():
                continue
            for path in sorted(base.rglob("*.yaml")):
                try:
                    loaded = parse(path.read_text(encoding="utf-8"), path, builtin)
                except SkillLoadError as e:
                    self.errors[str(path)] = str(e)
                    continue
                if not builtin and loaded.sha256 not in self._approved():
                    self.pending_user[loaded.skill.name] = loaded
                    continue
                self.skills[loaded.skill.name] = loaded

    def get(self, name: str) -> LoadedSkill:
        if name not in self.skills:
            raise KeyError(name)
        return self.skills[name]
