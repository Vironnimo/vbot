"""Provider Tool probe: workflow mcp."""

from __future__ import annotations

import argparse
import ast
import asyncio
import json
import logging
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any

from core.tools.contracts import ToolContractError


async def _probe_mcp_workflow(adapter: Any, args: argparse.Namespace) -> dict[str, Any]:
    """Let a real Model discover and drive an inert application through the real MCP host.

    The application records intent without evaluating generated code or touching Blender.
    Only structural outcomes are printed. The probe conversation stands in for the Session
    that keeps large results; the Extension state lives in a temporary directory.
    """
    from mcp.server import MCPServer

    from core.extensions.extensions import ExtensionAPI, ExtensionDeclarations
    from core.extensions.operations import ExtensionHost
    from core.tools import tool_failure
    from core.tools.availability import ToolAccess
    from core.tools.tools import ToolContext, ToolDefinitionProfileContext, ToolRegistry
    from core.utils.ids import new_id
    from resources.extensions.mcp.client import ConnectionRunner
    from resources.extensions.mcp.config import validate_connection
    from resources.extensions.mcp.extension import MCPService

    observed: list[str] = []
    rendered = False
    configured: list[dict[str, Any]] = []
    report_read = False
    server = MCPServer(
        "Blender workflow fixture",
        instructions=(
            "Inspect the current scene before changing it. This application is a test fixture."
        ),
    )

    @server.tool()
    def get_scene_info() -> dict[str, Any]:
        """Inspect the current scene and its camera, objects, and settings."""
        observed.append("scene")
        return {"camera": "Camera", "objects": ["Cube"], "engine": "BLENDER_EEVEE_NEXT"}

    @server.tool()
    def execute_blender_code(code: str) -> dict[str, Any]:
        """Execute Python code in Blender. Use small, focused steps."""
        nonlocal rendered
        observed.append("code")
        try:
            nodes = ast.walk(ast.parse(code))
            rendered = any(
                isinstance(node, ast.Call) and ast.unparse(node.func) == "bpy.ops.render.render"
                for node in nodes
            )
        except SyntaxError:
            rendered = False
        return {"render_completed": rendered, "path": "audit-render.png" if rendered else None}

    @server.tool()
    def get_render_report() -> str:
        """Read the complete report for the last render."""
        observed.append("report")
        return "scene diagnostic " * 600 + "\nFinal render status: completed. Reference: 739251."

    @server.tool()
    def configure_render(enabled: bool, count: int, labels: list[str]) -> dict[str, Any]:
        """Set whether rendering is enabled, its frame count, and output labels."""
        value = {"enabled": enabled, "count": count, "labels": labels}
        configured.append(value)
        return value

    with TemporaryDirectory(prefix="vbot-mcp-probe-") as directory:
        root = Path(directory)
        agent = SimpleNamespace(
            tool_access=ToolAccess(granted=("mcp_blender",)),
            memory_prompt_mode="off",
            workspace=directory,
        )

        async def sample(*_: Any) -> dict[str, Any]:
            raise ValueError("Sampling is not part of this workflow fixture")

        # Result payloads the MCP Extension attaches, kept for this one conversation.
        payloads: dict[str, Any] = {}

        def attach_payload(_call_id: str, _tool_name: str, payload: Any) -> str:
            payload_id = new_id("res")
            payloads[payload_id] = json.loads(json.dumps(payload))
            return payload_id

        async def load_payload(_context: Any, payload_id: str) -> Any:
            return payloads.get(payload_id)

        state_dir = root / "extension-data" / "mcp"
        state_dir.mkdir(parents=True)
        host = ExtensionHost(
            data_dir=root,
            sample=sample,
            resolve_agent=lambda *_: agent,
            resolve_tool_agent=lambda context: agent,
            store_attachment=lambda *_: None,
            resolve_credential=lambda _: "",
            set_credential=lambda *_: None,
            state_dir=state_dir,
            load_result_payload=load_payload,
        )
        api = ExtensionAPI(
            "mcp", ExtensionDeclarations(), config={}, logger=logging.getLogger("probe")
        )
        registry = ToolRegistry()
        api.operations.bind(registry)
        service = MCPService(api)
        await service.start(host)
        service.connections["blender"] = validate_connection(
            {"id": "blender", "transport": "stdio", "command": "unused"}
        )

        class FixtureRunner(ConnectionRunner):
            async def _transport(self, stack: Any) -> Any:
                return server

        runner = FixtureRunner(
            service.connections["blender"], host, service.inputs, service._publish
        )
        service.runners[runner.id] = runner
        service._publish(runner, {"tools": []})
        context = ToolContext(
            agent_id="probe",
            session_id="probe",
            run_id="probe",
            tool_call_id="probe",
            tool_name="mcp_blender",
            tool_call_index=0,
            workspace=root,
            vbot_root=root,
            data_root=root,
            result_payload_hook=attach_payload,
        )
        definitions = registry.provider_definitions(
            allowed_tools=["mcp_blender"],
            profile_context=ToolDefinitionProfileContext(agent_id="probe"),
        )
        prompt = (
            "Lies den Renderbericht und nenne mir den abschliessenden Status "
            "und die Referenznummer."
            if args.mcp_workflow_case == "large_result"
            else "Rendere die aktuelle Blender-Szene und speichere das Bild als audit-render.png."
        )
        if args.mcp_workflow_case.startswith("tolerance_"):
            enabled = "YES" if args.mcp_workflow_case == "tolerance_true" else "no"
            prompt = (
                "Find configure_render and execute this supplied request once: "
                + json.dumps({"enabled": enabled, "count": "3.0", "labels": "preview"})
                + ". This is an argument-recovery diagnostic: preserve the supplied spellings "
                "and types in the target's arguments. Report the actual configured state."
            )
        messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": (
                    "Complete the user's task using the available tools. "
                    "Report only verified results."
                ),
            },
            {"role": "user", "content": prompt},
        ]
        actions: list[str] = []
        final = ""
        invalid = 0

        async def dispatch(call: dict[str, Any]) -> None:
            nonlocal report_read, invalid
            inputs = call["arguments"]
            call_context = replace(context, tool_call_id=call["id"], tool_name=call["name"])
            actions.append(str(inputs.get("action")))
            try:
                result = await registry.dispatch(
                    call_context, inputs, service._allowed(call_context)
                )
            except (ValueError, ToolContractError) as error:
                # The Model sees the refusal Chat would return, including its corrected call.
                invalid += 1
                result = tool_failure("invalid_arguments", str(error))
            if inputs.get("action") == "read" and "739251" in json.dumps(result):
                report_read = True
            messages.append(
                {"role": "tool", "tool_call_id": call["id"], "content": json.dumps(result)}
            )

        try:
            if args.mcp_workflow_case == "no_match":
                seed = {
                    "id": "seed",
                    "name": "mcp_blender",
                    "arguments": {"action": "search", "query": "rendern", "kind": "tool"},
                }
                messages.append({"role": "assistant", "content": None, "tool_calls": [seed]})
                await dispatch(seed)
            async with asyncio.timeout(args.total_timeout):
                for _ in range(24):
                    raw = await adapter.send(
                        messages,
                        model_id=args.model,
                        tools=definitions,
                        thinking_effort=args.thinking_effort,
                        max_tokens=args.max_tokens or 2500,
                    )
                    response = adapter.normalize_response(raw, model_id=args.model)
                    calls = response.get("tool_calls") or []
                    messages.append(
                        {
                            key: response[key]
                            for key in ("content", "tool_calls", "reasoning", "reasoning_meta")
                            if key in response
                        }
                        | {"role": "assistant"}
                    )
                    if not calls:
                        final = response.get("content") or ""
                        break
                    for call in calls:
                        await dispatch(call)
        finally:
            await service.close()
        guidance_observed = (
            "scene" in observed
            and "code" in observed
            and observed.index("scene") < observed.index("code")
        )
        passed = (
            report_read and "739251" in final
            if args.mcp_workflow_case == "large_result"
            else rendered and guidance_observed and bool(final)
        )
        if args.mcp_workflow_case.startswith("tolerance_"):
            passed = bool(final) and configured == [
                {
                    "enabled": args.mcp_workflow_case == "tolerance_true",
                    "count": 3,
                    "labels": ["preview"],
                }
            ]
        return {
            "scenario": "mcp_workflow",
            "configured": configured,
            "case": args.mcp_workflow_case,
            "passed": passed,
            "actions": actions,
            "application_calls": observed,
            "guidance_observed": guidance_observed,
            "render_intent_verified": rendered,
            "complete_report_read": report_read,
            "invalid_calls": invalid,
            "model": args.model,
        }
