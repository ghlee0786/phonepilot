"""phonepilot-mcp: stdio <-> Streamable HTTP relay for stdio-only MCP clients such as Claude Desktop (FR-MCP-004).

It holds no tool logic: tools/list and tools/call are forwarded to the running PhonePilot app.
If the app is not running it answers with APP_NOT_RUNNING instead of starting a headless core (FR-AGT-012),
and it reconnects on the next call once the app is up.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import httpx2
import mcp_types as types
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.server.lowlevel.server import Server
from mcp.server.stdio import stdio_server

from phonepilot.paths import localdata_dir


def _token() -> str | None:
    path = localdata_dir() / "mcp.token"
    return path.read_text(encoding="ascii").strip() if path.exists() else None


def _not_running() -> types.CallToolResult:
    data = {"code": "APP_NOT_RUNNING", "message": "PhonePilot app is not running", "hint": "PhonePilot 앱을 실행하세요", "retryable": True}
    return types.CallToolResult(content=[types.TextContent(type="text", text=json.dumps(data, ensure_ascii=False))], structured_content=data, is_error=True)


class _Upstream:
    """Owns the HTTP client in one long-lived task (anyio scopes must exit in the task that entered them).

    Connects lazily and reconnects after a failure, re-reading the token file each time: the app writes a new
    token on every start, so a Claude session opened before the app, or kept across an app restart, still works.
    """

    def __init__(self, url: str) -> None:
        self.url = url
        self.client: Client | None = None
        self._want = asyncio.Event()
        self._ready = asyncio.Event()
        self._drop = asyncio.Event()
        self._lock = asyncio.Lock()

    async def run(self) -> None:
        while True:
            await self._want.wait()
            self._want.clear()
            token = os.environ.get("PHONEPILOT_MCP_TOKEN") or _token()
            try:
                async with httpx2.AsyncClient(
                    headers={"Authorization": f"Bearer {token or ''}", "X-PhonePilot-Client": "stdio-shim"},
                    timeout=httpx2.Timeout(connect=5.0, read=900.0, write=30.0, pool=30.0),
                ) as http, Client(streamable_http_client(self.url, http_client=http)) as client:
                    self.client = client
                    self._ready.set()
                    await self._drop.wait()
            except Exception:  # noqa: BLE001 - app not running, token mismatch or connection lost
                pass
            finally:
                self.client = None
                self._drop.clear()
                self._ready.set()

    async def get(self, timeout: float = 8.0) -> Client | None:
        async with self._lock:
            if self.client is not None:
                return self.client
            self._ready.clear()
            self._want.set()
            try:
                await asyncio.wait_for(self._ready.wait(), timeout)
            except TimeoutError:
                return None
            return self.client

    def drop(self) -> None:
        self._drop.set()


async def amain(url: str | None = None) -> None:
    upstream = _Upstream(url or os.environ.get("PHONEPILOT_MCP_URL", "http://127.0.0.1:47800/mcp"))
    owner = asyncio.create_task(upstream.run())

    async def on_list_tools(_ctx: Any, _params: Any) -> types.ListToolsResult:
        client = await upstream.get()
        if client is None:
            return types.ListToolsResult(tools=[])
        try:
            return await client.list_tools()
        except Exception:  # noqa: BLE001
            upstream.drop()
            return types.ListToolsResult(tools=[])

    async def on_call_tool(_ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        client = await upstream.get()
        if client is None:
            return _not_running()
        try:
            return await client.call_tool(params.name, params.arguments or {})
        except Exception:  # noqa: BLE001 - never retried here: the action may already have run on the phone
            upstream.drop()
            return _not_running()

    server = Server("phonepilot", version="0.1.0", on_list_tools=on_list_tools, on_call_tool=on_call_tool)
    try:
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())
    finally:
        upstream.drop()
        owner.cancel()


def main() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    main()
