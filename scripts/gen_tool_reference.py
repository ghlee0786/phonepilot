"""Generate docs/MCP_TOOLS.md from the live MCP tool definitions (single source of truth).

Usage: .venv\\Scripts\\python.exe scripts\\gen_tool_reference.py
Re-run whenever a tool, its arguments or its description changes.
"""

from __future__ import annotations

import asyncio
import json
from datetime import date
from pathlib import Path
from typing import Any

from phonepilot.agent.mcp_server import build_server
from phonepilot.security.policy import ALWAYS_HIGH_TOOLS, READ_ONLY_TOOLS
from phonepilot.testing.fake_device import FakeDevice, demo_screens
from phonepilot.testing.harness import build_stack

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "MCP_TOOLS.md"

GROUPS = [
    ("관찰 (읽기 전용)", ["observe", "screenshot", "zoom", "ui_tree", "find_elements", "get_device_info", "get_foreground_app", "list_apps", "get_gesture_profile"]),
    ("행동", ["tap", "double_tap", "long_press", "swipe", "scroll", "scroll_to", "fling", "drag", "pinch", "type_text", "press_key", "system_action", "launch_app", "stop_app", "open_url", "push_file", "install_apk", "batch_actions"]),
    ("대기", ["wait_for", "wait_stable", "sleep"]),
    ("비밀값 (값은 절대 반환되지 않음)", ["list_secret_refs", "type_secret", "type_profile", "secret_matches_screen"]),
    ("사람 개입·승인", ["request_human", "request_approval", "notify_user"]),
    ("스킬·워크플로", ["list_skills", "get_skill", "run_workflow", "workflow_status", "workflow_control", "checkpoint", "capture_backup_codes", "verify_2sv_methods"]),
    ("세션", ["session_info", "release_control", "end_session_tool"]),
    ("연동: YouTube (설정에서 켤 때만 노출)", ["yt_search_channels", "yt_channel_report", "yt_category_list"]),
    ("연동: 거래소·증권 (설정에서 켤 때만 노출)", ["market_quote", "account_balances", "account_open_orders", "order_draft", "order_submit", "order_cancel"]),
]


def _type(schema: dict[str, Any]) -> str:
    if "anyOf" in schema:
        return " | ".join(_type(s) for s in schema["anyOf"])
    t = schema.get("type", "any")
    if t == "array":
        return f"list[{_type(schema.get('items', {}))}]"
    return {"string": "str", "integer": "int", "number": "float", "boolean": "bool", "object": "dict", "null": "null"}.get(t, str(t))


# tools whose body goes through ToolService._act (directly, or per inner action for scroll_to/batch/capture)
ACTION_TOOLS = {
    "tap", "double_tap", "long_press", "swipe", "scroll", "scroll_to", "fling", "drag", "pinch", "type_text", "press_key",
    "system_action", "launch_app", "stop_app", "open_url", "push_file", "batch_actions", "type_secret", "type_profile",
    "capture_backup_codes",
}


def _gate(name: str, annotations: Any) -> str:
    if name in ALWAYS_HIGH_TOOLS:
        return "항상 강화 승인 (CONFIRM_HIGH)"
    if name in ACTION_TOOLS:
        return "정책 엔진 분류 (대상에 따라 ALLOW/CONFIRM/CONFIRM_HIGH/BLOCK)"
    if name in {"request_approval", "end_session_tool"}:
        return "사용자 승인 창을 띄움"
    if name == "request_human":
        return "사용자 안내 창을 띄우고 완료/취소까지 대기"
    if name == "workflow_control":
        return "skip_step만 사용자 승인"
    if name in READ_ONLY_TOOLS:
        return "없음 (읽기 전용)"
    return "없음 (폰 조작 없음)"


async def collect() -> dict[str, Any]:
    stack = build_stack(FakeDevice(demo_screens(), "home"))
    stack.tools.integrations = {"youtube": object(), "market": {"upbit": object()}}  # expose all tools for the reference

    async def noop() -> None:
        return None

    server = build_server(stack.tools, end_session=noop)
    return {t.name: t for t in await server.list_tools()}


def render(tools: dict[str, Any]) -> str:
    lines = [
        "# PhonePilot MCP 도구 레퍼런스",
        "",
        f"`scripts/gen_tool_reference.py`가 실제 서버 정의에서 생성했다({date.today().isoformat()}, 도구 {len(tools)}개). 직접 고치지 말고 스크립트를 다시 실행한다.",
        "",
        "공통 규칙: 결과는 `structuredContent`(JSON) + 같은 내용의 텍스트 + 필요 시 이미지. 오류는 `isError: true`와 `{code, message, hint, retryable}`. 모든 출력은 비밀값·민감 패턴이 마스킹된 뒤 나간다.",
        "",
        "행동 도구: `reason`(타임라인·승인 창에 표시)을 받고, 실행 후 화면 안정화까지 자동 대기한다. 결과는 `{ok, screen_changed, package_changed, new_dialogs, warnings[UNSTABLE_SCREEN|LOOP_SUSPECTED], approval?, elapsed_ms}`(`tap`은 `return_observation=true`면 새 관찰 포함). 대상 지정은 `element`+`obs_id`(권장) / `selector` / `x_norm`+`y_norm`(0~1000) 중 하나.",
        "",
    ]
    seen: set[str] = set()
    for title, names in GROUPS:
        present = [n for n in names if n in tools]
        if not present:
            continue
        lines += [f"## {title}", ""]
        for name in present:
            seen.add(name)
            lines += _tool_block(name, tools[name])
    rest = sorted(set(tools) - seen)
    if rest:
        lines += ["## 기타", ""]
        for name in rest:
            lines += _tool_block(name, tools[name])
    return "\n".join(lines).rstrip() + "\n"


def _tool_block(name: str, tool: Any) -> list[str]:
    schema = tool.input_schema or {}
    props: dict[str, Any] = {k: v for k, v in (schema.get("properties") or {}).items() if k != "ctx"}
    required = set(schema.get("required") or [])
    ann = tool.annotations
    flags = []
    if ann is not None:
        if ann.read_only_hint:
            flags.append("readOnly")
        if ann.destructive_hint:
            flags.append("destructive")
    out = [f"### `{name}`", "", (tool.description or "").strip(), "", f"- 승인 게이트: {_gate(name, ann)}" + (f" · 주석: {', '.join(flags)}" if flags else ""), ""]
    if props:
        out += ["| 인자 | 타입 | 필수 | 기본값 |", "| --- | --- | --- | --- |"]
        for pname, pschema in props.items():
            default = pschema.get("default", "")
            default_s = json.dumps(default, ensure_ascii=False) if default != "" else ""
            out.append(f"| `{pname}` | {_type(pschema)} | {'예' if pname in required else ''} | {default_s} |")
        out.append("")
    return out


def main() -> None:
    tools = asyncio.run(collect())
    OUT.write_text(render(tools), encoding="utf-8")
    print(f"wrote {OUT} ({len(tools)} tools)")


if __name__ == "__main__":
    main()
