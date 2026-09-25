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
            "id": "input_alias",
            "before": {},
            "arguments": {"input": "*** Add File: new.txt\n+hello"},
            "expected": {"new.txt": "hello\n"},
        },
        {
            "id": "conflicting_input_alias",
            "before": {},
            "arguments": {
                "patch": "*** Add File: new.txt\n+hello",
                "input": "*** Add File: other.txt\n+different",
            },
            "error": "invalid_arguments",
        },
        {
            "id": "separate_patch_frames",
            "before": {"one.txt": "old\n"},
            "arguments": patch(
                "*** Update File: one.txt\n@@\n-old\n+new\n*** End Patch\n"
                "*** Begin Patch\n*** Add File: two.txt\n+second\n*** End Patch\n"
                "*** Add File: three.txt\n+third"
            ),
            "expected": {"one.txt": "new\n", "two.txt": "second\n", "three.txt": "third\n"},
        },
        {
            "id": "text_after_patch_end",
            "before": {},
            "arguments": patch("*** Add File: new.txt\n+hello\n*** End Patch\n+stray"),
            "error": "invalid_patch",
        },
        {
            "id": "context_block_anchor",
            "before": {"one.txt": "first\nvalue=1\nsecond\nvalue=1\n"},
            "arguments": patch("*** Update File: one.txt\n@@\n second\n@@\n-value=1\n+value=2"),
            "expected": {"one.txt": "first\nvalue=1\nsecond\nvalue=2\n"},
        },
        {
            "id": "missing_context_block",
            "before": {"one.txt": "value=1\n"},
            "arguments": patch("*** Update File: one.txt\n@@\n missing\n@@\n-value=1\n+value=2"),
            "error": "context_not_found",
        },
        {
            "id": "create",
            "before": {},
            "task": "Create new.txt containing exactly hello followed by a newline.",
            "expected": {"new.txt": "hello\n"},
        },
        {
            "id": "overwrite",
            "before": {"one.txt": "old\ndiscard\n"},
            "read_paths": ["one.txt"],
            "task": "Replace all of one.txt with exactly new followed by a newline.",
            "expected": {"one.txt": "new\n"},
        },
        {
            "id": "empty_existing",
            "before": {"one.txt": "old\n"},
            "read_paths": ["one.txt"],
            "task": "Make one.txt completely empty, keeping the file itself.",
            "expected": {"one.txt": ""},
        },
        {
            "id": "overwrite_exact",
            "before": {"one.txt": "old\r\ndiscard\r\n"},
            "read_paths": ["one.txt"],
            "arguments": patch("*** Add File: one.txt\n+new"),
            "expected": {"one.txt": "new\r\n"},
        },
        {
            "id": "empty_exact",
            "before": {"one.txt": "old\n"},
            "read_paths": ["one.txt"],
            "arguments": patch("*** Add File: one.txt"),
            "expected": {"one.txt": ""},
        },
        {
            "id": "no_final_newline",
            "before": {},
            "arguments": patch("*** Add File: new.txt\n+false\n\\ No newline at end of file"),
            "expected": {"new.txt": "false"},
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
            "mentions": ["Failed: one.txt, hunk 2: the lines to replace were not found."],
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
            "mentions": ["2 of 3 changes applied", "Failed: File not found: missing.txt"],
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
            "mentions": ["Failed: Cannot move to destination.txt", "Skipped: destination.txt"],
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
            "id": "late_move",
            "before": {"one.txt": "old\n"},
            "arguments": patch(
                "*** Update File: one.txt\n@@\n-old\n+new\n"
                "*** Move to: moved.txt\n*** Move to: moved.txt"
            ),
            "expected": {"moved.txt": "new\n"},
        },
        {
            "id": "conflicting_move",
            "before": {"one.txt": "old\n"},
            "arguments": patch(
                "*** Add File: pending.txt\n+pending\n"
                "*** Update File: one.txt\n*** Move to: first.txt\n"
                "@@\n-old\n+new\n*** Move to: second.txt"
            ),
            "error": "conflicting_move",
        },
        {
            "id": "literal_move_text",
            "before": {"one.txt": "old\n"},
            "arguments": patch(
                "*** Update File: one.txt\n@@\n-old\n"
                "+*** Move to: literal.txt\n*** Move to: moved.txt"
            ),
            "expected": {"moved.txt": "*** Move to: literal.txt\n"},
        },
        {
            "id": "recover_short_hint",
            "before": {"one.txt": "timeout=10\nretries=1\n"},
            "task": "Change timeout to 20 in one.txt and preserve retries.",
            "seed": patch("*** Update File: one.txt\n@@ timeout\n-timeout=10\n+timeout=20"),
            "expected": {"one.txt": "timeout=20\nretries=1\n"},
            "only_paths": ["one.txt"],
        },
        {
            "id": "recover_ambiguous_hint",
            "before": {"one.txt": "first\nmarker\nvalue=1\nsecond\nmarker\nvalue=2\n"},
            "task": "Change value to 3 in the second section of one.txt; preserve the first.",
            "seed": patch("*** Update File: one.txt\n@@ marker\n-value=2\n+value=3"),
            "expected": {"one.txt": "first\nmarker\nvalue=1\nsecond\nmarker\nvalue=3\n"},
            "only_paths": ["one.txt"],
        },
        {
            "id": "natural_insert",
            "before": {"one.txt": "first\nlast\n"},
            "task": "Insert the line middle between first and last in one.txt.",
            "expected": {"one.txt": "first\nmiddle\nlast\n"},
        },
        {
            "id": "natural_insert_before",
            "before": {"one.txt": "start();\nsummary();\n"},
            "task": "Insert check(); on its own line immediately before summary(); in one.txt.",
            "expected": {"one.txt": "start();\ncheck();\nsummary();\n"},
        },
        {
            "id": "recover_context_only",
            "before": {"one.txt": "start();\nsummary();\n"},
            "task": "Insert check(); on its own line immediately before summary(); in one.txt.",
            "seed": patch("*** Update File: one.txt\n@@\n summary();"),
            "expected": {"one.txt": "start();\ncheck();\nsummary();\n"},
            "only_paths": ["one.txt"],
        },
        {
            "id": "context_only_is_not_success",
            "before": {"one.txt": "summary();\n"},
            "arguments": patch("*** Update File: one.txt\n@@\n summary();"),
            "error": "no_changes",
        },
        {
            "id": "add_missing_prefixes",
            "before": {},
            "arguments": patch("*** Add File: new.txt\n+first\n\n  indented\n+last"),
            "expected": {"new.txt": "first\n\n  indented\nlast\n"},
        },
        {
            "id": "add_raw_body",
            "before": {},
            "arguments": patch('*** Add File: new.txt\n{\n  "enabled": true\n}'),
            "expected": {"new.txt": '{\n  "enabled": true\n}\n'},
        },
        {
            "id": "add_opening_delimiter",
            "before": {},
            "arguments": patch("*** Add File: new.txt\n@@\n+first\n+last"),
            "expected": {"new.txt": "first\nlast\n"},
        },
        {
            "id": "add_removal_is_ambiguous",
            "before": {},
            "arguments": patch("*** Add File: new.txt\n-old\n+new"),
            "error": "invalid_patch",
        },
        {
            "id": "add_context_hint_is_not_discarded",
            "before": {},
            "arguments": patch("*** Add File: new.txt\n@@ section\n+new"),
            "error": "invalid_patch",
        },
        {
            "id": "add_literal_syntax",
            "before": {},
            "arguments": patch("*** Add File: new.txt\n+-literal\n+@@\n+*** End Patch\n++literal"),
            "expected": {"new.txt": "-literal\n@@\n*** End Patch\n+literal\n"},
        },
        {
            "id": "natural_eof",
            "before": {"one.txt": "old\nold\nold\n"},
            "task": "Change only the final line of one.txt to new, preserving the newline.",
            "expected": {"one.txt": "old\nold\nnew\n"},
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
            "status": "unchanged",
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
            "id": "unread_overwrite",
            "before": {"one.txt": "keep\n"},
            "arguments": patch("*** Add File: one.txt\n+clobber"),
            "error": "file_not_read",
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
            "arguments": {**patch("*** Add File: new.txt\n+hello"), "dry_run": True},
            "error": "invalid_arguments",
        },
        {
            "id": "edit_fields",
            "before": {"one.txt": "alpha\nold\nomega\n"},
            "arguments": {"file_path": "one.txt", "old_string": "old", "new_string": "new"},
            "expected": {"one.txt": "alpha\nnew\nomega\n"},
        },
        {
            "id": "write_fields",
            "before": {},
            "arguments": {"file_path": "new.txt", "content": "hello\n"},
            "expected": {"new.txt": "hello\n"},
        },
    ]


async def _probe_apply_patch_case(
    adapter: Any, args: argparse.Namespace, case: dict[str, Any]
) -> dict[str, Any]:
    from core.tools.apply_patch import patch_targets, register_apply_patch_tool
    from core.tools.file_state import FileReadState
    from core.tools.read import register_read_tool
    from core.tools.tools import ToolContext, ToolRegistry

    with TemporaryDirectory(prefix="vbot-patch-probe-") as directory:
        root = Path(directory).resolve()
        for name, content in case["before"].items():
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content.encode("utf-8"))
        registry = ToolRegistry()
        state = FileReadState()
        register_apply_patch_tool(registry, file_state=state)
        register_read_tool(
            registry,
            attachment_store=None,
            speech_service=None,
            file_state=state,
            speech_max_size_bytes=1024,
        )
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
        for index, name in enumerate(case.get("read_paths", [])):
            read_context = ToolContext(
                agent_id="probe",
                session_id="probe-session",
                run_id="probe-run",
                tool_call_id=f"read-{index}",
                tool_name="read",
                tool_call_index=index,
                workspace=root,
                cwd=root,
                vbot_root=root,
                data_root=root,
            )
            read_result = await registry.dispatch(read_context, {"path": name}, ["read"])
            assert read_result["ok"]
            messages.extend(
                [
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {"id": f"read-{index}", "name": "read", "arguments": {"path": name}}
                        ],
                    },
                    {
                        "role": "tool",
                        "tool_call_id": f"read-{index}",
                        "content": json.dumps(read_result),
                    },
                ]
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
            **adapter.request_context_kwargs(agent_id="patch-probe", session_id=root.name),
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
                tool = registry.get("apply_patch")
                normalized = (
                    tool.argument_normalizer(arguments) if tool.argument_normalizer else arguments
                )
                names = patch_targets(tool.contract.normalize_arguments(normalized))
            except Exception:
                names = []  # Let the real handler diagnose malformed input.
            safe = all((root / name).resolve().is_relative_to(root) for name in names)
            if not safe or call.get("name") != "apply_patch":
                outcomes.append({"ok": False, "error": {"code": "probe_scope_violation"}})
                continue
            touched_paths.update(
                (root / name).resolve().relative_to(root).as_posix() for name in names
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
            try:
                outcomes.append(await registry.dispatch(context, arguments, ["apply_patch"]))
            except ValueError as error:
                outcomes.append(
                    {"ok": False, "error": {"code": "invalid_arguments", "message": str(error)}}
                )
        snapshot = {
            p.relative_to(root).as_posix(): p.read_bytes().decode("utf-8")
            for p in root.rglob("*")
            if p.is_file()
        }
        expected = case.get("expected", case["before"])
        codes = [outcome["error"]["code"] if not outcome["ok"] else None for outcome in outcomes]
        data = (outcomes[0].get("data") or {}) if len(outcomes) == 1 else {}
        result_ok = (
            (case.get("status", "applied") == data.get("status")) if not case.get("error") else True
        )
        content = str(data.get("content", ""))
        entries_ok = all(text in content for text in case.get("mentions", []))
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
