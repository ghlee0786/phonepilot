"""FR-MCP-004 / FR-AGT-012: the stdio relay forwards to the running app and reports APP_NOT_RUNNING otherwise."""

import asyncio
import json
import os
import socket
import sys

from mcp import StdioServerParameters
from mcp.client import Client

from phonepilot.config import Config


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _params(url: str, token: str) -> StdioServerParameters:
    env = dict(os.environ, PHONEPILOT_MCP_URL=url, PHONEPILOT_MCP_TOKEN=token)
    return StdioServerParameters(command=sys.executable, args=["-m", "phonepilot.agent.stdio_shim"], env=env)


def test_shim_forwards_to_running_app(tmp_path, monkeypatch):
    from phonepilot.runtime import CoreRuntime

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("TEMP", str(tmp_path / "temp"))
    cfg = Config()
    cfg.mcp.port = _free_port()
    cfg.privacy.env_path = str(tmp_path / "none.env")
    rt = CoreRuntime(cfg, demo=True)
    rt.start()
    try:
        rt.call_sync(rt.connect(), timeout=30)
        url = rt.call_sync(rt.start_mcp(), timeout=30)

        async def run():
            await asyncio.sleep(0.5)
            async with Client(_params(url, rt.mcp_token)) as c:
                names = {t.name for t in (await c.list_tools()).tools}
                obs = await c.call_tool("observe", {})
                return names, json.loads(obs.content[0].text)

        names, obs = asyncio.run(run())
        assert "tap" in names and obs["package"] == "com.android.launcher3"
    finally:
        rt.call_sync(rt.shutdown(), timeout=30)
        rt.stop_loop()


def test_shim_without_app_reports_not_running():
    async def run():
        async with Client(_params(f"http://127.0.0.1:{_free_port()}/mcp", "x")) as c:
            tools = (await c.list_tools()).tools
            res = await c.call_tool("observe", {})
            return tools, res

    tools, res = asyncio.run(run())
    assert tools == []
    assert res.is_error and json.loads(res.content[0].text)["code"] == "APP_NOT_RUNNING"


def test_shim_reconnects_when_app_starts_later(tmp_path, monkeypatch):
    """A Claude session opened before the app: first call says APP_NOT_RUNNING, later calls reach the app
    (token re-read from the file the app writes on start)."""
    from phonepilot.runtime import CoreRuntime

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("TEMP", str(tmp_path / "temp"))
    monkeypatch.delenv("PHONEPILOT_MCP_TOKEN", raising=False)
    cfg = Config()
    cfg.mcp.port = _free_port()
    cfg.privacy.env_path = str(tmp_path / "none.env")
    url = f"http://127.0.0.1:{cfg.mcp.port}/mcp"
    env = dict(os.environ, PHONEPILOT_MCP_URL=url)
    params = StdioServerParameters(command=sys.executable, args=["-m", "phonepilot.agent.stdio_shim"], env=env)
    rt = CoreRuntime(cfg, demo=True)

    def start_app() -> None:
        rt.start()
        rt.call_sync(rt.connect(), timeout=30)
        rt.call_sync(rt.start_mcp(), timeout=30)

    async def run():
        async with Client(params) as c:
            before = await c.call_tool("observe", {})
            await asyncio.to_thread(start_app)
            await asyncio.sleep(0.5)
            after = await c.call_tool("observe", {})
            return json.loads(before.content[0].text), after

    try:
        before, after = asyncio.run(run())
        assert before["code"] == "APP_NOT_RUNNING"
        assert not after.is_error and json.loads(after.content[0].text)["package"] == "com.android.launcher3"
    finally:
        rt.call_sync(rt.shutdown(), timeout=30)
        rt.stop_loop()
