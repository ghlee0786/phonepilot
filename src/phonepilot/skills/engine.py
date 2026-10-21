"""Workflow engine: runs executable skills step by step (PRD 11.3).

Run state lives in memory only (FR-SKL-001). Each step has a post-condition (`expect`);
failures retry, then try `agent_recover` (if an agent runner is attached), then hand off to a human.
"""

from __future__ import annotations

import asyncio
import itertools
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from phonepilot.errors import PhonePilotError
from phonepilot.observe import uitree
from phonepilot.skills.loader import TEMPLATE, SkillRegistry
from phonepilot.skills.schema import Expect, Skill, Step

AgentRunner = Callable[[str, Expect | None, int, list[str] | None, str], Awaitable[bool]]

_run_ids = itertools.count(1)


class RunState(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    WAITING_HUMAN = "WAITING_HUMAN"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    PAUSED = "PAUSED"
    PAUSED_DISCONNECTED = "PAUSED_DISCONNECTED"
    COMPLETED = "COMPLETED"
    NEEDS_ATTENTION = "NEEDS_ATTENTION"
    FAILED = "FAILED"
    ABORTED = "ABORTED"


TERMINAL = {RunState.COMPLETED, RunState.NEEDS_ATTENTION, RunState.FAILED, RunState.ABORTED}


@dataclass
class WorkflowRun:
    run_id: str
    skill: Skill
    inputs: dict[str, Any]
    requested_by: str
    state: RunState = RunState.PENDING
    current_step: str | None = None
    completed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    history: list[dict[str, Any]] = field(default_factory=list)
    vars: dict[str, Any] = field(default_factory=dict)
    error: dict[str, Any] | None = None
    report: dict[str, Any] = field(default_factory=dict)
    human_interventions: dict[str, int] = field(default_factory=dict)
    started: float = field(default_factory=time.time)
    finished: float | None = None
    task: asyncio.Task[None] | None = field(default=None, repr=False)
    resume: asyncio.Event = field(default_factory=asyncio.Event, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "skill": self.skill.name,
            "state": self.state.value,
            "current_step": self.current_step,
            "completed": self.completed,
            "skipped": self.skipped,
            "total_steps": len(self.skill.steps),
            "error": self.error,
            "report": self.report,
            "human_interventions": self.human_interventions,
            "elapsed_s": int((self.finished or time.time()) - self.started),
        }


class StepFailed(Exception):
    def __init__(self, step: str, detail: str) -> None:
        super().__init__(f"{step}: {detail}")
        self.step = step
        self.detail = detail


class WorkflowEngine:
    def __init__(self, tools: Any, registry: SkillRegistry) -> None:
        self.tools = tools
        self.registry = registry
        self.runs: dict[str, WorkflowRun] = {}
        self.agent_runner: AgentRunner | None = None
        self.listeners: list[Callable[[WorkflowRun], None]] = []
        self.on_account_created: Callable[[], None] | None = None

    # ------------------------------------------------------------- queries
    def list(self, tag: str | None = None) -> list[dict[str, Any]]:
        out = []
        for name, ls in sorted(self.registry.skills.items()):
            if tag and tag not in ls.skill.tags:
                continue
            out.append(
                {
                    "name": name,
                    "description": ls.skill.description,
                    "version": ls.skill.version,
                    "sensitive": ls.skill.sensitive,
                    "inputs": {k: v.model_dump() for k, v in ls.skill.inputs.items()},
                }
            )
        return out

    def describe(self, name: str) -> dict[str, Any]:
        try:
            ls = self.registry.get(name)
        except KeyError:
            raise PhonePilotError("ELEMENT_NOT_FOUND", f"unknown skill {name}") from None
        return {"name": name, "sha256": ls.sha256, "warnings": ls.warnings, "skill": ls.skill.model_dump(exclude_none=True)}

    def status(self, run_id: str) -> dict[str, Any]:
        run = self.runs.get(run_id)
        if run is None:
            raise PhonePilotError("ELEMENT_NOT_FOUND", f"unknown run {run_id}")
        return run.to_dict()

    def active(self) -> list[dict[str, Any]]:
        return [r.to_dict() for r in self.runs.values() if r.state not in TERMINAL]

    def _emit(self, run: WorkflowRun) -> None:
        for cb in list(self.listeners):
            try:
                cb(run)
            except Exception:
                pass

    # ------------------------------------------------------------- control
    async def start(self, name: str, inputs: dict[str, Any], requested_by: str = "user", wait: bool = False) -> dict[str, Any]:
        try:
            ls = self.registry.get(name)
        except KeyError:
            raise PhonePilotError("ELEMENT_NOT_FOUND", f"unknown skill {name}") from None
        skill = ls.skill
        merged: dict[str, Any] = {}
        for key, spec in skill.inputs.items():
            if key in inputs:
                merged[key] = inputs[key]
            elif spec.default is not None:
                merged[key] = spec.default
            elif spec.required:
                raise PhonePilotError("WORKFLOW_STEP_FAILED", f"missing input {key}")
            if spec.type == "secret_ref" and key in merged and not self.tools.secrets.has(str(merged[key])):
                raise PhonePilotError("SECRET_NOT_FOUND", f"input {key}: {merged[key]} is not in .env")
        if skill.account_creation:
            self.tools.counters.check_account_creation()
        if any(r.state not in TERMINAL for r in self.runs.values()):
            raise PhonePilotError("CONTROL_BUSY", "another workflow is running")
        run = WorkflowRun(f"wf-{next(_run_ids):04d}", skill, merged, requested_by)
        self.runs[run.run_id] = run
        if skill.sensitive or skill.account_creation:
            run.state = RunState.WAITING_APPROVAL
            self._emit(run)
            await self.tools.request_approval(
                actor=f"workflow:{skill.name}",
                title=f"워크플로 시작: {skill.name}",
                summary=skill.description,
                risk="high" if skill.account_creation else "medium",
            )
        run.task = asyncio.create_task(self._run(run))
        if wait:
            await run.task
        return run.to_dict()

    async def control(self, run_id: str, action: str) -> dict[str, Any]:
        run = self.runs.get(run_id)
        if run is None:
            raise PhonePilotError("ELEMENT_NOT_FOUND", f"unknown run {run_id}")
        if action == "pause":
            run.state = RunState.PAUSED
            run.resume.clear()
        elif action == "resume":
            if run.state in (RunState.PAUSED, RunState.PAUSED_DISCONNECTED):
                run.state = RunState.RUNNING
                self.tools.arbiter.resume_ai()
                run.resume.set()
        elif action == "abort":
            run.state = RunState.ABORTED
            run.finished = time.time()
            if run.task and not run.task.done():
                run.task.cancel()
        elif action == "skip_step":
            await self.tools.request_approval(actor=f"workflow:{run.skill.name}", title="단계 건너뛰기", summary=str(run.current_step))
            run.vars["_skip_current"] = True
            run.resume.set()
        else:
            raise PhonePilotError("ELEMENT_NOT_FOUND", f"unknown action {action}")
        self._emit(run)
        return run.to_dict()

    def abort_all(self) -> None:
        for run in self.runs.values():
            if run.state not in TERMINAL:
                run.state = RunState.ABORTED
                run.finished = time.time()
                if run.task and not run.task.done():
                    run.task.cancel()
                self._emit(run)

    # ------------------------------------------------------------- running
    def _render(self, value: Any, run: WorkflowRun) -> Any:
        if isinstance(value, str):
            return TEMPLATE.sub(lambda m: str(run.inputs.get(m.group(1), "")), value)
        if isinstance(value, dict):
            return {k: self._render(v, run) for k, v in value.items()}
        if isinstance(value, list):
            return [self._render(v, run) for v in value]
        return value

    async def _check(self, expect: Expect | None, run: WorkflowRun, timeout_override: int | None = None) -> bool:
        if expect is None:
            return True
        timeout = (timeout_override if timeout_override is not None else expect.timeout_ms) / 1000
        deadline = time.monotonic() + timeout
        e = Expect.model_validate(self._render(expect.model_dump(), run))
        while True:
            obs = await self.tools.observer.observe()
            texts = obs.screen_texts()
            ok = True
            if e.package and obs.package != e.package:
                ok = False
            if e.text and not any(e.text == t or e.text in t for t in texts):
                ok = False
            if e.text_any and not any(opt == t or opt in t for opt in e.text_any for t in texts):
                ok = False
            if e.text_gone and any(e.text_gone in t for t in texts):
                ok = False
            if e.element and not uitree.find(obs.elements, e.element):
                ok = False
            if ok:
                return True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(0.3)

    async def _wait_if_paused(self, run: WorkflowRun) -> None:
        while run.state in (RunState.PAUSED, RunState.PAUSED_DISCONNECTED):
            run.resume.clear()
            await run.resume.wait()

    async def _run(self, run: WorkflowRun) -> None:
        run.state = RunState.RUNNING
        self._emit(run)
        actor = f"workflow:{run.skill.name}"
        try:
            await self._run_steps(run, run.skill.steps, actor)
            if run.skill.final_check and not await self._check(run.skill.final_check, run):
                raise StepFailed("final_check", "final verification failed")
            if run.state not in TERMINAL:
                run.state = RunState.NEEDS_ATTENTION if run.vars.get("_needs_attention") else RunState.COMPLETED
            if run.skill.account_creation and run.state == RunState.COMPLETED and self.on_account_created:
                self.on_account_created()
        except asyncio.CancelledError:
            run.state = RunState.ABORTED
        except StepFailed as e:
            run.state = RunState.FAILED
            run.error = {"step": e.step, "detail": e.detail}
        except PhonePilotError as e:
            run.state = RunState.ABORTED if e.code in {"APPROVAL_DENIED", "KILL_SWITCH_ACTIVE"} else RunState.FAILED
            run.error = {"step": run.current_step, **e.to_dict()}
        finally:
            run.finished = time.time()
            run.current_step = None
            self._emit(run)

    async def _run_steps(self, run: WorkflowRun, steps: list[Step], actor: str) -> None:
        index = 0
        by_id = {s.id: i for i, s in enumerate(steps)}
        while index < len(steps):
            step = steps[index]
            await self._wait_if_paused(run)
            if run.state in TERMINAL:
                return
            run.current_step = step.id
            self._emit(run)
            if step.skip_if is not None and await self._check(step.skip_if, run, timeout_override=500):
                run.skipped.append(step.id)
                index += 1
                continue
            jump = await self._run_step_with_recovery(run, step, actor)
            if run.vars.pop("_skip_current", False):
                run.skipped.append(step.id)
            else:
                run.completed.append(step.id)
            run.history.append({"step": step.id, "at": time.time()})
            self._emit(run)
            if jump:
                if jump not in by_id:
                    raise StepFailed(step.id, f"goto unknown step {jump}")
                index = by_id[jump]
            else:
                index += 1

    async def _run_step_with_recovery(self, run: WorkflowRun, step: Step, actor: str) -> str | None:
        retries = step.retry if step.retry is not None else run.skill.on_error.retry
        last: Exception | None = None
        for attempt in range(retries + 1):
            try:
                jump = await self._run_step(run, step, actor)
                if step.expect is not None and not await self._check(step.expect, run):
                    raise StepFailed(step.id, "expected state not reached")
                return jump
            except PhonePilotError as e:
                if e.code in {"APPROVAL_DENIED", "KILL_SWITCH_ACTIVE", "POLICY_BLOCKED", "SECRET_NOT_FOUND", "LIMIT_EXCEEDED", "APPROVAL_TIMEOUT"}:
                    raise
                last = e
            except StepFailed as e:
                last = e
            if step.optional:
                return None
            await asyncio.sleep(0.5)
        # recovery
        strategy = run.skill.on_error.then
        if strategy == "agent_recover" and self.agent_runner is not None and step.expect is not None:
            ok = await self.agent_runner(
                f"워크플로 '{run.skill.name}'의 단계 '{step.id}'({step.description})가 실패했습니다. 화면을 이 단계의 기대 상태로 만드세요.",
                step.expect, 8, None, actor,
            )
            if ok and await self._check(step.expect, run):
                return None
        if strategy in {"agent_recover", "human"}:
            outcome = await self._human(run, actor, "manual_step", f"단계 '{step.description or step.id}'를 자동으로 끝내지 못했습니다. 폰에서 직접 완료한 뒤 [계속]을 눌러 주세요.")
            if outcome == "continued" and await self._check(step.expect, run, timeout_override=3000):
                return None
        raise StepFailed(step.id, str(last) if last else "failed")

    async def _human(self, run: WorkflowRun, actor: str, kind: str, instructions: str) -> str:
        run.state = RunState.WAITING_HUMAN
        run.human_interventions[kind] = run.human_interventions.get(kind, 0) + 1
        self._emit(run)
        res = await self.tools.request_human(actor=actor, kind=kind, instructions=instructions)
        if res["outcome"] == "aborted":
            run.state = RunState.ABORTED
            raise PhonePilotError("APPROVAL_DENIED", "aborted by the user")
        run.state = RunState.RUNNING
        self._emit(run)
        return str(res["outcome"])

    async def _run_step(self, run: WorkflowRun, step: Step, actor: str) -> str | None:
        t = step.type
        if t == "action":
            if not step.action:
                raise StepFailed(step.id, "action missing")
            fn = getattr(self.tools, step.action, None)
            if fn is None:
                raise StepFailed(step.id, f"unknown tool {step.action}")
            args = self._render(step.args, run)
            result = await fn(actor=actor, **args)
            if step.then == "tap":
                el = result.get("element") if isinstance(result, dict) else None
                if el is None:
                    raise StepFailed(step.id, "nothing to tap")
                await self.tools.tap(actor=actor, element=el["id"], obs_id=result["obs_id"], reason=step.description)
            return None
        if t == "observe":
            obs = await self.tools.observer.observe()
            run.vars[step.capture or step.id] = [e.label for e in obs.elements]
            return None
        if t == "assert":
            if not await self._check(step.condition, run):
                raise StepFailed(step.id, step.message or "assertion failed")
            return None
        if t == "branch":
            ok = await self._check(step.condition, run, timeout_override=step.condition.timeout_ms if step.condition else 0)
            return step.goto if ok else step.else_goto
        if t == "loop":
            for _ in range(step.max_iter):
                if step.until is not None and await self._check(step.until, run, timeout_override=500):
                    return None
                await self._run_steps(run, step.steps, actor)
            if step.until is not None and not await self._check(step.until, run, timeout_override=500):
                raise StepFailed(step.id, "loop limit reached")
            return None
        if t == "agent_step":
            if step.success is not None and await self._check(step.success, run, timeout_override=500):
                return None
            if self.agent_runner is None:
                await self._human(run, actor, "manual_step", f"{step.goal}\n(자동 에이전트가 없어 사람이 진행해야 합니다. 완료 후 [계속])")
            else:
                ok = await self.agent_runner(self._render(step.goal, run), step.success, step.max_actions, step.allowed_tools, actor)
                if not ok:
                    raise StepFailed(step.id, "agent could not reach the goal")
            if step.success is not None and not await self._check(step.success, run):
                raise StepFailed(step.id, "success condition not met")
            return None
        if t == "human":
            while True:
                await self._human(run, actor, step.kind, self._render(step.instructions, run))
                if step.success is None or await self._check(step.success, run, timeout_override=3000):
                    return None
        if t == "approval":
            run.state = RunState.WAITING_APPROVAL
            self._emit(run)
            await self.tools.request_approval(actor=actor, title=self._render(step.title, run), summary=self._render(step.summary, run), risk=step.risk)
            run.state = RunState.RUNNING
            return None
        if t == "secret_input":
            # `ref` is either an input name holding a secret ref, or a literal secret ref
            ref = str(run.inputs.get(step.ref or "", self._render(step.ref, run)))
            target = self._render(step.target or {}, run)
            await self.tools.type_secret(actor=actor, ref=ref, submit=step.submit, **({"selector": target} if target else {}))
            return None
        if t == "call":
            child = self.registry.get(step.skill or "").skill
            sub = WorkflowRun(run.run_id + "." + child.name, child, self._render(step.inputs, run), actor)
            await self._run_steps(sub, child.steps, f"workflow:{child.name}")
            return None
        if t == "capture":
            if step.capture == "backup_codes":
                account = str(self._render(step.account_ref or "GOOGLE_MAIN", run))
                res = await self.tools.capture_backup_codes(actor=actor, account_ref=account)
                run.report["backup_codes"] = {"count": res["count"], "path_hint": res["path_hint"]}
                return None
            if step.capture == "2sv_methods":
                res = await self.tools.verify_2sv_methods(actor=actor)
                run.report["two_step"] = res
                if res["phone_methods"]:
                    run.vars["_needs_attention"] = True
                return None
            raise StepFailed(step.id, f"unknown capture {step.capture}")
        raise StepFailed(step.id, f"unknown step type {t}")
