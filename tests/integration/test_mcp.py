"""TC-MCP-01..04: auth middleware, tool catalogue, observe->tap round trip, structured errors, redaction."""

import json

import httpx
import pytest
from mcp.client import Client

from phonepilot.agent.mcp_server import AuthMiddleware, build_server

from tests.conftest import CANARY_PASSWORD

EXPECTED = {
    "observe", "screenshot", "zoom", "ui_tree", "find_elements", "get_device_info", "get_foreground_app", "list_apps",
    "tap", "double_tap", "long_press", "swipe", "scroll", "scroll_to", "fling", "drag", "pinch", "type_text", "press_key",
    "system_action", "launch_app", "stop_app", "open_url", "push_file", "install_apk", "batch_actions", "wait_for",
    "wait_stable", "sleep", "list_secret_refs", "type_secret", "type_profile", "secret_matches_screen", "request_human",
    "request_approval", "notify_user", "list_skills", "get_skill", "run_workflow", "workflow_status", "workflow_control",
    "checkpoint", "capture_backup_codes", "verify_2sv_methods", "session_info", "release_control",
}


async def ok_app(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


@pytest.mark.parametrize(
    "headers,status",
    [
        ({}, 401),
        ({"Authorization": "Bearer wrong"}, 401),
        ({"Authorization": "Bearer t0ken", "Origin": "https://evil.example"}, 403),
        ({"Authorization": "Bearer t0ken", "Origin": "http://127.0.0.1:47800"}, 200),
        ({"Authorization": "Bearer t0ken"}, 200),
    ],
)
async def test_auth_middleware_tc_mcp_01(headers, status):
    app = AuthMiddleware(ok_app, "t0ken")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:47800") as c:
        r = await c.post("/mcp", headers=headers, json={})
    assert r.status_code == status


def parse(result):
    return json.loads(result.content[0].text)


async def test_tool_catalogue_tc_mcp_02(stack):
    server = build_server(stack.tools)
    async with Client(server) as c:
        tools = {t.name: t for t in (await c.list_tools()).tools}
    assert EXPECTED <= set(tools)
    assert tools["observe"].annotations.read_only_hint is True
    assert tools["install_apk"].annotations.destructive_hint is True
    assert "ctx" not in json.dumps(tools["tap"].input_schema)
    assert not any(n.startswith("order_") for n in tools)  # integrations disabled by default


async def test_observe_tap_roundtrip_tc_mcp_03(stack):
    server = build_server(stack.tools)
    async with Client(server) as c:
        obs = parse(await c.call_tool("observe", {}))
        assert obs["package"] == "com.android.launcher3"
        res = await c.call_tool("tap", {"element": 1, "obs_id": obs["obs_id"], "reason": "설정"})
        assert not res.is_error
        assert parse(res)["screen_changed"] is True
        img = await c.call_tool("observe", {"image": "som"})
        assert any(b.type == "image" for b in img.content)
    assert stack.device.current == "settings"


async def test_errors_are_structured(stack):
    server = build_server(stack.tools)
    async with Client(server) as c:
        res = await c.call_tool("tap", {"element": 99})
        assert res.is_error
        data = parse(res)
        assert data["code"] in {"ELEMENT_STALE", "ELEMENT_NOT_FOUND"} and data["hint"]


async def test_secrets_never_leave_via_mcp(stack):
    server = build_server(stack.tools)
    stack.device.goto("google_signin")
    async with Client(server) as c:
        refs = await c.call_tool("list_secret_refs", {})
        typed = await c.call_tool("type_secret", {"ref": "GOOGLE_MAIN_PASSWORD", "selector": {"resource_id": "com.google.android.gms:id/password"}})
        tree = await c.call_tool("ui_tree", {})
        obs = await c.call_tool("observe", {})
    for r in (refs, typed, tree, obs):
        assert CANARY_PASSWORD not in str(r.model_dump())
    assert parse(typed)["typed"] is True


async def test_control_lease_between_clients_tc_mcp_04(stack):
    stack.arbiter.acquire_lease("mcp:other-client")
    server = build_server(stack.tools)
    async with Client(server) as c:
        res = await c.call_tool("press_key", {"key": "tab"})
    assert res.is_error and parse(res)["code"] == "CONTROL_BUSY"


async def test_resources_and_prompts(stack):
    server = build_server(stack.tools)
    async with Client(server) as c:
        prompts = {p.name for p in (await c.list_prompts()).prompts}
        assert {"phonepilot_operator", "google_account_setup", "youtube_channel_setup", "channel_launch_checklist"} <= prompts
        res = await c.read_resource("policy://summary")
        assert "sensitive_packages" in res.contents[0].text
