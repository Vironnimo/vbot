"""Channel probes with production preparation and a disposable delivery receiver."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import AsyncMock, Mock

from core.channels import ChannelConfig
from core.channels.adapter import RouteFacts
from core.database import write_bootstrap_marker
from core.sessions import ChatSessionManager
from core.tools import ToolContext, ToolRegistry, tool_failure
from core.tools.channel import _normalize_channel_send_arguments, register_channel_send_tool
from core.tools.contracts import compile_tool_contract
from core.tools.tools import ToolDefinitionProfileContext
from scripts.provider_probe.choices import CHANNEL_SEND_CASES
from scripts.provider_probe.scenario_agents import _channel_send_scenario


def channel_tolerance_cases() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = [
        {"id": name, "arguments": _channel_send_scenario(name).expected_arguments}
        for name in CHANNEL_SEND_CASES
    ]
    base = {"channel_id": "telegram-probe", "message": "Fixture message", "platform_target": 123}
    cases.extend(
        [
            {"id": "wrapper", "arguments": {"send": base}},
            {"id": "operation", "arguments": {"request": {"operation": "SEND", **base}}},
            {
                "id": "scalar_file",
                "arguments": {
                    "channel_id": "telegram-probe",
                    "filePaths": "artifacts/provider-tool-probe.txt",
                },
            },
            {"id": "unknown_action", "arguments": {**base, "action": "delete"}, "success": False},
            {
                "id": "conflict",
                "arguments": {**base, "platform-target": "different"},
                "success": False,
            },
        ]
    )
    return cases


async def channel_case(
    adapter: Any, args: argparse.Namespace, case: dict[str, Any]
) -> dict[str, Any]:
    class FixtureContext(ToolContext):
        def resolve_path(self, path: str | Path, *, follow_final_link: bool = True) -> Path:
            target = super().resolve_path(path, follow_final_link=follow_final_link)
            if not target.is_relative_to(self.workspace):
                raise ValueError("Only the disposable fixture directory is in scope")
            return target

    with TemporaryDirectory(prefix="vbot-channel-tolerance-") as temporary:
        root = Path(temporary).resolve()
        write_bootstrap_marker(root)
        sessions = ChatSessionManager(root)
        sessions.create("probe-agent", session_id="source")
        target = sessions.create("probe-agent", session_id="target")
        for name in ("provider-tool-probe.txt", "provider-tool-probe.png"):
            file = root / "artifacts" / name
            file.parent.mkdir(exist_ok=True)
            file.write_bytes(b"fixture content")
        platforms = (
            ("discord", "telegram")
            if case["id"].startswith("mixed_")
            else ("discord",)
            if case["id"].startswith("discord_")
            else ("telegram",)
        )
        configs = [
            ChannelConfig(
                id=f"{platform}-probe",
                platform=platform,
                agent_id="probe-agent",
                token_env_var="UNUSED",
                allowed_chat_ids=["default-target"],
            )
            for platform in platforms
        ]
        received = []

        async def receive(channel_id, message, platform_target, **options):
            received.append(
                {
                    "channel_id": channel_id,
                    "message": message,
                    "target": platform_target,
                    "thread_id": options.get("thread_id"),
                    "files": [
                        {"name": f.filename, "body": f.data.decode()}
                        for f in options.get("files") or []
                    ],
                    "buttons": [[asdict(b) for b in row] for row in options.get("buttons") or []],
                }
            )

        service = Mock()
        service.list_channels.return_value = configs
        service.send = AsyncMock(side_effect=receive)
        service.ensure_outbound_session = AsyncMock(
            return_value=RouteFacts(agent_id="probe-agent", session_id="target")
        )
        registry = ToolRegistry()
        register_channel_send_tool(registry, service, sessions, max_attachment_size_bytes=10000)
        definitions = registry.provider_definitions(
            profile_context=ToolDefinitionProfileContext(agent_id="probe-agent")
        )
        contract = compile_tool_contract(
            name="channel_send",
            input_schema=definitions[0]["parameters"],
            require_closed_input=False,
        )
        request = case["arguments"]
        try:
            raw = await adapter.send(
                [
                    {
                        "role": "system",
                        "content": "Make one Tool Call with the supplied diagnostic "
                        "arguments. Preserve all fields, including differently spelled "
                        "conflicting fields. "
                        "Equivalent types are acceptable. Delivery uses an isolated receiver.",
                    },
                    {"role": "user", "content": "Arguments: " + json.dumps(request)},
                ],
                tools=definitions,
                model_id=args.model,
                thinking_effort=args.thinking_effort,
                max_tokens=args.max_tokens or 2500,
            )
            calls = adapter.normalize_response(raw, model_id=args.model).get("tool_calls") or []
            results = []
            for call in calls:
                context = FixtureContext(
                    agent_id="probe-agent",
                    session_id="source",
                    run_id="probe",
                    tool_call_id=call["id"],
                    tool_name=call["name"],
                    tool_call_index=0,
                    workspace=root,
                    vbot_root=root,
                    data_root=root,
                    input_contract=contract,
                )
                try:
                    results.append(
                        await registry.dispatch(context, call["arguments"], ["channel_send"])
                    )
                except ValueError as error:
                    results.append(tool_failure("invalid_arguments", str(error)))
            checks = {"one_call": len(calls) == len(results) == 1}
            if not case.get("success", True):
                checks["no_delivery"] = bool(results) and not results[0]["ok"] and not received
            else:
                expected = _normalize_channel_send_arguments(request)
                checks["delivered"] = bool(results) and results[0]["ok"] and len(received) == 1
                if received:
                    delivery = received[0]
                    checks["destination"] = delivery["channel_id"] == expected[
                        "channel_id"
                    ] and delivery["target"] == expected.get("platform_target", "default-target")
                    checks["message"] = delivery["message"] == expected.get("message")
                    checks["thread"] = delivery["thread_id"] == expected.get("thread_id")
                    checks["files"] = delivery["files"] == [
                        {"name": Path(p).name, "body": "fixture content"}
                        for p in expected.get("file_paths", [])
                    ]
                    checks["buttons"] = delivery["buttons"] == expected.get("buttons", [])
                    checks["saved_note"] = any(
                        m.role == "note" or "channel_send" in str(m.content) for m in target.load()
                    )
            return {
                "case": case["id"],
                "passed": all(checks.values()),
                "checks": checks,
                "observed": calls,
                "results": results,
                "received": received,
            }
        finally:
            sessions.close()
