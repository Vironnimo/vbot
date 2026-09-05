"""A Tool installed during a Run is usable in its next Provider cycle."""

from typing import Any

import pytest

from core.extensions.operations import ExtensionOperations
from core.tools import ToolRegistry, tool_success
from tests.core.chat.chat_loop_support import StubAdapter, StubAgent, StubRuntime, build_chat_loop


@pytest.mark.asyncio
async def test_live_catalog_publication_refreshes_next_provider_cycle(tmp_path):
    tools = ToolRegistry()
    operations = ExtensionOperations("test")
    operations.bind(tools)
    calls = []

    async def installed(context, arguments):
        calls.append("installed-called")
        return tool_success({"sentinel": True})

    async def install(context, arguments):
        operations.replace_tools(
            "connection",
            [
                {
                    "name": "installed",
                    "description": "test-sentinel",
                    "parameters": {
                        "type": "object",
                        "properties": {},
                        "additionalProperties": False,
                    },
                    "handler": installed,
                }
            ],
        )
        return tool_success({"installed": True})

    tools.register("install", "test-sentinel", {"type": "object"}, install)
    adapter = StubAdapter(
        [
            {
                "content": None,
                "tool_calls": [{"id": "install", "name": "install", "arguments": {}}],
            },
            {"content": None, "tool_calls": [{"id": "use", "name": "installed", "arguments": {}}]},
            {"content": "finished"},
        ]
    )
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter, tools=tools)
    runtime.chat_sessions.create("coder", session_id="session-one")

    run = await build_chat_loop(runtime).start_run(
        "coder", "install and use", session_id="session-one"
    )
    await run.wait()

    assert calls == ["installed-called"]
    assert "installed" not in {tool["name"] for tool in adapter.requests[0]["kwargs"]["tools"]}
    assert "installed" in {tool["name"] for tool in adapter.requests[1]["kwargs"]["tools"]}
