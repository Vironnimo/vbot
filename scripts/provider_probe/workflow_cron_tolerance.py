"""Cron Agent probes with production dispatch and disposable persisted jobs."""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import Mock

from core.automation.cron import CronService
from core.tools import ToolContext, ToolRegistry, tool_failure
from core.tools.cron import _normalize_cron_arguments, register_cron_tool
from scripts.provider_probe.choices import CRON_CASES
from scripts.provider_probe.scenario_automation import _cron_scenario


def cron_tolerance_cases() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = [
        {"id": name, "arguments": _cron_scenario(name).expected_arguments} for name in CRON_CASES
    ]
    base = {"action": "create", "prompt": "Check health", "schedule": "every 2h"}
    cases.extend(
        [
            {"id": "alias", "arguments": {**base, "agentId": "reviewer@vbot", "repeat": "3.0"}},
            {"id": "wrapper", "arguments": {"request": {**base, "operation": "CREATE"}}},
            {
                "id": "conflict",
                "arguments": {**base, "target": "first", "agent_id": "second"},
                "success": False,
            },
        ]
    )
    return cases


async def cron_case(adapter: Any, args: argparse.Namespace, case: dict[str, Any]) -> dict[str, Any]:
    with TemporaryDirectory(prefix="vbot-cron-tolerance-") as temporary:
        root = Path(temporary)
        service = CronService(Mock(), root, tz="UTC")  # Scheduler is never started.
        request = json.loads(json.dumps(case["arguments"]))
        if "id" in request:
            seed = service.create_job(
                agent_id="probe",
                prompt="Original",
                schedule_type="interval",
                interval_seconds=3600,
                remaining_runs=2,
                status="paused" if request["action"] == "enable" else "active",
            )
            request["id"] = seed.id
        if case["id"] == "create_once_iso":
            request["schedule"] = (datetime.now(UTC) + timedelta(days=10)).isoformat()
        before = [asdict(job) for job in service.list_jobs()]
        registry = ToolRegistry()
        register_cron_tool(registry, service)
        raw = await adapter.send(
            [
                {
                    "role": "system",
                    "content": "Use the available Tool for the supplied diagnostic request. "
                    "Copy all supplied fields; omit fields that were not supplied. "
                    "Keep target and agent_id as separate arguments when both appear; "
                    "do not move fields into prompt text or choose between targets. "
                    "Equivalent representations "
                    "are acceptable. Only this disposable job catalog is in scope.",
                },
                {
                    "role": "user",
                    "content": "Make one Tool Call with these arguments: "
                    + json.dumps(request)
                    + (
                        "\nInclude the literal agent_id key as well as target. Both keys are "
                        "intentional and supported by the runtime. Do not omit agent_id even "
                        "though it is absent from the preferred schema."
                        if case["id"] == "conflict"
                        else ""
                    ),
                },
            ],
            tools=registry.provider_definitions(),
            model_id=args.model,
            thinking_effort=args.thinking_effort,
            max_tokens=args.max_tokens or 2500,
        )
        response = adapter.normalize_response(raw, model_id=args.model)
        calls = response.get("tool_calls") or []
        results = []
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
                results.append(await registry.dispatch(context, call["arguments"], ["cron"]))
            except ValueError as error:
                results.append(tool_failure("invalid_arguments", str(error)))
        stored = [asdict(job) for job in CronService(Mock(), root, tz="UTC").list_jobs()]
        checks = {
            "one_call": len(calls) == len(results) == 1,
            "persisted": stored == [asdict(job) for job in service.list_jobs()],
        }
        if not case.get("success", True):
            checks["rejected_without_effect"] = (
                bool(results) and not results[0]["ok"] and stored == before
            )
        else:
            expected = _normalize_cron_arguments(request)
            checks["call_intent"] = (
                bool(calls) and _normalize_cron_arguments(calls[0]["arguments"]) == expected
            )
            checks["runtime_ok"] = bool(results) and results[0]["ok"]
            action = expected["action"]
            if action == "delete":
                checks["deleted"] = not stored
            elif action == "list":
                checks["listed"] = bool(results) and results[0].get("data", {}).get("jobs") == 0
            elif stored:
                job = stored[0]
                for field in ("name", "prompt"):
                    if field in expected:
                        checks[field] = job[field] == expected[field]
                if "repeat" in expected:
                    checks["repeat"] = job["remaining_runs"] == expected["repeat"]
                if "target" in expected:
                    agent, _, project = expected["target"].partition("@")
                    checks["target"] = job["agent_id"] == agent and job["project_id"] == (
                        project or None
                    )
                checks["status"] = job["status"] == ("paused" if action == "disable" else "active")
            else:
                checks["job_exists"] = False
        return {
            "case": case["id"],
            "passed": all(checks.values()),
            "checks": checks,
            "observed": calls,
            "results": results,
            "stored": stored,
        }


async def probe_cron_tolerance(adapter: Any, args: argparse.Namespace) -> dict[str, Any]:
    rows = []
    semaphore = asyncio.Semaphore(3)

    async def run(case: dict[str, Any]) -> None:
        async with semaphore:
            try:
                async with asyncio.timeout(args.total_timeout):
                    row = await cron_case(adapter, args, case)
            except Exception as error:
                row = {"case": case["id"], "passed": False, "error": str(error)}
            rows.append(row)
            if args.tolerance_report:
                args.tolerance_report.write_text(json.dumps(rows, indent=2), encoding="utf-8")

    await asyncio.gather(
        *(
            run(case)
            for case in cron_tolerance_cases()
            if args.tolerance_case in {"all", case["id"]}
        )
    )
    return {"passed": all(row["passed"] for row in rows), "cases": rows}
