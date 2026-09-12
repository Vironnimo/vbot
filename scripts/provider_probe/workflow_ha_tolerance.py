"""Home Assistant probes through real Extension registration and isolated HTTP."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import httpx
import respx

from core.extensions import ExtensionRegistry
from core.tools import ToolContext, ToolRegistry, tool_failure
from scripts.provider_probe.choices import HA_CALL_SERVICE_CASES
from scripts.provider_probe.scenario_extensions import _ha_call_service_scenario


def ha_tolerance_cases() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = [
        {"id": name, "arguments": _ha_call_service_scenario(name).expected_arguments}
        for name in HA_CALL_SERVICE_CASES
    ]
    base = {"domain": "light", "service": "turn_on"}
    cases.extend(
        [
            {
                "id": "case",
                "arguments": {
                    "domain": " LIGHT ",
                    "service": "TURN_ON",
                    "entity_id": "Light.Living_Room",
                },
            },
            {"id": "qualified", "arguments": {"service": "Light.Turn_On"}},
            {
                "id": "nested",
                "arguments": {
                    **base,
                    "data": {"entity_id": "Light.Living_Room", "brightness": 123},
                },
            },
            {
                "id": "duplicate",
                "arguments": {
                    **base,
                    "entity_id": "light.living_room",
                    "data": {"entity_id": "LIGHT.LIVING_ROOM"},
                },
            },
            {
                "id": "conflict",
                "arguments": {
                    **base,
                    "entity_id": "light.living_room",
                    "data": {"entity_id": "light.kitchen"},
                },
                "success": False,
            },
            {
                "id": "domain_conflict",
                "arguments": {"domain": "switch", "service": "light.turn_on"},
                "success": False,
            },
            {"id": "blocked", "arguments": {"service": "SHELL_COMMAND.RUN"}, "success": False},
            {
                "id": "traversal",
                "arguments": {**base, "data": {"entity_id": "light/../sensor"}},
                "success": False,
            },
        ]
    )
    return cases


async def ha_case(adapter: Any, args: argparse.Namespace, case: dict[str, Any]) -> dict[str, Any]:
    with TemporaryDirectory(prefix="vbot-ha-tolerance-") as temporary:
        root = Path(temporary)
        extensions = ExtensionRegistry.load(
            root / "extensions",
            bundled_dir=Path(__file__).resolve().parents[2] / "resources" / "extensions",
            credential_resolver=lambda key: "fixture-token" if key == "HASS_TOKEN" else "",
            config_provider=lambda _: {"url": "https://ha.fixture"},
        )
        registry = ToolRegistry()
        extensions.apply_tools(registry)
        name = "ha_call_service"
        raw = await adapter.send(
            [
                {
                    "role": "system",
                    "content": "Make one diagnostic Tool Call with all supplied "
                    "arguments. Preserve nested entity_id and contradictory values even if the "
                    "preferred schema shows another shape. The receiving service is isolated.",
                },
                {"role": "user", "content": "Arguments: " + json.dumps(case["arguments"])},
            ],
            tools=registry.provider_definitions([name]),
            model_id=args.model,
            thinking_effort=args.thinking_effort,
            max_tokens=args.max_tokens or 2500,
        )
        calls = adapter.normalize_response(raw, model_id=args.model).get("tool_calls") or []
        received = []

        def receive(request: httpx.Request) -> httpx.Response:
            received.append({"path": request.url.path, "body": json.loads(request.content)})
            return httpx.Response(200, json=[{"entity_id": "light.living_room", "state": "on"}])

        results = []
        with respx.mock(assert_all_called=False) as router:
            router.post(url__startswith="https://ha.fixture/").mock(side_effect=receive)
            for call in calls:
                context = ToolContext(
                    agent_id="probe",
                    session_id="probe",
                    run_id="probe",
                    tool_call_id=call["id"],
                    tool_name=call["name"],
                    tool_call_index=0,
                    workspace=root,
                    vbot_root=root,
                    data_root=root,
                )
                try:
                    results.append(await registry.dispatch(context, call["arguments"], [name]))
                except ValueError as error:
                    results.append(tool_failure("invalid_arguments", str(error)))
        checks = {"one_call": len(calls) == len(results) == 1}
        if case.get("success", True):
            request = case["arguments"]
            body = dict(request.get("data") or {})
            entity = request.get("entity_id", body.get("entity_id"))
            if entity:
                body["entity_id"] = entity.strip().lower()
            checks["receiver"] = received == [{"path": "/api/services/light/turn_on", "body": body}]
            checks["result"] = bool(results) and results[0].get("data", {}).get("result") == [
                {"entity_id": "light.living_room", "state": "on"}
            ]
        else:
            checks["rejected_without_effect"] = (
                bool(results) and not results[0]["ok"] and not received
            )
        return {
            "case": case["id"],
            "passed": all(checks.values()),
            "checks": checks,
            "observed": calls,
            "results": results,
            "received": received,
        }
