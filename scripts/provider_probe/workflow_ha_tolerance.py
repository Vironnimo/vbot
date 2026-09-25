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


def ha_tolerance_cases(tool: str = "ha_call_service") -> list[dict[str, Any]]:
    if tool == "ha_list_services":
        return [
            {"id": "default", "arguments": {}},
            {"id": "domain", "arguments": {"domain": "climate"}},
            {"id": "case", "arguments": {"domain": " CLIMATE "}},
            {"id": "invalid", "arguments": {"domain": "light/../sensor"}, "success": False},
        ]
    if tool == "ha_list_entities":
        return [
            {"id": "default", "arguments": {}},
            {"id": "domain", "arguments": {"domain": "light"}},
            {"id": "area", "arguments": {"area": "Living Room"}},
            {"id": "all", "arguments": {"domain": "climate", "area": "Upstairs"}},
            {"id": "case", "arguments": {"domain": " LIGHT "}},
            {"id": "invalid", "arguments": {"domain": "light/../sensor"}, "success": False},
        ]
    if tool == "ha_get_state":
        return [
            {"id": "light", "arguments": {"entity_id": "light.living_room"}},
            {"id": "sensor", "arguments": {"entity_id": "sensor.outdoor_temperature"}},
            {"id": "case", "arguments": {"entityId": " LIGHT.Living_Room "}},
            {"id": "traversal", "arguments": {"entity_id": "light/../sensor"}, "success": False},
        ]
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
        name = getattr(args, "tolerance_tool", "ha_call_service")
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

        entities: list[dict[str, Any]] = [
            {
                "entity_id": "light.living_room",
                "state": "on",
                "attributes": {"friendly_name": "Living Room Light"},
            },
            {
                "entity_id": "climate.upstairs",
                "state": "heat",
                "attributes": {"friendly_name": "Upstairs Heating"},
            },
        ]

        services: list[dict[str, Any]] = [
            {"domain": "light", "services": {"turn_on": {"description": "Turn on", "fields": {}}}},
            {
                "domain": "climate",
                "services": {"set_temperature": {"description": "Set temperature", "fields": {}}},
            },
        ]

        def receive(request: httpx.Request) -> httpx.Response:
            received.append(
                {
                    "path": request.url.path,
                    "body": json.loads(request.content) if request.content else None,
                }
            )
            if name == "ha_list_services":
                return httpx.Response(200, json=services)
            if name == "ha_list_entities":
                return httpx.Response(200, json=entities)
            if name == "ha_get_state":
                return httpx.Response(
                    200,
                    json={
                        "entity_id": request.url.path.rsplit("/", 1)[1],
                        "state": "on",
                        "attributes": {},
                        "last_changed": "fixture",
                        "last_updated": "fixture",
                    },
                )
            return httpx.Response(200, json=[{"entity_id": "light.living_room", "state": "on"}])

        results = []
        with respx.mock(assert_all_called=False) as router:
            router.route(url__startswith="https://ha.fixture/").mock(side_effect=receive)
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
        if name == "ha_list_services" and case.get("success", True):
            domain = case["arguments"].get("domain", "").strip().lower()
            expected_services = [s for s in services if not domain or s["domain"] == domain]
            checks["receiver"] = received == [{"path": "/api/services", "body": None}]
            lines = (
                (results[0].get("data") or {}).get("content", "").splitlines() if results else []
            )
            checks["result"] = [line.split(":")[0] for line in lines] == (
                [next(iter(s["services"])) for s in expected_services]
                if domain
                else sorted(s["domain"] for s in expected_services)
            )
        elif name == "ha_list_entities" and case.get("success", True):
            request = case["arguments"]
            expected = [
                {
                    "entity_id": e["entity_id"],
                    "state": e["state"],
                    "friendly_name": e["attributes"]["friendly_name"],
                }
                for e in entities
                if (
                    not request.get("domain")
                    or e["entity_id"].startswith(request["domain"].strip().lower() + ".")
                )
                and request.get("area", "").lower() in e["attributes"]["friendly_name"].lower()
            ]
            checks["receiver"] = received == [{"path": "/api/states", "body": None}]
            content = (results[0].get("data") or {}).get("content", "") if results else ""
            checks["result"] = content.splitlines() == [
                f"{e['entity_id']}: {e['state']} ({e['friendly_name']})"
                for e in sorted(expected, key=lambda item: item["entity_id"])
            ]
        elif name == "ha_get_state" and case.get("success", True):
            request = case["arguments"]
            entity = request.get("entity_id", request.get("entityId")).strip().lower()
            checks["receiver"] = received == [{"path": "/api/states/" + entity, "body": None}]
            checks["result"] = (
                bool(results) and results[0].get("data", {}).get("entity_id") == entity
            )
        elif case.get("success", True):
            request = case["arguments"]
            body = dict(request.get("data") or {})
            entity = request.get("entity_id", body.get("entity_id"))
            if entity:
                body["entity_id"] = entity.strip().lower()
            checks["receiver"] = received == [{"path": "/api/services/light/turn_on", "body": body}]
            checks["result"] = bool(results) and results[0].get("data") == {
                "service": "light.turn_on",
                "changed": 1,
                "content": "light.living_room: on",
            }
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
