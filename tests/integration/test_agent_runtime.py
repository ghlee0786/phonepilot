"""Built-in agent loop (fake Claude client), full runtime over real HTTP MCP, cleanup (TC-CLN-01) and canary scan (TC-SEC-07)."""

import asyncio
import json
import os
import socket
from pathlib import Path
from types import SimpleNamespace

import httpx2
import pytest
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client

from phonepilot.agent.builtin_agent import BuiltinAgent, tool_definitions
from phonepilot.agent.mcp_server import build_server
from phonepilot.config import Config
from phonepilot.core.scrcpy import protocol as p
from phonepilot.logging_setup import RING, setup_logging

from tests.conftest import CANARY_EMAIL, CANARY_PASSWORD


def block(**kw):
    return SimpleNamespace(**kw)


class FakeClaude:
    """Scripted stand-in for AsyncAnthropic: returns queued responses and records requests."""

    def __init__(self, script):
        self.script = list(script)
        self.requests = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    async def _create(self, **kw):
        self.requests.append(kw)
        content, stop = self.script.pop(0)
        return SimpleNamespace(content=content, stop_reason=stop, usage=SimpleNamespace(input_tokens=1000, output_tokens=100, cache_read_input_tokens=0, cache_creation_input_tokens=0))


async def test_builtin_agent_tool_loop(stack, approver):
    server = build_server(stack.tools)
    fake = FakeClaude([
        ([block(type="text", text="화면을 볼게요"), block(type="tool_use", id="t1", name="observe", input={"image": "som"})], "tool_use"),
        ([block(type="tool_use", id="t2", name="tap", input={"selector": {"text": "설정"}, "reason": "설정 열기"})], "tool_use"),
        ([block(type="tool_use", id="t3", name="type_text", input={"text": CANARY_PASSWORD})], "tool_use"),
        ([block(type="text", text="설정을 열었습니다")], "end_turn"),
    ])
    agent = BuiltinAgent(stack.tools, server, client=fake)
    events = []
    agent.listeners.append(lambda e: events.append(e.kind))
    out = await agent.run("설정 앱을 열어줘")
    assert out == "설정을 열었습니다"
    assert stack.device.current == "settings"
    req = fake.requests[-1]
    assert req["model"] == "claude-opus-5-5" and req["fallbacks"] == "default"
    assert req["system"][0]["cache_control"] == {"type": "ephemeral"}
    msgs = req["messages"]
    # append-only history: user, assistant, tool_result(user), ...
    assert [m["role"] for m in msgs] == ["user", "assistant", "user", "assistant", "user", "assistant", "user"]
    first_result = msgs[2]["content"][0]
    assert any(c["type"] == "image" for c in first_result["content"])
    secret_attempt = msgs[6]["content"][0]
    assert secret_attempt["is_error"] and "USE_TYPE_SECRET" in secret_attempt["content"][0]["text"]
    assert "tool_call" in events and events[-1] == "done"
    assert agent.usage.cost("claude-opus-5-5") > 0


async def test_agent_tool_filter_and_budget(stack):
    server = build_server(stack.tools)
    defs = await tool_definitions(server, {"observe", "tap"})
    assert [d["name"] for d in defs] == ["observe", "tap"]
    assert defs[-1]["cache_control"] == {"type": "ephemeral"}
    all_defs = await tool_definitions(server)
    assert "install_apk" not in {d["name"] for d in all_defs}
    fake = FakeClaude([([block(type="tool_use", id=f"x{i}", name="observe", input={})], "tool_use") for i in range(5)])
    agent = BuiltinAgent(stack.tools, server, client=fake, budget_usd=100)
    await agent.run("loop", max_actions=2)
    assert len(fake.requests) == 3  # stops after exceeding the action budget


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def isolated_dirs(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setenv("TEMP", str(tmp_path / "temp"))
    (tmp_path / "appdata" / "PhonePilot").mkdir(parents=True)
    env = tmp_path / "appdata" / "PhonePilot" / ".env"
    env.write_text(f"GOOGLE_MAIN_EMAIL={CANARY_EMAIL}\nGOOGLE_MAIN_PASSWORD={CANARY_PASSWORD}\n", encoding="utf-8")
    (env.parent / "secrets.meta.yaml").write_text(
        "GOOGLE_MAIN_EMAIL: {kind: username, allowed_packages: [com.google.android.gms]}\n"
        "GOOGLE_MAIN_PASSWORD: {kind: password, allowed_packages: [com.google.android.gms]}\n",
        encoding="utf-8",
    )
    return tmp_path


def test_full_runtime_over_http_cleanup_and_canary(isolated_dirs):
    from phonepilot.runtime import CoreRuntime

    setup_logging(debug=True)
    cfg = Config()
    cfg.mcp.port = _free_port()
    cfg.privacy.env_path = str(isolated_dirs / "appdata" / "PhonePilot" / ".env")
    cfg.recovery_vault.dir = str(isolated_dirs / "vault")
    rt = CoreRuntime(cfg, demo=True)
    rt.start()
    try:
        rt.call_sync(rt.connect(), timeout=30)
        url = rt.call_sync(rt.start_mcp(), timeout=30)
        token = (isolated_dirs / "local" / "PhonePilot" / "mcp.token").read_text().strip()
        assert token == rt.mcp_token

        async def session():
            for _ in range(50):
                try:
                    async with httpx2.AsyncClient() as probe:
                        await probe.get(url)
                    break
                except Exception:
                    await asyncio.sleep(0.1)
            async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=30) as http:
                async with Client(streamable_http_client(url, http_client=http)) as c:
                    obs = json.loads((await c.call_tool("observe", {})).content[0].text)
                    await c.call_tool("tap", {"selector": {"text": "설정"}})
                    await c.call_tool("tap", {"selector": {"text": "Google"}})
                    typed = await c.call_tool("type_secret", {"ref": "GOOGLE_MAIN_PASSWORD", "selector": {"resource_id": "com.google.android.gms:id/password"}})
                    await c.call_tool("type_text", {"text": "한글 입력 테스트", "selector": {"resource_id": "com.google.android.gms:id/email"}})
                    tree = await c.call_tool("ui_tree", {})
                    return obs, typed, tree
            raise AssertionError("unreachable")

        obs, typed, tree = asyncio.run(session())
        assert obs["package"] == "com.android.launcher3"
        assert json.loads(typed.content[0].text)["typed"] is True
        dumps = json.dumps([typed.model_dump(), tree.model_dump()], ensure_ascii=False)
        assert CANARY_PASSWORD not in dumps

        # unauthenticated request is refused
        r = httpx2.post(url, json={}, headers={"Authorization": "Bearer nope"})
        assert r.status_code == 401

        device = rt.device
        assert device.field_value("google_signin", "com.google.android.gms:id/password") == CANARY_PASSWORD
        session_temp = rt.session_temp
        report = rt.call_sync(rt.end_session(), timeout=30)
        assert report.failed == [], report.failed
        # TC-CLN-01
        last_clip = [m for m in device.decoded if m["type"] == p.ControlType.SET_CLIPBOARD][-1]
        assert last_clip["text"] == "" and last_clip["paste"] is False
        assert any("rm -rf /sdcard/Download/phonepilot_tmp" in c for c in device.shell_log)
        assert not session_temp.exists()
        assert not rt.secrets.has("GOOGLE_MAIN_PASSWORD")
        assert len(rt.stack.timeline.events) == 0
        # TC-SEC-07: the canary must not appear in any log or file PhonePilot wrote (the .env itself excluded)
        assert not any(CANARY_PASSWORD in line for line in RING)
        for f in isolated_dirs.rglob("*"):
            if f.is_file() and f.name != ".env":
                data = f.read_bytes()
                assert CANARY_PASSWORD.encode() not in data, f
                assert CANARY_EMAIL.encode() not in data or f.name.startswith("google_"), f
    finally:
        try:
            rt.call_sync(rt.shutdown(), timeout=30)
        finally:
            rt.stop_loop()
            for h in list(__import__("logging").getLogger().handlers):
                if h.__class__.__name__ in {"TimedRotatingFileHandler", "RingHandler"}:
                    __import__("logging").getLogger().removeHandler(h)
                    h.close()
