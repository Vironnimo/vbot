"""Agent-error recovery probes using production dispatch and disposable Skills."""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from core.skills import SkillRegistry
from core.skills.authoring import SkillAuthoringService
from core.tools import ToolContext, ToolRegistry, register_skill_manage_tool, tool_failure
from core.tools.skill import register_skill_tool
from scripts.provider_probe.choices import EDIT_CASES, SKILL_MANAGE_CASES
from scripts.provider_probe.scenario_agents import _skill_manage_scenario
from scripts.provider_probe.scenario_files import _edit_scenario


def skill_tolerance_cases() -> list[dict[str, Any]]:
    cases = []
    for name in SKILL_MANAGE_CASES:
        scenario = _skill_manage_scenario(name)
        cases.append({"id": name, "arguments": scenario.expected_arguments})
    base = {
        "action": "write_file",
        "name": "provider-probe",
        "file_path": "assets/placeholder.txt",
        "content": "",
    }
    cases.extend(
        [
            {"id": "spelling", "arguments": {**base, "action": " WRITE-FILE "}},
            {
                "id": "field_typo",
                "arguments": {k if k != "file_path" else "file_pth": v for k, v in base.items()},
            },
            {"id": "request_wrapper", "arguments": {"request": base}},
            {
                "id": "action_wrapper",
                "arguments": {"write_file": {k: v for k, v in base.items() if k != "action"}},
            },
            {
                "id": "operation_alias",
                "arguments": {k if k != "action" else "operation": v for k, v in base.items()},
            },
            {
                "id": "natural_patch",
                "task": (
                    "Lies references/notes.md im privaten Skill provider-probe und entferne nur "
                    "die veraltete Zeile. Der übrige Inhalt soll erhalten bleiben."
                ),
            },
            {
                "id": "natural_empty",
                "task": (
                    "Leere assets/placeholder.txt im privaten Skill provider-probe. "
                    "Die Datei soll bestehen bleiben und danach vollständig leer sein."
                ),
            },
        ]
    )
    return cases


async def _skill_case(
    adapter: Any, args: argparse.Namespace, case: dict[str, Any]
) -> dict[str, Any]:
    with TemporaryDirectory(prefix="vbot-tolerance-") as temporary:
        root = Path(temporary)
        skills_root = root / "skills"
        authoring = SkillAuthoringService()
        original = _skill_manage_scenario("create_own").expected_arguments
        assert original is not None
        if case["id"] != "create_own":
            authoring.create(skills_root, "provider-probe", original["content"], author="agent")
            for path, content in {
                "scripts/check.py": "value = 1\n",
                "references/notes.md": "Keep this step.\nobsolete line\n",
                "assets/placeholder.txt": "old content",
            }.items():
                authoring.write_file(skills_root, "provider-probe", path, content)
        registry = ToolRegistry()
        register_skill_manage_tool(registry, authoring, lambda _: skills_root, lambda _: None)
        register_skill_tool(registry, lambda *_: SkillRegistry.load(skills_root), lambda: None)
        context = ToolContext(
            agent_id="probe",
            session_id="probe",
            run_id="probe",
            tool_call_id="probe",
            tool_name="skill_manage",
            tool_call_index=0,
            workspace=root,
            vbot_root=root,
            data_root=root,
        )
        task = case.get("task") or (
            "Execute this requested Skill operation once; "
            "equivalent representations are acceptable: " + json.dumps(case["arguments"])
        )
        messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": (
                    "Complete the user's task using the available Tools. Only this "
                    "disposable private Skill is in scope. Report what actually happened. "
                    "Available Skill: provider-probe (Provider testing instructions)."
                ),
            },
            {"role": "user", "content": task},
        ]
        observed = []
        final = ""
        for _ in range(8):
            raw = await adapter.send(
                messages,
                model_id=args.model,
                tools=registry.provider_definitions(),
                thinking_effort=args.thinking_effort,
                max_tokens=args.max_tokens or 3000,
            )
            response = adapter.normalize_response(raw, model_id=args.model)
            messages.append(
                {
                    "role": "assistant",
                    **{
                        k: response[k]
                        for k in ("content", "tool_calls", "reasoning", "reasoning_meta")
                        if k in response
                    },
                }
            )
            calls = response.get("tool_calls") or []
            if not calls:
                final = str(response.get("content") or "")
                break
            for call in calls:
                try:
                    result = await registry.dispatch(
                        replace(context, tool_name=call["name"], tool_call_id=call["id"]),
                        call["arguments"],
                    )
                except (ValueError, KeyError) as error:
                    result = tool_failure("invalid_arguments", str(error))
                observed.append({"call": call, "result": result})
                messages.append(
                    {
                        "role": "tool",
                        "name": call["name"],
                        "tool_call_id": call["id"],
                        "content": json.dumps(result),
                    }
                )
        files = {
            p.relative_to(skills_root).as_posix(): p.read_text(encoding="utf-8")
            for p in skills_root.rglob("*")
            if p.is_file()
        }
        canonical = registry.get("skill_manage").contract
        writes = [item for item in observed if item["call"]["name"] == "skill_manage"]
        checks = {
            "finished": bool(final),
            "successful_mutation": bool(writes) and all(item["result"]["ok"] for item in writes),
        }
        if "arguments" in case:
            expected = canonical.normalize_arguments(case["arguments"])
            checks["call_intent"] = (
                len(writes) == 1
                and canonical.normalize_arguments(writes[0]["call"]["arguments"]) == expected
            )
            action = expected["action"]
            target = "provider-probe/" + expected.get("file_path", "SKILL.md")
            if action in {"create", "edit"}:
                checks["file_effect"] = expected["content"].split("---", 2)[
                    -1
                ].strip() in files.get(target, "")
            elif action == "write_file":
                checks["file_effect"] = files.get(target) == expected["content"]
            elif action == "patch":
                checks["file_effect"] = expected["match"] not in files.get(target, "") and expected[
                    "content"
                ] in files.get(target, "")
            elif action == "remove_file":
                checks["file_effect"] = target not in files
            else:
                checks["file_effect"] = not files
        elif case["id"] == "natural_patch":
            checks["file_effect"] = (
                files.get("provider-probe/references/notes.md") == "Keep this step.\n"
            )
        else:
            checks["file_effect"] = files.get("provider-probe/assets/placeholder.txt") == ""
        return {
            "case": case["id"],
            "passed": all(checks.values()),
            "checks": checks,
            "observed": observed,
            "final": final,
            "files": files,
        }


def edit_tolerance_cases() -> list[dict[str, Any]]:
    cases = [
        {"id": name, "arguments": _edit_scenario(name).expected_arguments} for name in EDIT_CASES
    ]
    item = {"path": "notes.txt", "old_string": "old", "new_string": "new"}
    cases.extend(
        [
            {"id": "flat", "arguments": item},
            {"id": "encoded", "arguments": {"request": {"edits": {**item, "replace-all": "yes"}}}},
            {
                "id": "partial",
                "arguments": {
                    "path": "notes.txt",
                    "edits": [
                        {"old_string": "old", "new_string": "wrong", "new-string": "conflict"},
                        {"old-string": "old", "new-string": "new"},
                    ],
                },
            },
            {"id": "natural", "task": "In notes.txt steht 'old'. Ersetze das durch 'new'."},
        ]
    )
    return cases


async def _edit_case(
    adapter: Any, args: argparse.Namespace, case: dict[str, Any]
) -> dict[str, Any]:
    from core.tools.edit import (
        _EDIT_BATCH_CONTRACT,
        _EDIT_ITEM_CONTRACT,
        register_edit_tool,
    )
    from core.tools.file_state import FileReadState

    class FixtureContext(ToolContext):
        def resolve_path(self, path: str | Path) -> Path:
            target = super().resolve_path(path)
            if not target.is_relative_to(self.workspace):
                raise ValueError("Only the disposable fixture directory is in scope")
            return target

    with TemporaryDirectory(prefix="vbot-edit-tolerance-") as temporary:
        root = Path(temporary)
        request = case.get(
            "arguments",
            {"edits": [{"path": "notes.txt", "old_string": "old", "new_string": "new"}]},
        )
        repaired = _EDIT_BATCH_CONTRACT.normalize_arguments(request)
        items = repaired.get("edits", [repaired])
        before: dict[str, str] = {}
        expected: dict[str, str] = {}
        for item in items:
            try:
                item = _EDIT_ITEM_CONTRACT.normalize_arguments(item)
            except ValueError:
                continue
            path = item.get("path", repaired.get("path"))
            if path not in before:
                before[path] = item["old_string"] * (2 if item.get("replace_all") else 1)
                expected[path] = before[path]
            expected[path] = expected[path].replace(
                item["old_string"], item["new_string"], -1 if item.get("replace_all") else 1
            )
        for path, content in before.items():
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        registry = ToolRegistry()
        register_edit_tool(registry, file_state=FileReadState())
        task = case.get("task") or (
            "Execute this diagnostic edit request once. Copy every supplied field. "
            "If both new_string and new-string appear, keep both fields and their "
            "different values exactly; neither may be omitted or replaced. "
            "Equivalent type representations are fine: " + json.dumps(request)
        )
        raw = await adapter.send(
            [
                {
                    "role": "system",
                    "content": "Use the available Tool to perform the task. "
                    "Only the disposable working directory is in scope.",
                },
                {"role": "user", "content": task},
            ],
            tools=registry.provider_definitions(),
            model_id=args.model,
            thinking_effort=args.thinking_effort,
            max_tokens=args.max_tokens or 3000,
        )
        response = adapter.normalize_response(raw, model_id=args.model)
        calls = response.get("tool_calls") or []
        results = []
        for call in calls:
            if call.get("name") != "edit":
                continue
            context = FixtureContext(
                agent_id="probe",
                session_id="probe",
                run_id="probe",
                tool_call_id=call["id"],
                tool_name="edit",
                tool_call_index=0,
                workspace=root,
                vbot_root=root,
                data_root=root,
            )
            results.append(await registry.dispatch(context, call["arguments"], ["edit"]))
        files = {
            p.relative_to(root).as_posix(): p.read_text(encoding="utf-8")
            for p in root.rglob("*")
            if p.is_file()
        }
        passed = len(calls) == len(results) == 1 and results[0]["ok"] and files == expected
        if case["id"] == "partial":
            passed = passed and results[0]["data"]["status"] == "partial"
        return {
            "case": case["id"],
            "passed": bool(passed),
            "observed": calls,
            "results": results,
            "files": files,
        }


async def _probe_tool_tolerance(adapter: Any, args: argparse.Namespace) -> dict[str, Any]:
    if args.tolerance_tool == "cron":
        from scripts.provider_probe.workflow_cron_tolerance import probe_cron_tolerance

        return await probe_cron_tolerance(adapter, args)
    if args.tolerance_tool in {
        "ha_call_service",
        "ha_get_state",
        "ha_list_entities",
        "ha_list_services",
    }:
        from scripts.provider_probe.workflow_ha_tolerance import ha_case, ha_tolerance_cases

        runner, cases = ha_case, ha_tolerance_cases(args.tolerance_tool)
    elif args.tolerance_tool == "web_fetch":
        from scripts.provider_probe.workflow_web_tolerance import web_case, web_tolerance_cases

        runner, cases = web_case, web_tolerance_cases()
    elif args.tolerance_tool == "channel_send":
        from scripts.provider_probe.workflow_channel_tolerance import (
            channel_case,
            channel_tolerance_cases,
        )

        runner, cases = channel_case, channel_tolerance_cases()
    elif args.tolerance_tool == "edit":
        runner, cases = _edit_case, edit_tolerance_cases()
    else:
        runner, cases = _skill_case, skill_tolerance_cases()
    rows = []
    semaphore = asyncio.Semaphore(
        1
        if args.tolerance_tool
        in {"web_fetch", "ha_call_service", "ha_get_state", "ha_list_entities", "ha_list_services"}
        else 3
    )

    async def run(case: dict[str, Any]) -> None:
        async with semaphore:
            try:
                async with asyncio.timeout(args.total_timeout):
                    row = await runner(adapter, args, case)
            except Exception as error:
                row = {"case": case["id"], "passed": False, "error": str(error)}
            rows.append(row)
            if args.tolerance_report:
                args.tolerance_report.write_text(
                    json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8"
                )

    await asyncio.gather(
        *(run(case) for case in cases if args.tolerance_case in {"all", case["id"]})
    )
    return {
        "scenario": "tool_tolerance",
        "passed": all(row["passed"] for row in rows),
        "cases": [{k: v for k, v in row.items() if k not in {"observed", "files"}} for row in rows],
    }
