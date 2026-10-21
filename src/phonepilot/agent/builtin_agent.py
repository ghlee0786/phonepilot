"""Built-in agent (PRD 9.8): a Claude tool-use loop over the same ToolService the MCP server exposes.

History is append-only (current models bind thinking blocks to the exact history), so instead of
rewriting old screenshots the agent only receives images when it asks for them.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import anthropic

from phonepilot.agent.mcp_server import to_error, to_result
from phonepilot.agent.prompts import OPERATOR_PROMPT
from phonepilot.agent.tools import ToolService
from phonepilot.errors import PhonePilotError

log = logging.getLogger(__name__)

EXCLUDED_TOOLS = {"install_apk", "end_session_tool", "order_submit", "order_cancel", "release_control"}
# USD per 1M tokens (input, output) for the cost display (FR-AGT-005)
PRICES = {"claude-opus-5-5": (4.0, 20.0), "claude-sonnet-5-5": (2.0, 10.0), "claude-haiku-4-5": (1.0, 5.0)}


@dataclass
class AgentUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_write: int = 0

    def cost(self, model: str) -> float:
        pin, pout = PRICES.get(model, (4.0, 20.0))
        return (self.input_tokens * pin + self.cache_write * pin * 1.25 + self.cache_read * pin * 0.1 + self.output_tokens * pout) / 1_000_000


@dataclass
class AgentEvent:
    kind: str  # text | tool_call | tool_result | done | error
    data: dict[str, Any] = field(default_factory=dict)


async def tool_definitions(server: Any, allowed: set[str] | None = None) -> list[dict[str, Any]]:
    """Anthropic tool definitions generated from the MCP server's tool list (single source of truth)."""
    tools = []
    for t in await server.list_tools():
        if t.name in EXCLUDED_TOOLS or (allowed is not None and t.name not in allowed):
            continue
        schema = dict(t.input_schema or {"type": "object", "properties": {}})
        schema.get("properties", {}).pop("ctx", None)
        tools.append({"name": t.name, "description": (t.description or "")[:1000], "input_schema": schema})
    tools.sort(key=lambda d: d["name"])  # deterministic order keeps the prompt cache warm
    if tools:
        tools[-1]["cache_control"] = {"type": "ephemeral"}
    return tools


def _blocks(result: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for c in result.content:
        if c.type == "text":
            out.append({"type": "text", "text": c.text})
        elif c.type == "image":
            out.append({"type": "image", "source": {"type": "base64", "media_type": c.mime_type, "data": c.data}})
    return out or [{"type": "text", "text": "{}"}]


class BuiltinAgent:
    def __init__(self, service: ToolService, server: Any, model: str = "claude-opus-5-5", budget_usd: float = 5.0, client: Any = None) -> None:
        self.service = service
        self.server = server
        self.model = model
        self.budget_usd = budget_usd
        self.client = client or anthropic.AsyncAnthropic(api_key=self._api_key())
        self.usage = AgentUsage()
        self.listeners: list[Callable[[AgentEvent], None]] = []
        self._stop = False

    def _api_key(self) -> str | None:
        if self.service.secrets.has("ANTHROPIC_API_KEY"):
            with self.service.secrets.get("ANTHROPIC_API_KEY").reveal() as buf:
                return bytes(buf).decode()
        return None  # fall back to the SDK's own credential resolution

    def _emit(self, kind: str, **data: Any) -> None:
        ev = AgentEvent(kind, data)
        for cb in list(self.listeners):
            try:
                cb(ev)
            except Exception:  # noqa: BLE001
                pass

    def stop(self) -> None:
        self._stop = True

    async def run(self, goal: str, *, max_actions: int = 40, allowed_tools: list[str] | None = None, actor: str = "builtin-agent", extra_system: str = "") -> str:
        self._stop = False
        allowed = set(allowed_tools) | {"observe", "wait_for", "wait_stable", "request_human", "list_secret_refs"} if allowed_tools else None
        tools = await tool_definitions(self.server, allowed)
        system = [{"type": "text", "text": OPERATOR_PROMPT + ("\n\n" + extra_system if extra_system else ""), "cache_control": {"type": "ephemeral"}}]
        messages: list[dict[str, Any]] = [{"role": "user", "content": self.service.redactor.redact(goal)}]
        actions = 0
        final_text = ""
        while True:
            if self._stop:
                self._emit("done", reason="stopped")
                return final_text
            if self.usage.cost(self.model) >= self.budget_usd:
                self._emit("error", message=f"세션 비용 한도 ${self.budget_usd} 도달")
                return final_text
            try:
                response = await self.client.beta.messages.create(
                    model=self.model,
                    max_tokens=16000,
                    system=system,
                    tools=tools,
                    messages=messages,
                    output_config={"effort": "medium"},
                    betas=["server-side-fallback-2026-07-01"],
                    fallbacks="default",
                )
            except anthropic.APIStatusError as e:
                self._emit("error", message=f"API {e.status_code}: {e.message}")
                raise PhonePilotError("INTEGRATION_AUTH_FAILED" if e.status_code in (401, 403) else "WORKFLOW_STEP_FAILED", str(e.message)) from None
            except anthropic.APIConnectionError:
                self._emit("error", message="네트워크 오류")
                raise PhonePilotError("WORKFLOW_STEP_FAILED", "network error") from None
            u = response.usage
            self.usage.input_tokens += u.input_tokens or 0
            self.usage.output_tokens += u.output_tokens or 0
            self.usage.cache_read += getattr(u, "cache_read_input_tokens", 0) or 0
            self.usage.cache_write += getattr(u, "cache_creation_input_tokens", 0) or 0
            for block in response.content:
                if block.type == "text" and block.text:
                    final_text = block.text
                    self._emit("text", text=block.text)
            if response.stop_reason == "refusal":
                self._emit("error", message="모델이 요청을 거절했습니다")
                return final_text
            if response.stop_reason == "pause_turn":
                messages.append({"role": "assistant", "content": response.content})
                continue
            if response.stop_reason != "tool_use":
                self._emit("done", reason=response.stop_reason, cost_usd=round(self.usage.cost(self.model), 4))
                return final_text
            messages.append({"role": "assistant", "content": response.content})
            results = []
            for block in (b for b in response.content if b.type == "tool_use"):
                actions += 1
                self._emit("tool_call", name=block.name, input=self.service.redactor.redact_obj(block.input))
                if actions > max_actions:
                    res = to_error(self.service, PhonePilotError("STEP_BUDGET_EXCEEDED", f"more than {max_actions} actions"))
                elif allowed is not None and block.name not in allowed:
                    res = to_error(self.service, PhonePilotError("POLICY_BLOCKED", f"tool {block.name} not allowed in this step"))
                else:
                    res = await self._call(block.name, dict(block.input or {}), actor)
                self._emit("tool_result", name=block.name, is_error=bool(res.is_error))
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": _blocks(res), "is_error": bool(res.is_error)})
            messages.append({"role": "user", "content": results})
            if actions > max_actions:
                self._emit("done", reason="step_budget")
                return final_text

    async def _call(self, name: str, args: dict[str, Any], actor: str) -> Any:
        fn = getattr(self.service, name, None)
        if fn is None:
            return to_error(self.service, PhonePilotError("ELEMENT_NOT_FOUND", f"unknown tool {name}"))
        try:
            data = await fn(actor=actor, **{k: v for k, v in args.items() if v is not None})
            sensitive = bool(self.service.observer.last and self.service.observer.last.sensitive_screen)
            return to_result(self.service, data, sensitive)
        except PhonePilotError as e:
            return to_error(self.service, e)
        except TypeError as e:
            return to_error(self.service, PhonePilotError("ELEMENT_NOT_FOUND", f"bad arguments: {e}"))

    def workflow_runner(self) -> Any:
        """Adapter for WorkflowEngine.agent_runner."""

        async def runner(goal: str, success: Any, max_actions: int, allowed: list[str] | None, actor: str) -> bool:
            cond = json.dumps(success.model_dump(exclude_none=True), ensure_ascii=False) if success else "(없음)"
            await self.run(
                f"{goal}\n\n이 단계의 성공 조건(화면에 보여야 할 것): {cond}\n목표에 도달하면 도구 호출 없이 한 줄로 결과를 보고하고 멈춘다.",
                max_actions=max_actions,
                allowed_tools=allowed,
                actor=actor,
            )
            return True

        return runner
