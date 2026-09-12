"""Provider Tool probe: workflow patch."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from scripts.provider_probe.common import PROJECT_ROOT


def _apply_patch_cases() -> list[dict[str, Any]]:
    def patch(body: str) -> dict[str, str]:
        return {"patch": "*** Begin Patch\n" + body + "\n*** End Patch"}

    return [
        {
            "id": "create",
            "before": {},
            "task": "Create new.txt containing exactly hello followed by a newline.",
            "expected": {"new.txt": "hello\n"},
        },
        {
            "id": "batch_locations",
            "before": {
                "settings.txt": "timeout=10\nseparator\nretries=1\n",
                "notes.txt": "Status: draft\n",
            },
            "task": (
                "Set timeout to 20 and retries to 3 in settings.txt, and mark notes.txt as ready."
            ),
            "expected": {
                "settings.txt": "timeout=20\nseparator\nretries=3\n",
                "notes.txt": "Status: ready\n",
            },
        },
        {
            "id": "batch_file_operations",
            "before": {"source.txt": "keep\n", "obsolete.txt": "obsolete\n"},
            "task": (
                "Move source.txt to nested/moved.txt, delete obsolete.txt, and create "
                "notes.txt containing done followed by a newline."
            ),
            "expected": {"nested/moved.txt": "keep\n", "notes.txt": "done\n"},
        },
        {
            "id": "partial_hunks",
            "before": {"one.txt": "first=old\nsecond=old\n"},
            "arguments": patch(
                "*** Update File: one.txt\n@@\n-first=old\n+first=new\n@@\n"
                "-completely missing declaration\n+unused\n@@\n-second=old\n+second=new"
            ),
            "expected": {"one.txt": "first=new\nsecond=new\n"},
            "status": "partial",
            "entries": ["applied", "failed", "applied"],
        },
        {
            "id": "partial_files",
            "before": {},
            "arguments": patch(
                "*** Add File: first.txt\n+one\n*** Delete File: missing.txt\n"
                "*** Add File: last.txt\n+two"
            ),
            "expected": {"first.txt": "one\n", "last.txt": "two\n"},
            "status": "partial",
            "entries": ["applied", "failed", "applied"],
        },
        {
            "id": "failed_move_dependency",
            "before": {"source.txt": "source\n", "destination.txt": "destination\n"},
            "arguments": patch(
                "*** Move File: source.txt -> destination.txt\n"
                "*** Update File: destination.txt\n@@\n-destination\n+clobbered\n"
                "*** Add File: good.txt\n+done"
            ),
            "expected": {
                "source.txt": "source\n",
                "destination.txt": "destination\n",
                "good.txt": "done\n",
            },
            "status": "partial",
            "entries": ["failed", "skipped", "applied"],
        },
        {
            "id": "recover_without_replay",
            "before": {
                "log.txt": "start\n",
                "one.txt": "def deploy():\n    timeout = 30\n    retries = 5\n",
            },
            "task": (
                "Append done followed by a newline to log.txt and change the deploy timeout "
                "to 60 in one.txt. Preserve the retries setting."
            ),
            "seed": patch(
                "*** Update File: log.txt\n@@\n+done\n*** Update File: one.txt\n@@\n"
                " def deploy():\n-    completely unrelated declaration\n"
                "+    timeout = 60\n     retries = 5"
            ),
            "expected": {
                "log.txt": "start\ndone\n",
                "one.txt": "def deploy():\n    timeout = 60\n    retries = 5\n",
            },
            "only_paths": ["one.txt"],
        },
        {
            "id": "unframed_implicit_hunk",
            "before": {"one.txt": "heading\nold\ntail\n"},
            "arguments": {"patch": "*** Update File: one.txt\nheading\n-old\n+new\ntail"},
            "expected": {"one.txt": "heading\nnew\ntail\n"},
        },
        {
            "id": "binary_update",
            "before": {"one.txt": "a\x00b"},
            "arguments": patch("*** Update File: one.txt\n@@\n-a\n+b"),
            "error": "binary_file",
        },
        {
            "id": "empty",
            "before": {},
            "task": "Create an empty new.txt file.",
            "expected": {"new.txt": ""},
        },
        {
            "id": "update",
            "before": {"one.txt": "alpha\nold\nomega\n"},
            "task": "Change the line old to new in one.txt.",
            "expected": {"one.txt": "alpha\nnew\nomega\n"},
        },
        {
            "id": "delete",
            "before": {"gone.txt": "obsolete\n"},
            "task": "Delete gone.txt.",
            "expected": {},
        },
        {
            "id": "move",
            "before": {"one.txt": "keep\n"},
            "task": "Move one.txt to nested/new.txt without changing its contents.",
            "expected": {"nested/new.txt": "keep\n"},
        },
        {
            "id": "update_move",
            "before": {"one.txt": "old\n"},
            "task": "Move one.txt to moved.txt and change old to new in it.",
            "expected": {"moved.txt": "new\n"},
        },
        {
            "id": "move_with_hunks",
            "before": {"one.txt": "old\n"},
            "arguments": patch("*** Move File: one.txt -> moved.txt\n@@\n-old\n+new"),
            "expected": {"moved.txt": "new\n"},
        },
        {
            "id": "sequence",
            "before": {},
            "arguments": patch(
                "*** Add File: one.txt\n+old\n*** Update File: one.txt\n@@\n-old\n+new"
            ),
            "expected": {"one.txt": "new\n"},
        },
        {
            "id": "append",
            "before": {"one.txt": "first\n"},
            "arguments": patch("*** Update File: one.txt\n@@\n+last"),
            "expected": {"one.txt": "first\nlast\n"},
        },
        {
            "id": "hint",
            "before": {"one.txt": "first\nold\nsecond\nold\n"},
            "arguments": patch("*** Update File: one.txt\n@@ second\n-old\n+new"),
            "expected": {"one.txt": "first\nold\nsecond\nnew\n"},
        },
        {
            "id": "eof",
            "before": {"one.txt": "old\nsecond\nold\n"},
            "arguments": patch("*** Update File: one.txt\n@@\n-old\n+new\n*** End of File"),
            "expected": {"one.txt": "old\nsecond\nnew\n"},
        },
        {
            "id": "retry",
            "before": {"one.txt": "alpha\nnew\nomega\n"},
            "arguments": patch("*** Update File: one.txt\n@@\n alpha\n-old\n+new\n omega"),
            "expected": {"one.txt": "alpha\nnew\nomega\n"},
        },
        {
            "id": "ambiguous",
            "before": {"one.txt": "old\nother\nold\n"},
            "arguments": patch("*** Update File: one.txt\n@@\n-old\n+new"),
            "error": "ambiguous_match",
        },
        {
            "id": "missing",
            "before": {},
            "arguments": patch("*** Delete File: missing.txt"),
            "error": "file_not_found",
        },
        {
            "id": "collision",
            "before": {"one.txt": "keep\n"},
            "arguments": patch("*** Add File: one.txt\n+clobber"),
            "error": "destination_exists",
        },
        {
            "id": "malformed",
            "before": {},
            "arguments": patch("*** Unknown File: one.txt"),
            "error": "invalid_patch",
        },
        {
            "id": "unknown_field",
            "before": {},
            "arguments": {**patch("*** Add File: new.txt\n+hello"), "path": "new.txt"},
            "error": "invalid_arguments",
        },
    ]


async def _probe_apply_patch_case(
    adapter: Any, args: argparse.Namespace, case: dict[str, Any]
) -> dict[str, Any]:
    from core.tools._patch_syntax import _parse
    from core.tools.apply_patch import register_apply_patch_tool
    from core.tools.file_state import FileReadState
    from core.tools.tools import ToolContext, ToolRegistry

    with TemporaryDirectory(prefix="vbot-patch-probe-") as directory:
        root = Path(directory).resolve()
        for name, content in case["before"].items():
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content.encode("utf-8"))
        registry = ToolRegistry()
        register_apply_patch_tool(registry, file_state=FileReadState())
        definitions = registry.provider_definitions(allowed_tools=["apply_patch"])
        if "arguments" in case:
            task = (
                "Exercise this file-tool request exactly once, including any invalid fields or "
                "patch syntax so its validation is exercised. Do not repair the request: "
                + json.dumps(case["arguments"])
            )
        else:
            task = case["task"]
        instruction = (
            task + "\nCurrent files and their exact contents:\n" + json.dumps(case["before"])
        )
        # Natural tasks receive no call-count instruction or expected arguments.
        messages: list[dict[str, Any]] = (
            [
                {
                    "role": "system",
                    "content": "Exercise the requested Tool Call exactly once. Preserve the "
                    "requested arguments verbatim, including deliberately invalid fields "
                    "or syntax: the Tool's runtime validation is under test. "
                    "Do not repair or omit invalid arguments. Do not answer with ordinary text.",
                },
                {"role": "user", "content": instruction},
            ]
            if "arguments" in case
            else [{"role": "user", "content": instruction}]
        )
        if "seed" in case:
            seed_context = ToolContext(
                agent_id="probe",
                session_id="probe-session",
                run_id="probe-run",
                tool_call_id="seed",
                tool_name="apply_patch",
                tool_call_index=0,
                workspace=root,
                cwd=root,
                vbot_root=root,
                data_root=root,
            )
            seed_result = await registry.dispatch(seed_context, case["seed"], ["apply_patch"])
            messages.extend(
                [
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {"id": "seed", "name": "apply_patch", "arguments": case["seed"]}
                        ],
                    },
                    {"role": "tool", "tool_call_id": "seed", "content": json.dumps(seed_result)},
                ]
            )
        raw = await adapter.send(
            messages,
            model_id=args.model,
            tools=definitions,
            thinking_effort=args.thinking_effort,
            max_tokens=args.max_tokens or 4000,
        )
        response = adapter.normalize_response(raw, model_id=args.model)
        calls = response.get("tool_calls") or []
        outcomes: list[dict[str, Any]] = []
        touched_paths: set[str] = set()
        for index, call in enumerate(calls):
            arguments = call.get("arguments", {})
            # The evaluator can only mutate its disposable directory, even if
            # the model invents a path. Runtime file tools retain normal agency.
            try:
                operations = _parse(arguments.get("patch", ""))
            except Exception:
                operations = []  # Let the real handler diagnose malformed input.
            safe = all(
                (root / name).resolve().is_relative_to(root)
                for operation in operations
                for name in (operation.path, operation.destination)
                if name is not None
            )
            if not safe or call.get("name") != "apply_patch":
                outcomes.append({"ok": False, "error": {"code": "probe_scope_violation"}})
                continue
            touched_paths.update(
                (root / name).resolve().relative_to(root).as_posix()
                for operation in operations
                for name in (operation.path, operation.destination)
                if name is not None
            )
            context = ToolContext(
                agent_id="probe",
                session_id="probe-session",
                run_id="probe-run",
                tool_call_id=call["id"],
                tool_name="apply_patch",
                tool_call_index=index,
                workspace=root,
                cwd=root,
                vbot_root=root,
                data_root=root,
            )
            outcomes.append(await registry.dispatch(context, arguments, ["apply_patch"]))
        snapshot = {
            p.relative_to(root).as_posix(): p.read_bytes().decode("utf-8")
            for p in root.rglob("*")
            if p.is_file()
        }
        expected = case.get("expected", case["before"])
        codes = [outcome["error"]["code"] if not outcome["ok"] else None for outcome in outcomes]
        data = (outcomes[0].get("data") or {}) if len(outcomes) == 1 else {}
        entries = [entry["status"] for entry in data.get("results", [])]
        result_ok = (
            (case.get("status", "success") == data.get("status")) if not case.get("error") else True
        )
        entries_ok = entries == case["entries"] if "entries" in case else True
        recovery_ok = touched_paths == set(case["only_paths"]) if "only_paths" in case else True
        passed = (
            len(outcomes) == 1
            and snapshot == expected
            and codes == [case.get("error")]
            and result_ok
            and entries_ok
            and recovery_ok
        )
        return {
            "case": case["id"],
            "passed": passed,
            "calls": len(calls),
            "effect_ok": snapshot == expected,
            "error_codes": codes,
            "result_ok": result_ok and entries_ok,
            "recovery_ok": recovery_ok,
            "definition_chars": len(json.dumps(definitions, separators=(",", ":"))),
        }


async def _probe_apply_patch(adapter: Any, args: argparse.Namespace) -> dict[str, Any]:
    from core.tools import apply_patch as patch_module

    expected_source = PROJECT_ROOT / "core/tools/apply_patch.py"
    if Path(patch_module.__file__).resolve() != expected_source:
        raise RuntimeError(
            "The probe loaded apply_patch from another checkout. Run "
            "python -m scripts.probe_provider_tool_call from the checkout being evaluated."
        )
    limit = asyncio.Semaphore(4)

    async def evaluate(case: dict[str, Any]) -> dict[str, Any]:
        async with limit:
            return await _probe_apply_patch_case(adapter, args, case)

    rows = await asyncio.gather(*(evaluate(case) for case in _apply_patch_cases()))
    return {
        "scenario": "apply_patch",
        "model": args.model,
        "cases": rows,
        "passed": all(row["passed"] for row in rows),
    }
