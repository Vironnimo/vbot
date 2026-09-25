"""Guided single-Tool search conformance, not independent Tool-choice evaluation."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from core.tools.search_files import interpret_search_call, register_search_files_tool
from core.tools.tools import ToolContext, ToolRegistry


def search_cases() -> list[dict[str, Any]]:
    return [
        {
            "id": "content_defaults",
            "arguments": {"args": ["-e", "alpha"]},
            "content": "src/a.py:1:alpha alpha\ntests/b.PY:1:alpha",
        },
        {
            "id": "multiple_patterns_roots",
            "arguments": {"args": ["-e", "beta", "-e", "omega", "--", "src", "other.txt"]},
            "content": "other.txt:1:omega\nsrc/a.py:2:beta",
        },
        {
            "id": "file_root",
            "arguments": {"args": ["-e", "alpha", "--", "src/a.py"]},
            "content": "src/a.py:1:alpha alpha",
        },
        {
            "id": "paths_defaults",
            "arguments": {"args": ["--entries", "--sort=path"]},
            "content": "other.txt\nsrc/\nsrc/a.py\nsrc/empty/\ntests/\ntests/b.PY",
        },
        {
            "id": "paths_patterns",
            "arguments": {"args": ["--files", "--sort=path", "-g", "./**/*.py"]},
            "content": "src/a.py\ntests/b.PY",
        },
        {
            "id": "directories",
            "arguments": {"args": ["--dirs", "-g", "./**/empty"]},
            "content": "src/empty/",
        },
        {
            "id": "help",
            "arguments": {"args": ["--help"]},
            "content": None,
            "contains": "--files-without-match",
        },
        {
            "id": "limit",
            "arguments": {"args": ["-e", "alpha"], "limit": 1},
            "content": "src/a.py:1:alpha alpha",
            "next_offset": 1,
        },
        {
            "id": "offset",
            "arguments": {"args": ["-e", "alpha"], "offset": 1},
            "content": "tests/b.PY:1:alpha",
        },
        {
            "id": "beyond_end",
            "arguments": {"args": ["-e", "alpha"], "offset": 50},
            "content": None,
            "contains": "offset 50",
        },
        {
            "id": "files",
            "arguments": {"args": ["-l", "-e", "alpha"]},
            "content": "src/a.py\ntests/b.PY",
        },
        {
            "id": "without",
            "arguments": {"args": ["--files-without-match", "-e", "alpha"]},
            "content": "other.txt",
        },
        {
            "id": "line_counts",
            "arguments": {"args": ["-c", "-e", "alpha"]},
            "content": "src/a.py:1\ntests/b.PY:1",
        },
        {
            "id": "occurrences",
            "arguments": {"args": ["--count-matches", "-e", "alpha"]},
            "content": "src/a.py:2\ntests/b.PY:1",
        },
        {
            "id": "zero_counts",
            "arguments": {"args": ["-c", "--include-zero", "-e", "alpha"]},
            "content": "other.txt:0\nsrc/a.py:1\ntests/b.PY:1",
        },
        {
            "id": "only_matches",
            "arguments": {"args": ["-o", "-e", "alpha"]},
            "content": "src/a.py:1:1:alpha\nsrc/a.py:1:7:alpha\ntests/b.PY:1:1:alpha",
        },
        {
            "id": "filters",
            "arguments": {"args": ["-g", "*.py", "-g", "!b.PY", "-e", "alpha"]},
            "content": "src/a.py:1:alpha alpha",
        },
        {
            "id": "case_sensitive_globs",
            "arguments": {"args": ["--no-glob-case-insensitive", "-g", "*.py", "-e", "alpha"]},
            "content": "src/a.py:1:alpha alpha",
        },
        {
            "id": "ignore_case_glob",
            "arguments": {"args": ["--no-glob-case-insensitive", "--iglob", "*.py", "-e", "alpha"]},
            "content": "src/a.py:1:alpha alpha\ntests/b.PY:1:alpha",
        },
        {
            "id": "type",
            "arguments": {"args": ["-tpy", "-e", "alpha"]},
            "content": "src/a.py:1:alpha alpha\ntests/b.PY:1:alpha",
        },
        {
            "id": "type_not",
            "arguments": {"args": ["-Tpy", "-e", "alpha"]},
            "content": "No results.",
        },
        {
            "id": "type_add_clear",
            "arguments": {
                "args": [
                    "--type-add",
                    "custom:*.py",
                    "--type-clear",
                    "custom",
                    "--type-add",
                    "custom:*.PY",
                    "-tcustom",
                    "-e",
                    "alpha",
                ]
            },
            "content": "src/a.py:1:alpha alpha\ntests/b.PY:1:alpha",
        },
        {
            "id": "max_size",
            "arguments": {"args": ["--max-filesize", "10", "-e", "alpha"]},
            "content": "tests/b.PY:1:alpha",
        },
        {
            "id": "depth",
            "arguments": {"args": ["--max-depth", "1", "-e", "alpha"]},
            "content": "No results.",
        },
        {
            "id": "sort_reverse",
            "arguments": {"args": ["--sortr=path", "-e", "alpha"]},
            "content": "tests/b.PY:1:alpha\nsrc/a.py:1:alpha alpha",
        },
        {
            "id": "context",
            "arguments": {"args": ["-C1", "-e", "alpha"]},
            "content": "src/a.py:1:alpha alpha\nsrc/a.py:2-beta\ntests/b.PY:1:alpha",
        },
        {
            "id": "asymmetric_context",
            "arguments": {"args": ["-B2", "-A1", "-e", "alpha"]},
            "content": "src/a.py:1:alpha alpha\nsrc/a.py:2-beta\ntests/b.PY:1:alpha",
        },
        {
            "id": "flags_separate",
            "arguments": {"args": ["-n", "-H", "--no-heading", "--color=never", "-e", "alpha"]},
            "content": "src/a.py:1:alpha alpha\ntests/b.PY:1:alpha",
        },
        {
            "id": "fixed",
            "arguments": {"args": ["-F", "-e", "alpha"]},
            "content": "src/a.py:1:alpha alpha\ntests/b.PY:1:alpha",
        },
        {
            "id": "word",
            "arguments": {"args": ["-w", "-e", "alpha"]},
            "content": "src/a.py:1:alpha alpha\ntests/b.PY:1:alpha",
        },
        {
            "id": "line",
            "arguments": {"args": ["-x", "-e", "alpha"]},
            "content": "tests/b.PY:1:alpha",
        },
        {
            "id": "overrides",
            "arguments": {"args": ["-i", "-s", "-F", "--no-fixed-strings", "-e", "alpha"]},
            "content": "src/a.py:1:alpha alpha\ntests/b.PY:1:alpha",
        },
        {
            "id": "unrestricted",
            "arguments": {"args": ["-uuu", "-e", "alpha"]},
            "content": "src/a.py:1:alpha alpha\ntests/b.PY:1:alpha",
        },
        {
            "id": "ignore_controls",
            "arguments": {
                "args": [
                    "--no-ignore",
                    "--ignore",
                    "--no-ignore-parent",
                    "--no-ignore-global",
                    "--no-ignore-exclude",
                    "--no-ignore-dot",
                    "--no-ignore-vcs",
                    "--no-ignore-files",
                    "-e",
                    "alpha",
                ]
            },
            "content": "src/a.py:1:alpha alpha\ntests/b.PY:1:alpha",
        },
        {
            "id": "scope_controls",
            "arguments": {"args": ["--no-hidden", "--follow", "--one-file-system", "-e", "alpha"]},
            "content": "src/a.py:1:alpha alpha\ntests/b.PY:1:alpha",
        },
        {
            "id": "unicode",
            "arguments": {"args": ["--no-unicode", "--unicode", "-e", "alpha"]},
            "content": "src/a.py:1:alpha alpha\ntests/b.PY:1:alpha",
        },
        {
            "id": "encoding",
            "arguments": {"args": ["-Eutf-8", "--crlf", "-e", "alpha"]},
            "content": "src/a.py:1:alpha alpha\ntests/b.PY:1:alpha",
        },
        {
            "id": "early_stop",
            "arguments": {"args": ["-m1", "--stop-on-nonmatch", "-e", "alpha"]},
            "content": "src/a.py:1:alpha alpha\ntests/b.PY:1:alpha",
        },
        {
            "id": "diagnostics",
            "arguments": {"args": ["--stats", "--debug", "-e", "alpha"]},
            "content": "src/a.py:1:alpha alpha\ntests/b.PY:1:alpha",
        },
        {
            "id": "ignore_case",
            "arguments": {"args": ["-i", "-e", "upper"]},
            "content": "src/a.py:3:UPPER",
        },
        {
            "id": "smart_case",
            "arguments": {"args": ["-S", "-e", "upper"]},
            "content": "src/a.py:3:UPPER",
        },
        {
            "id": "smart_case_upper",
            "arguments": {"args": ["-S", "-e", "ALPHA"]},
            "content": "No results.",
        },
        {
            "id": "inversion",
            "arguments": {"args": ["-v", "-e", "alpha"]},
            "content": "other.txt:1:omega\nsrc/a.py:2:beta\nsrc/a.py:3:UPPER",
        },
        {
            "id": "multiline",
            "arguments": {"args": ["-U", "-e", "alpha alpha\\nbeta"]},
            "content": "src/a.py:1:alpha alpha\nbeta",
        },
        {
            "id": "dotall",
            "arguments": {"args": ["-U", "--multiline-dotall", "-e", "alpha.*beta"]},
            "content": "src/a.py:1:alpha alpha\nbeta",
        },
        {
            "id": "pcre",
            "arguments": {"args": ["-P", "-o", "-e", "alpha(?= alpha)"]},
            "content": "src/a.py:1:1:alpha",
        },
        {
            "id": "auto_engine",
            "arguments": {"args": ["--engine=auto", "-o", "-e", "alpha(?= alpha)"]},
            "content": "src/a.py:1:1:alpha",
        },
        {
            "id": "types_help",
            "arguments": {"args": ["--type-list"]},
            "content": None,
            "contains": "py:",
        },
        {
            "id": "exists",
            "arguments": {"args": ["-q", "-e", "alpha"]},
            "content": None,
            "matched": True,
        },
        {
            "id": "absent",
            "arguments": {"args": ["-q", "-e", "absent"]},
            "content": None,
            "matched": False,
        },
        {
            "id": "preserve_array_regex",
            "arguments": {"args": ["-e", "\\balpha\\s+alpha"]},
            "content": "src/a.py:1:alpha alpha",
        },
        {
            "id": "literal_call_text",
            "arguments": {"args": ["-F", "-e", "computeDamage("]},
            "content": "calls.ts:2:computeDamage(actor, target);",
            "files": {"calls.ts": "// ordinary comment\ncomputeDamage(actor, target);\n"},
        },
        {
            "id": "preserve_quotes",
            "arguments": {"args": ["-e", '"alpha"']},
            "content": "No results.",
            "patterns": ['"alpha"'],
            "searched_paths": ["."],
        },
        {
            "id": "missing_root",
            "arguments": {"args": ["-e", "alpha", "--", "missing", "src"]},
            "content": "src/a.py:1:alpha alpha",
            "complete": False,
            "missing_paths": ["missing"],
            "searched_paths": ["src"],
        },
        {
            "id": "all_roots_missing",
            "arguments": {"args": ["-e", "alpha", "--", "missing"]},
            "content": None,
            "error": True,
            "error_contains": "not found",
        },
        {
            "id": "list_without_pattern",
            "arguments": {"path": "src", "glob": "*.py"},
            "content": "src/a.py",
        },
        {
            "id": "named_fields",
            "arguments": {"pattern": "alpha", "path": "tests"},
            "content": "tests/b.PY:1:alpha",
        },
        {
            "id": "other_interface_spellings",
            "arguments": {
                "pattern": "ALPHA",
                "-i": True,
                "glob": "*.py",
                "output_mode": "files_with_matches",
            },
            "content": "src/a.py\ntests/b.PY",
        },
        {
            "id": "glob_in_pattern",
            "arguments": {"pattern": "*.py", "path": "src"},
            "content": "src/a.py",
        },
        {
            "id": "inapplicable",
            "arguments": {"args": ["--files", "-c"]},
            "content": None,
            "error": True,
            "error_contains": "contents",
        },
        {
            "id": "directory_type",
            "arguments": {"args": ["--entries", "-tpy"]},
            "content": None,
            "error": True,
            "error_contains": "--files",
        },
        {
            "id": "invalid_regex",
            "arguments": {"args": ["-e", "["]},
            "content": None,
            "error": True,
            "error_contains": "regex",
        },
        {
            "id": "unknown_option",
            "arguments": {"args": ["--pre", "program", "-e", "alpha"]},
            "content": None,
            "error": True,
            "error_contains": "unsupported",
        },
        {
            "id": "natural_word_scope",
            "task": (
                "Find the whole word alpha in Python files under src and tests; "
                "show matching lines."
            ),
            "content": "src/a.py:1:alpha alpha\ntests/b.PY:1:alpha",
        },
        {
            "id": "natural_directories",
            "task": "Find directories named empty, including empty directories.",
            "content": "src/empty/",
        },
        {
            "id": "natural_count",
            "task": (
                "How many individual occurrences of alpha does each file contain? "
                "Return per-file counts."
            ),
            "content": "src/a.py:2\ntests/b.PY:1",
        },
        {
            "id": "natural_literal_call",
            "task": "Find lines containing the literal text computeDamage( in this directory.",
            "files": {"calls.ts": "// ordinary comment\ncomputeDamage(actor, target);\n"},
            "content": "calls.ts:2:computeDamage(actor, target);",
        },
        {
            "id": "positional",
            "arguments": {"args": ["alpha"]},
            "content": "src/a.py:1:alpha alpha\ntests/b.PY:1:alpha",
        },
        {
            "id": "flags_after_operands",
            "arguments": {"args": ["alpha", "tests", "-n"]},
            "content": "tests/b.PY:1:alpha",
        },
        {
            "id": "multiple_regexp",
            "arguments": {"args": ["-ealpha", "--regexp=omega"]},
            "content": "other.txt:1:omega\nsrc/a.py:1:alpha alpha\ntests/b.PY:1:alpha",
        },
        {
            "id": "literal_program_name",
            "arguments": {"args": ["rg"]},
            "files": {"search.txt": "rg is a search program\n"},
            "content": "search.txt:1:rg is a search program",
        },
        {
            "id": "name_ignore_case",
            "arguments": {"args": ["--files", "-i", "-g", "*.py", "--sort=path"]},
            "content": "src/a.py\ntests/b.PY",
        },
        {
            "id": "name_case_sensitive",
            "arguments": {"args": ["--files", "-s", "-g", "*.py"]},
            "content": "src/a.py",
        },
        {
            "id": "directory_basename",
            "arguments": {"args": ["--dirs", "-g", "empty"]},
            "content": "src/empty/",
        },
        {"id": "empty_root", "arguments": {"args": ["alpha", ""]}, "content": None, "error": True},
        {
            "id": "conflicting_modes",
            "arguments": {"args": ["--files", "--dirs"]},
            "content": None,
            "error": True,
        },
        {
            "id": "content_pattern_in_discovery",
            "arguments": {"args": ["--files", "-e", "alpha"]},
            "content": None,
            "error": True,
        },
        {"id": "missing_flag_value", "arguments": {"args": ["-g"]}, "content": None, "error": True},
        {
            "id": "literal_boolean_word",
            "arguments": {"args": ["-n", "true"]},
            "content": "No results.",
        },
        {
            "id": "repair_encoded_args",
            "arguments": {"args": '["-e", "alpha\\s+alpha"]'},
            "content": "src/a.py:1:alpha alpha",
        },
        {
            "id": "argv_alias",
            "arguments": {"argv": ["alpha"]},
            "content": "src/a.py:1:alpha alpha\ntests/b.PY:1:alpha",
        },
        {
            "id": "conflicting_alias",
            "arguments": {"args": ["alpha"], "argv": ["beta"]},
            "error": True,
            "error_contains": "conflicting",
        },
        {
            "id": "unknown_feature",
            "arguments": {"args": ["alpha"], "fuzzy": True},
            "error": True,
            "error_contains": "is not a parameter",
        },
        {
            "id": "natural_recipe_call",
            "task": "Find lines containing add_recipe( in Python files under src.",
            "files": {"src/recipes.py": "# recipes\ndef add_recipe(title):\n    pass\n"},
            "content": "src/recipes.py:2:def add_recipe(title):",
        },
        {
            "id": "natural_name_only",
            "task": "Find Python file paths anywhere below this directory, ignoring filename case.",
            "content": "src/a.py\ntests/b.PY",
            "unordered_lines": True,
        },
    ]


async def _case(adapter: Any, args: argparse.Namespace, case: dict) -> dict:
    with TemporaryDirectory(prefix="vbot-search-probe-") as directory:
        # Compare canonical paths on both sides (Windows Temp may use 8.3 names).
        root = Path(directory).resolve()
        for name, content in {
            "src/a.py": "alpha alpha\nbeta\nUPPER\n",
            "tests/b.PY": "alpha\n",
            "other.txt": "omega\n",
            **case.get("files", {}),
        }.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8", newline="\n")
        (root / "src/empty").mkdir()
        registry = ToolRegistry()
        register_search_files_tool(registry)
        definitions = registry.provider_definitions(["search_files"])
        messages = [
            {
                "role": "system",
                "content": (
                    "Use the supplied Tool to perform the requested search. "
                    "Make exactly one Tool Call."
                ),
            },
            {
                "role": "user",
                "content": case.get("task")
                or (
                    "Exercise this exact call, preserving intentional mistakes "
                    "so the Tool can handle them. Keep unknown and conflicting fields "
                    "to exercise the Tool's validation. Preserve each value's type as well "
                    "as its contents, including strings in array-typed fields. "
                    "Do not add omitted fields, or change the argument order: "
                )
                + json.dumps(case["arguments"]),
            },
        ]
        async with asyncio.timeout(args.total_timeout):
            raw = await adapter.send(
                messages,
                model_id=args.model,
                tools=definitions,
                thinking_effort=args.thinking_effort,
                max_tokens=args.max_tokens or 1500,
                **adapter.request_context_kwargs(agent_id="search-probe", session_id=root.name),
            )
        response = adapter.normalize_response(raw, model_id=args.model)
        calls = response.get("tool_calls") or []
        if len(calls) != 1 or calls[0].get("name") != "search_files":
            return {
                "case": case["id"],
                "passed": False,
                "reason": "expected one search_files call",
                "tool_calls": calls,
            }
        arguments = calls[0].get("arguments", {})
        context = ToolContext(
            agent_id="probe",
            session_id="probe",
            run_id="probe",
            tool_call_id="probe",
            tool_name="search_files",
            tool_call_index=0,
            workspace=root,
            vbot_root=root,
            data_root=root,
        )
        try:
            query = interpret_search_call(arguments)
            roots = query["paths"] or ["."]
            if any(not (root / p).resolve().is_relative_to(root) for p in roots):
                return {"case": case["id"], "passed": False, "reason": "outside fixture scope"}
            result = await registry.dispatch(context, arguments, ["search_files"])
        except ValueError as error:
            result = {"ok": False, "error": {"message": str(error)}}
        data = result.get("data") or {}
        passed = bool(result["ok"]) != bool(case.get("error"))
        if "error_contains" in case:
            passed = passed and case["error_contains"] in (
                (result.get("error") or {}).get("message", "").lower()
            )
        if case.get("content") is not None:
            actual, expected = data.get("content", ""), case["content"]
            passed = passed and (
                sorted(actual.splitlines()) == sorted(expected.splitlines())
                if case.get("unordered_lines")
                else actual == expected
            )
        if "contains" in case:
            passed = passed and case["contains"] in data.get("content", "")
        for field in ("matched", "next_offset", "patterns", "complete"):
            if field in case:
                passed = passed and data.get(field) == case[field]
        for field in ("searched_paths", "missing_paths"):
            if field in case:
                passed = passed and data.get(field) == [
                    (root / path).as_posix() for path in case[field]
                ]
        return {
            "case": case["id"],
            "passed": passed,
            "runtime_ok": result["ok"],
            **({} if passed else {"arguments": arguments, "result": result}),
        }


async def _probe_search_files(adapter: Any, args: argparse.Namespace) -> dict:
    selected = getattr(args, "search_case", "all")
    requested = set(selected.split(","))
    known = {case["id"] for case in search_cases()}
    if selected != "all" and requested - known:
        raise ValueError(f"Unknown search case(s): {', '.join(sorted(requested - known))}")
    cases = [case for case in search_cases() if selected == "all" or case["id"] in requested]
    if not cases:
        raise ValueError(f"Unknown search case: {selected}")
    semaphore = asyncio.Semaphore(4)

    async def evaluate(case):
        async with semaphore:
            try:
                result = await _case(adapter, args, case)
            except Exception as error:
                result = {
                    "case": case["id"],
                    "passed": False,
                    "reason": str(error) or type(error).__name__,
                }
            logging.getLogger("vbot.search_probe").info(
                "%s: %s", case["id"], "passed" if result["passed"] else "failed"
            )
            return result

    rows = await asyncio.gather(*(evaluate(case) for case in cases))
    return {
        "scenario": "search_files",
        "evaluation": "guided_single_tool_conformance",
        "cases": len(rows),
        "passed": all(row["passed"] for row in rows),
        "results": rows,
    }
