"""Fresh-Model file-search calls, checked through real dispatch on disposable files."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from core.tools.search_files import register_search_files_tool
from core.tools.tools import ToolContext, ToolRegistry


def search_cases() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []

    def add(name, arguments, content=None, **checks):
        cases.append({"id": name, "arguments": arguments, "content": content, **checks})

    base = {"action": "content", "patterns": ["alpha"]}
    normal = "src/a.py:1:alpha alpha\ntests/b.PY:1:alpha"
    add("content_defaults", base, normal)
    add(
        "multiple_patterns_roots",
        {**base, "patterns": ["beta", "omega"], "paths": ["src", "other.txt"]},
        "other.txt:1:omega\nsrc/a.py:2:beta",
    )
    add("file_root", {**base, "paths": ["src/a.py"]}, "src/a.py:1:alpha alpha")
    add(
        "paths_defaults",
        {"action": "paths", "options": ["--sort=path"]},
        "other.txt\nsrc/\nsrc/a.py\nsrc/empty/\ntests/\ntests/b.PY",
    )
    add(
        "paths_patterns",
        {"action": "paths", "patterns": ["**/*.py"], "kind": "files", "options": ["--sort=path"]},
        "src/a.py\ntests/b.PY",
    )
    add(
        "directories",
        {"action": "paths", "kind": "directories", "patterns": ["**/empty"]},
        "src/empty/",
    )
    add("help", {"action": "help"}, contains="--files-without-match")
    add("limit", {**base, "limit": 1}, "src/a.py:1:alpha alpha", next_offset=1)
    add("offset", {**base, "offset": 1}, "tests/b.PY:1:alpha")
    add("beyond_end", {**base, "offset": 50}, contains="offset 50")
    for name, options, content in (
        ("files", ["-l"], "src/a.py\ntests/b.PY"),
        ("without", ["--files-without-match"], "other.txt"),
        ("line_counts", ["-c"], "src/a.py:1\ntests/b.PY:1"),
        ("occurrences", ["--count-matches"], "src/a.py:2\ntests/b.PY:1"),
        ("zero_counts", ["-c", "--include-zero"], "other.txt:0\nsrc/a.py:1\ntests/b.PY:1"),
        ("only_matches", ["-o"], "src/a.py:1:1:alpha\nsrc/a.py:1:7:alpha\ntests/b.PY:1:1:alpha"),
        ("filters", ["-g", "*.py", "-g", "!b.PY"], "src/a.py:1:alpha alpha"),
        (
            "case_sensitive_globs",
            ["--no-glob-case-insensitive", "-g", "*.py"],
            "src/a.py:1:alpha alpha",
        ),
        ("ignore_case_glob", ["--no-glob-case-insensitive", "--iglob", "*.py"], normal),
        ("type", ["-tpy"], normal),
        ("type_not", ["-Tpy"], "No results."),
        (
            "type_add_clear",
            [
                "--type-add",
                "custom:*.py",
                "--type-clear",
                "custom",
                "--type-add",
                "custom:*.PY",
                "-tcustom",
            ],
            normal,
        ),
        ("max_size", ["--max-filesize", "10"], "tests/b.PY:1:alpha"),
        ("depth", ["--max-depth", "1"], "No results."),
        ("sort_reverse", ["--sortr=path"], "tests/b.PY:1:alpha\nsrc/a.py:1:alpha alpha"),
        ("context", ["-C1"], "src/a.py:1:alpha alpha\nsrc/a.py:2-beta\ntests/b.PY:1:alpha"),
        (
            "asymmetric_context",
            ["-B2", "-A1"],
            "src/a.py:1:alpha alpha\nsrc/a.py:2-beta\ntests/b.PY:1:alpha",
        ),
        ("flags_separate", ["-n", "-H", "--no-heading", "--color=never"], normal),
        ("fixed", ["-F"], normal),
        ("word", ["-w"], normal),
        ("line", ["-x"], "tests/b.PY:1:alpha"),
        ("overrides", ["-i", "-s", "-F", "--no-fixed-strings"], normal),
        ("unrestricted", ["-uuu"], normal),
        (
            "ignore_controls",
            [
                "--no-ignore",
                "--ignore",
                "--no-ignore-parent",
                "--no-ignore-global",
                "--no-ignore-exclude",
                "--no-ignore-dot",
                "--no-ignore-vcs",
                "--no-ignore-files",
            ],
            normal,
        ),
        ("scope_controls", ["--no-hidden", "--follow", "--one-file-system"], normal),
        ("unicode", ["--no-unicode", "--unicode"], normal),
        ("encoding", ["-Eutf-8", "--crlf"], normal),
        ("early_stop", ["-m1", "--stop-on-nonmatch"], normal),
        ("diagnostics", ["--stats", "--debug"], normal),
    ):
        add(name, {**base, "options": options}, content)
    for name, pattern, options, content in (
        ("ignore_case", "upper", ["-i"], "src/a.py:3:UPPER"),
        ("smart_case", "upper", ["-S"], "src/a.py:3:UPPER"),
        ("smart_case_upper", "ALPHA", ["-S"], "No results."),
        ("inversion", "alpha", ["-v"], "other.txt:1:omega\nsrc/a.py:2:beta\nsrc/a.py:3:UPPER"),
        ("multiline", "alpha alpha\\nbeta", ["-U"], "src/a.py:1:alpha alpha\nbeta"),
        ("dotall", "alpha.*beta", ["-U", "--multiline-dotall"], "src/a.py:1:alpha alpha\nbeta"),
        ("pcre", "alpha(?= alpha)", ["-P", "-o"], "src/a.py:1:1:alpha"),
        ("auto_engine", "alpha(?= alpha)", ["--engine=auto", "-o"], "src/a.py:1:1:alpha"),
    ):
        add(name, {"action": "content", "patterns": [pattern], "options": options}, content)
    add("types_help", {"action": "paths", "options": ["--type-list"]}, contains="py:")
    add("exists", {**base, "options": ["-q"]}, matched=True)
    add("absent", {"action": "content", "patterns": ["absent"], "options": ["-q"]}, matched=False)
    add(
        "repair_scalar_aliases",
        {"operation": "Content", "pattern": "alpha", "path": "tests", "limit": "1", "-n": "true"},
        "tests/b.PY:1:alpha",
    )
    add(
        "repair_legacy", {**base, "literal": "true", "ignore_case": "false", "context": "0"}, normal
    )
    error_reasons = {
        "missing_root": "not found",
        "ambiguous_alias": "conflict",
        "conflicting_case": "conflict",
        "unknown_feature": "unknown argument",
        "missing_patterns": "requires",
        "empty_paths": "at least",
        "inapplicable": "content",
        "directory_type": "kind='files'",
        "invalid_regex": "regex",
        "unknown_option": "unsupported",
    }
    for name, arguments in (
        ("missing_root", {**base, "paths": ["missing", "src"]}),
        ("ambiguous_alias", {**base, "pattern": "beta"}),
        ("conflicting_case", {**base, "options": ["-i"], "ignore_case": False}),
        ("unknown_feature", {**base, "fuzzy": True}),
        ("missing_patterns", {"action": "content"}),
        ("empty_paths", {**base, "paths": []}),
        ("inapplicable", {"action": "paths", "options": ["-i"]}),
        ("directory_type", {"action": "paths", "kind": "all", "options": ["-tpy"]}),
        ("invalid_regex", {**base, "patterns": ["["]}),
        ("unknown_option", {**base, "options": ["--pre", "program"]}),
    ):
        add(name, arguments, error=True, error_contains=error_reasons[name])
    cases.extend(
        [
            {
                "id": "natural_word_scope",
                "task": (
                    "Find the whole word alpha in Python files under src and tests; "
                    "show matching lines."
                ),
                "content": normal,
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
        ]
    )
    return cases


async def _case(adapter: Any, args: argparse.Namespace, case: dict) -> dict:
    with TemporaryDirectory(prefix="vbot-search-probe-") as directory:
        # Compare canonical paths on both sides (Windows Temp may use 8.3 names).
        root = Path(directory).resolve()
        for name, content in {
            "src/a.py": "alpha alpha\nbeta\nUPPER\n",
            "tests/b.PY": "alpha\n",
            "other.txt": "omega\n",
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
                    "so the Tool can handle them: "
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
            )
        response = adapter.normalize_response(raw, model_id=args.model)
        calls = response.get("tool_calls") or []
        if len(calls) != 1 or calls[0].get("name") != "search_files":
            return {"case": case["id"], "passed": False, "reason": "expected one search_files call"}
        arguments = calls[0].get("arguments", {})
        # Keep the evaluation confined even if a Model supplies unexpected roots.
        roots = arguments.get("paths", arguments.get("path", ["."]))
        roots = roots if isinstance(roots, list) else [roots]
        if any(
            not isinstance(p, str) or not (root / p).resolve().is_relative_to(root) for p in roots
        ):
            return {"case": case["id"], "passed": False, "reason": "outside fixture scope"}
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
            passed = passed and data.get("content") == case["content"]
        if "contains" in case:
            passed = passed and case["contains"] in data.get("content", "")
        for field in ("matched", "next_offset"):
            if field in case:
                passed = passed and data.get(field) == case[field]
        return {
            "case": case["id"],
            "passed": passed,
            "runtime_ok": result["ok"],
            **({} if passed else {"arguments": arguments, "result": result}),
        }


async def _probe_search_files(adapter: Any, args: argparse.Namespace) -> dict:
    selected = getattr(args, "search_case", "all")
    cases = [case for case in search_cases() if selected == "all" or case["id"] == selected]
    if not cases:
        raise ValueError(f"Unknown search case: {selected}")
    semaphore = asyncio.Semaphore(4)

    async def evaluate(case):
        async with semaphore:
            try:
                return await _case(adapter, args, case)
            except Exception as error:
                return {"case": case["id"], "passed": False, "reason": str(error)}

    rows = await asyncio.gather(*(evaluate(case) for case in cases))
    return {
        "scenario": "search_files",
        "cases": len(rows),
        "passed": all(row["passed"] for row in rows),
        "results": rows,
    }
