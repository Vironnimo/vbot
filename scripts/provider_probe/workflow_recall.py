"""First-use Recall evaluation with production Tools and disposable SQLite effects."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from core.prompts.prompts import _format_skill_catalog
from core.providers.tool_schema import render_tool_definitions
from core.recall import RecallBackendContext, RecallBackendRegistry
from core.skills import SkillRegistry
from core.tools.bash import register_bash_tool
from core.tools.contracts import ToolContractError
from core.tools.process_manager import ProcessManager
from core.tools.session_search import (
    _normalize_search_arguments,
    _parse_period,
    register_session_search_tool,
)
from core.tools.skill import register_skill_tool
from core.tools.tools import ToolContext, ToolRegistry, tool_failure
from scripts.provider_probe.common import PROJECT_ROOT
from scripts.provider_probe.recall_cases import (
    FixtureEmbeddings,
    recall_cases,
    recall_matrix,
    seed_sessions,
)


async def _evaluate_case(
    adapter: Any, args: argparse.Namespace, case: dict[str, Any]
) -> dict[str, Any]:
    with TemporaryDirectory(prefix="vbot-recall-workflow-") as temporary:
        root = Path(temporary)
        data_root = root / "data"
        sessions = await seed_sessions(data_root)
        manager = ProcessManager()
        registry = ToolRegistry()
        skills = SkillRegistry.load(PROJECT_ROOT / "resources/skills")
        backend = RecallBackendRegistry.with_builtins().create(
            case.get("backend", "sqlite_fts"),
            RecallBackendContext(
                data_root,
                sessions,
                embeddings=FixtureEmbeddings() if case.get("embeddings") else None,
            ),
        )
        register_session_search_tool(registry, backend, sessions)
        if "exact_arguments" not in case:
            register_skill_tool(registry, lambda *_: skills, lambda: None)
        if case.get("bash", True) and "exact_arguments" not in case:
            register_bash_tool(registry, manager)
        context = ToolContext(
            agent_id="coder",
            session_id="current-session",
            run_id="probe",
            tool_call_id="probe",
            tool_name="session_search",
            tool_call_index=0,
            workspace=root,
            data_root=data_root,
            vbot_root=PROJECT_ROOT,
            allowed_skills=["vbot-cli"],
        )
        definitions = registry.provider_definitions()
        wire = render_tool_definitions(definitions, profile="explicit_non_strict")
        assert all(tool.get("strict") is False for tool in wire)
        skill_prompt = (
            (PROJECT_ROOT / "resources/prompts/skills.md")
            .read_text(encoding="utf-8")
            .replace(
                "{generated:skill_catalog}",
                _format_skill_catalog(skills.filter_allowed(["vbot-cli"])),
            )
        )
        if "exact_arguments" in case:
            skill_prompt = ""
        system = (
            "Complete the user's task using the available Tools. Report verified findings. "
            "Current Agent: coder. Current Session: current-session. No Project. "
            "The current conversation contains no earlier decision about Rotation. "
            "Only the provided disposable local data is in scope. No network "
            "access, server startup, "
            "installation or configuration changes. Read the database without changing it; "
            "temporary scripts and requested output files may be written in the working directory. "
            f"Working directory: {root.as_posix()}. Local vBot data_dir: {data_root.as_posix()}. "
            "This target has no running CLI server. Its data directory is already known.\n\n"
            + skill_prompt
        )
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": case["task"]},
        ]
        actions: list[dict[str, Any]] = []
        observed: list[dict[str, Any]] = []
        result_bytes = 0
        initial_hash = hashlib.sha256((data_root / "sessions.db").read_bytes()).hexdigest()
        final = ""
        artifacts: dict[str, Any] = {}

        async def dispatch(call: dict[str, Any], *, seeded: bool = False) -> None:
            nonlocal result_bytes
            current = replace(context, tool_name=call["name"], tool_call_id=call["id"])
            try:
                result = await registry.dispatch(current, call["arguments"])
            except (ToolContractError, KeyError) as error:
                result = tool_failure("invalid_arguments", str(error))
            serialized = json.dumps(result, ensure_ascii=False)
            result_bytes += len(serialized.encode("utf-8"))
            actions.append(
                {
                    "tool": call["name"],
                    "arguments": call["arguments"],
                    "ok": result["ok"],
                    "seeded": seeded,
                }
            )
            observed.append({"call": call, "result": result, "seeded": seeded})
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "name": call["name"],
                    "content": serialized,
                }
            )

        try:
            if "seed" in case:
                call = {"id": "seed", "name": "session_search", "arguments": case["seed"]}
                messages.append({"role": "assistant", "content": None, "tool_calls": [call]})
                await dispatch(call, seeded=True)
            for _ in range(14):
                raw = await adapter.send(
                    messages,
                    model_id=args.model,
                    tools=definitions,
                    thinking_effort=args.thinking_effort,
                    max_tokens=args.max_tokens or 5000,
                )
                response = adapter.normalize_response(raw, model_id=args.model)
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
                calls = response.get("tool_calls") or []
                if not calls:
                    final = str(response.get("content") or "")
                    break
                for call in calls:
                    await dispatch(call)
            fresh = [action for action in actions if not action["seeded"]]
            searches = [action for action in fresh if action["tool"] == "session_search"]
            extended = any(
                action["tool"] == "skill"
                and action["arguments"].get("file_path") == "references/session-search.md"
                and action["ok"]
                for action in fresh
            )
            executed = any(action["tool"] == "bash" and action["ok"] for action in fresh)
            checks = {
                "finished": bool(final),
                "evidence": all(
                    token.casefold() in final.casefold() for token in case.get("contains", [])
                ),
                "database_unchanged": initial_hash
                == hashlib.sha256((data_root / "sessions.db").read_bytes()).hexdigest(),
            }
            returned_text = []
            for record in observed:
                if record["call"]["name"] not in {"session_search", "bash"}:
                    continue
                data = record["result"].get("data") or {}
                returned_text.append(str(data.get("output", "")))
                for item in data.get("items", []):
                    returned_text.append(item["excerpt"]["text"])
                    returned_text.extend(part["text"] for part in item.get("context", []))
            checks["retrieved_evidence"] = all(
                token.casefold() in "\n".join(returned_text).casefold()
                for token in case.get("contains", [])
            )
            if case.get("extended"):
                checks["extended_read"] = extended and executed
            if case.get("arguments"):
                checks["scope"] = any(
                    all(
                        action["arguments"].get(key) == value
                        for key, value in case["arguments"].items()
                    )
                    for action in searches
                )
            if case.get("period"):
                checks["period"] = any(
                    action["arguments"].get("period") == "2026-07-01/2026-07-31"
                    for action in searches
                )
            if case.get("no_more_search"):
                checks["no_invalid_retry"] = not searches
            if case.get("transcript"):
                path = root / "transcript.jsonl"
                try:
                    rows = [
                        json.loads(line)
                        for line in path.read_text(encoding="utf-8-sig").splitlines()
                        if line.strip()
                    ]
                except (OSError, ValueError):
                    rows = []
                checks["transcript"] = [row.get("role") for row in rows] == [
                    "user",
                    "assistant",
                    "user",
                    "assistant",
                ] and all(
                    text in json.dumps(row, ensure_ascii=False)
                    for row, text in zip(
                        rows,
                        [
                            "Erste Frage: Welche Farbe?",
                            "Blau.",
                            "Zweite Frage: Welcher Farbton?",
                            "Kobaltblau, Farbcode #0047AB.",
                        ],
                        strict=False,
                    )
                )
                artifacts["transcript.jsonl"] = rows
            if case.get("unavailable"):
                quote = "Freigabe erst nach bestandenem Restore-Test."
                checks["no_invented_quote"] = (
                    quote not in final
                    or any(
                        quote in json.dumps(item["result"], ensure_ascii=False) for item in observed
                    )
                ) and not any(action["tool"] == "bash" for action in fresh)
                checks["ending_or_limitation"] = quote in final or "nicht" in final.casefold()
            if "exact_arguments" in case:

                def normalize(value: dict[str, Any]) -> dict[str, Any]:
                    value = _normalize_search_arguments(
                        registry.get("session_search").contract.normalize_arguments(value)
                    )
                    value.setdefault("agent_id", "coder")
                    value.setdefault("include_subagents", False)
                    value["period"] = _parse_period(value.get("period"))
                    return value

                checks["call_intent"] = (
                    len(searches) == 1
                    and normalize(searches[0]["arguments"]) == normalize(case["exact_arguments"])
                    and searches[0]["ok"] is case["expected_ok"]
                )
            if case.get("summary"):
                checks["summary_not_quote"] = (
                    "zusammenfassung" in final.casefold() and "nicht" in final.casefold()
                )
            return {
                "case": case["id"],
                "backend": case.get("backend", "sqlite_fts"),
                "passed": all(checks.values()),
                "checks": checks,
                "actions": actions,
                "final": final,
                "result_bytes": result_bytes,
                "definition_bytes": len(json.dumps(wire, separators=(",", ":")).encode()),
                "search_definition_bytes": len(
                    json.dumps(
                        next(tool for tool in wire if tool["name"] == "session_search"),
                        separators=(",", ":"),
                    ).encode()
                ),
                "observed": observed,
                "artifacts": artifacts,
                "definitions": definitions,
            }
        finally:
            await manager.aclose()
            sessions.close()


async def _probe_recall_workflow(adapter: Any, args: argparse.Namespace) -> dict[str, Any]:
    selected = [
        case
        for case in recall_cases() + recall_matrix()
        if (args.recall_case == "all" and not case["id"].startswith("matrix_"))
        or (args.recall_case == "matrix" and case["id"].startswith("matrix_"))
        or case["id"] in args.recall_case.split(",")
    ]
    if not selected:
        raise ValueError("Unknown Recall workflow case")
    rows = []
    semaphore = asyncio.Semaphore(3)

    async def run_case(case: dict[str, Any]) -> None:
        try:
            async with semaphore, asyncio.timeout(args.total_timeout):
                row = await _evaluate_case(adapter, args, case)
        except Exception as error:
            row = {
                "case": case["id"],
                "passed": False,
                "error_type": type(error).__name__,
                "error": str(error),
            }
        rows.append(row)
        rows.sort(
            key=lambda row: next(
                index for index, case in enumerate(selected) if case["id"] == row["case"]
            )
        )
        if args.recall_report:
            args.recall_report.write_text(
                json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8"
            )

    await asyncio.gather(*(run_case(case) for case in selected))
    return {
        "scenario": "recall_workflow",
        "model": args.model,
        "passed": all(row["passed"] for row in rows),
        "cases": [
            {
                key: value
                for key, value in row.items()
                if key not in {"definitions", "observed", "final", "actions", "artifacts"}
            }
            for row in rows
        ],
    }
