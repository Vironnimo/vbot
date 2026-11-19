"""Antigravity's isolated child process for Tool inventory and pre-execution hooks.

The CLI runs this file by absolute path. The MCP endpoint only describes Tools.
The pre-execution hook records the CLI's proposed call and always refuses it.
Only the vBot server executes Tools.
"""

from __future__ import annotations

import asyncio
import json
import site
import sys
import time
from pathlib import Path
from typing import Any


async def _serve(inventory: Path) -> None:
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import CallToolResult, ListToolsResult, Tool

    tools = [
        Tool.model_validate({**tool, "_meta": {"antigravity/alwaysLoad": True}})
        for tool in json.loads(inventory.read_text("utf-8"))
    ]

    async def list_tools(_context: Any, _params: Any) -> ListToolsResult:
        inventory.with_name("listed").touch()
        return ListToolsResult(tools=tools)

    async def call_tool(_context: Any, _params: Any) -> CallToolResult:
        return CallToolResult(is_error=True, content=[])

    server = Server("vbot", on_list_tools=list_tools, on_call_tool=call_tool)
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def _guard(kind: str, state: Path) -> dict[str, str]:
    if kind in {"before", "before-tools"}:
        # One model invocation per canonical request, even if agy tries another
        # inference after it sees a denied Tool. Never return a Tool result to agy.
        try:
            if kind == "before-tools":
                # MCP registration runs concurrently with CLI startup. Wait inside
                # this native hook, before any inference can spend the user's quota.
                deadline = time.monotonic() + 10
                while not (state / "listed").is_file():
                    if time.monotonic() >= deadline:
                        return {"terminationBehavior": "terminate"}
                    time.sleep(0.05)
            (state / "invocation").touch(exist_ok=False)
        except OSError:
            return {"terminationBehavior": "terminate"}
        return {}
    if kind != "guard":
        raise ValueError("Unknown Antigravity hook")
    try:
        event = json.load(sys.stdin)
        step = event.get("stepIdx")
        if type(step) is int and step >= 0:
            # The CLI cannot report this hook's decision until the write completes.
            with (state / f"call-{step}.json").open("x", encoding="utf-8") as saved:
                json.dump(event.get("toolCall"), saved)
    except OSError, ValueError, AttributeError:
        pass
    return {"decision": "deny"}


if __name__ == "__main__":
    operation, directory, *package_dirs = sys.argv[1:]
    if operation == "mcp":
        # A development vBot can use a user-site MCP installation. Isolated
        # mode ignores the redirected HOME and any inherited Python paths.
        for path in package_dirs:
            site.addsitedir(path)
        asyncio.run(_serve(Path(directory) / "tools.json"))
    else:
        sys.stdout.write(json.dumps(_guard(operation, Path(directory))) + "\n")
