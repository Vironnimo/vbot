"""Provider Tool probe: workflow reflection."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from scripts.provider_probe.common import PROJECT_ROOT


def _reflection_cases() -> list[dict[str, Any]]:
    path = PROJECT_ROOT / "tests/fixtures/reflection/cases.json"
    cases: list[dict[str, Any]] = json.loads(path.read_text(encoding="utf-8"))
    return cases


async def _probe_reflection_case(
    adapter: Any, args: argparse.Namespace, case: dict[str, Any], scope: str
) -> dict[str, Any]:
    """Evaluate decisions using production prose and real disposable Tool effects.

    Only synthetic history and production context reach the Model; expectations
    stay in the observer. Chat fork/cadence integration is tested separately.
    """
    from core.automation.reflection import REFLECT_FRAGMENT_NAMES, REFLECTION_TOOL_RESTRICTIONS
    from core.memory.memory import MemoryService, memory_block_definition
    from core.prompts.prompts import _format_skill_catalog
    from core.skills import SkillAuthoringService, SkillRegistry
    from core.tools.memory import register_memory_tool
    from core.tools.skill import register_skill_tool
    from core.tools.skill_manage import register_skill_manage_tool
    from core.tools.tools import ToolContext, ToolRegistry, tool_failure

    resources = PROJECT_ROOT / "resources/prompts"
    names = ("memory", "skill", "skill_manage")
    allowed = names if scope == "learn" else REFLECTION_TOOL_RESTRICTIONS[scope]  # type: ignore[index]
    with TemporaryDirectory(prefix="vbot-reflection-probe-") as temporary:
        root = Path(temporary)
        own, bundled = root / "skills", root / "bundled"
        memory = MemoryService()
        for memory_scope, entries in case.get("memory", {}).items():
            for entry in entries:
                memory.add_entry(root, memory_scope, entry)
        for skill in case.get("skills", []):
            home = bundled if skill.get("readonly") else own
            path = home / skill["name"] / "SKILL.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(skill["content"], encoding="utf-8")

        def skills() -> SkillRegistry:
            return SkillRegistry.load(own, extra_dirs=[bundled], origins=["agent", "bundled"])

        registry = ToolRegistry()
        register_memory_tool(registry, memory)
        register_skill_tool(registry, lambda *_args: skills(), lambda: None)
        register_skill_manage_tool(
            registry,
            SkillAuthoringService(protected_roots=[bundled]),
            lambda _agent: own,
            lambda _agent: None,
            resolve_external_skill_scope=lambda _agent, name, _project: (
                "bundled" if (bundled / name / "SKILL.md").is_file() else None
            ),
        )
        definitions = registry.provider_definitions(names)
        if {tool["name"] for tool in definitions} != set(names):
            raise RuntimeError("Reflection probe requires all three production Tool definitions")
        catalog = _format_skill_catalog(skills().filter_allowed(["*"]))
        memory_text = memory.read_prompt_files(root, "agent_user")
        if case.get("stale_memory_prompt"):
            memory_text = "# Agent Memory\nNo entries yet.\n# User Profile\nNo entries yet."
        system = "\n\n".join(
            [
                (memory_block_definition().default_text or "").replace(
                    "{generated:memory_files}", memory_text
                ),
                (resources / "skills.md")
                .read_text(encoding="utf-8")
                .replace("{generated:skill_catalog}", catalog),
                (resources / "skill_maintenance.md").read_text(encoding="utf-8"),
            ]
        )
        fragment = "learn.md" if scope == "learn" else REFLECT_FRAGMENT_NAMES[scope]  # type: ignore[index]
        brief = (resources / fragment).read_text(encoding="utf-8").strip()
        if scope == "learn":
            brief += "\n\nThe request to learn from:\n" + case["learn_request"]
        messages = [
            {"role": "system", "content": system},
            *case["history"],
            {"role": "user", "content": "<system-reminder>\n" + brief + "\n</system-reminder>"},
        ]

        def snapshot() -> dict[str, Any]:
            return {
                "memory": {
                    scope_name: [entry.content for entry in memory.list_entries(root, scope_name)]
                    for scope_name in ("user", "agent")
                },
                "files": {
                    path.relative_to(root).as_posix(): path.read_text(encoding="utf-8")
                    for home in (own, bundled)
                    for path in home.rglob("*")
                    if path.is_file()
                },
            }

        before = snapshot()
        reads: set[tuple[str, str]] = set()
        actions: list[dict[str, Any]] = []
        violations: list[str] = []
        finished = False
        steps = 0
        for step in range(12):
            steps = step + 1
            raw = await adapter.send(
                messages,
                model_id=args.model,
                tools=definitions,
                thinking_effort=args.thinking_effort,
                max_tokens=args.max_tokens or 4000,
            )
            response = adapter.normalize_response(raw, model_id=args.model)
            calls = response.get("tool_calls") or []
            messages.append(
                {
                    "role": "assistant",
                    **{
                        key: response[key]
                        for key in ("content", "tool_calls", "reasoning", "reasoning_meta")
                        if key in response
                    },
                }
            )
            if not calls:
                finished = bool(str(response.get("content") or "").strip())
                break
            for index, call in enumerate(calls):
                name, arguments = call["name"], call["arguments"]
                action = arguments.get("action", "")
                target = (
                    arguments.get("scope", "") if name == "memory" else arguments.get("name", "")
                )
                file_path = arguments.get("file_path", "SKILL.md")
                mutation = (name == "memory" and action != "list") or name == "skill_manage"
                if mutation:
                    if name == "memory" and ("memory", target) not in reads:
                        violations.append("memory_write_without_current_list")
                    if name == "skill_manage":
                        if action == "create" and ("skill", "catalog") not in reads:
                            violations.append("create_without_current_catalog")
                        if (own / target / file_path).is_file() and (
                            target,
                            file_path,
                        ) not in reads:
                            violations.append("skill_write_without_current_file")
                context = ToolContext(
                    agent_id="probe",
                    session_id="probe-session",
                    run_id="probe-run",
                    tool_call_id=call["id"],
                    tool_name=name,
                    tool_call_index=index,
                    workspace=root,
                    vbot_root=root,
                    data_root=root,
                    cwd=root,
                )
                try:
                    result = await registry.dispatch(context, arguments, allowed)
                except Exception as error:
                    result = tool_failure("probe_dispatch_rejected", type(error).__name__)
                if not result["ok"]:
                    violations.append("tool_call_rejected")
                elif name == "memory" and action == "list":
                    reads.add(("memory", target))
                elif name == "skill" and not arguments:
                    reads.add(("skill", "catalog"))
                elif name == "skill" and arguments.get("file_path"):
                    reads.add((target, file_path))
                if mutation:
                    actions.append({"tool": name, "action": action, "ok": result["ok"]})
                messages.append(
                    {
                        "role": "tool",
                        "name": name,
                        "tool_call_id": call["id"],
                        "content": json.dumps(result),
                    }
                )
        after = snapshot()
        expected = case["expected"][scope]
        kind = expected["kind"]
        changed_files = {
            path
            for path in before["files"].keys() | after["files"].keys()
            if before["files"].get(path) != after["files"].get(path)
        }
        if kind == "none":
            effect_ok = before == after and not actions
            payload = ""
        elif kind in ("user", "agent"):
            other = "agent" if kind == "user" else "user"
            payload = "\n".join(after["memory"][kind])
            effect_ok = (
                not changed_files
                and before["memory"][other] == after["memory"][other]
                and len(after["memory"][kind]) == expected.get("count", 1)
                and before["memory"][kind] != after["memory"][kind]
            )
        else:
            payload = "\n".join(after["files"].get(path, "") for path in changed_files)
            created = [
                path
                for path in changed_files
                if path.endswith("/SKILL.md") and path not in before["files"]
            ]
            effect_ok = (
                bool(changed_files)
                and before["memory"] == after["memory"]
                and all(path.startswith("skills/") for path in changed_files)
            )
            if kind == "create":
                effect_ok = effect_ok and len(created) == 1
            else:
                effect_ok = effect_ok and changed_files == {
                    "skills/" + expected["name"] + "/SKILL.md"
                }
        evidence_ok = all(
            token.lower() in payload.lower() for token in expected.get("contains", [])
        ) and all(token.lower() not in payload.lower() for token in expected.get("excludes", []))
        return {
            "case": case["id"],
            "scope": scope,
            "steps": steps,
            "actions": actions,
            "violations": violations,
            "final_response_received": finished,
            "effect_ok": effect_ok,
            "evidence_ok": evidence_ok,
            "passed": finished and effect_ok and evidence_ok and not violations,
        }


async def _probe_reflection_workflow(adapter: Any, args: argparse.Namespace) -> dict[str, Any]:
    selected = [
        (case, scope)
        for case in _reflection_cases()
        for scope in case["expected"]
        if args.reflection_case in ("all", case["id"]) and args.reflection_scope in ("all", scope)
    ]
    if not selected:
        raise ValueError("No matching reflection scenario")
    slots = asyncio.Semaphore(3)

    async def evaluate(case: dict[str, Any], scope: str) -> dict[str, Any]:
        async with slots:
            try:
                async with asyncio.timeout(args.total_timeout):
                    return await _probe_reflection_case(adapter, args, case, scope)
            except Exception as error:  # noqa: BLE001 - retain other independent case results
                return {
                    "case": case["id"],
                    "scope": scope,
                    "passed": False,
                    "error_type": type(error).__name__,
                }

    rows = await asyncio.gather(*(evaluate(case, scope) for case, scope in selected))
    return {
        "scenario": "reflection_workflow",
        "model": args.model,
        "cases": rows,
        "passed": all(row["passed"] for row in rows),
    }
