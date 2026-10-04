"""Natural tasks and independent outcome oracles; expectations never reach Models."""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from typing import Any

BASE_FILES = {
    "src/a.py": "alpha alpha\nbeta\nUPPER\n",
    "tests/b.PY": "alpha\n",
    "src/recipes.py": "# recipes\ndef add_recipe(title):\n    pass\n",
    "other.txt": "omega\n",
    "README.md": "Release tag: ORCHID-73.\n",
    "check.py": (
        "from pathlib import Path\n"
        "Path('check-result.txt').write_text('CHECK-42 passed\\n', encoding='utf-8')\n"
        "print('CHECK-42 passed')\n"
    ),
}


_INVENTORY = (
    "def calc_total(items):\n"
    "    total = 0\n"
    "    for item in items:\n"
    "        total += item.price * item.quantity\n"
    "    return total\n"
    "\n"
    "\n"
    "def report(items):\n"
    '    print("Total:", calc_total(items))\n'
    "\n"
    "\n"
    "def invoice(items, tax):\n"
    "    net = calc_total(items)\n"
    "    return net + net * tax\n"
)
_HANDLER = (
    "class Handler:\n"
    "    def handle(self, request):\n"
    '        if request.method == "GET":\n'
    "            return self.get(request)\n"
    "        return self.reject(request)\n"
)
_STYLE = (
    "# Style guide\n\n"
    "Pick one accent colour per page.\n"
    "Body text keeps the default colour.\n\n"
    "## Links\n"
    "Links use the accent colour; visited links use a muted colour.\n"
)
_SETTINGS_NAMES = ("cache", "queue", "upload", "search", "billing", "email", "export", "audit")


def _settings_module(changes: dict[str, dict[str, str]]) -> str:
    """A long module whose functions differ only in names and values."""
    blocks = []
    for index, name in enumerate(_SETTINGS_NAMES, 1):
        values = {"enabled": "True", "timeout": str(index * 10), "retries": "3"}
        values.update(changes.get(name, {}))
        blocks.append(
            f"def {name}_settings():\n"
            f'    """Return the {name} settings."""\n'
            "    return {\n"
            f'        "enabled": {values["enabled"]},\n'
            f'        "timeout": {values["timeout"]},\n'
            f'        "retries": {values["retries"]},\n'
            f'        "label": "{name}",\n'
            "    }\n"
        )
    return "# Generated defaults; edit by hand.\n\n\n" + "\n\n".join(blocks)


_MAKEFILE = ".PHONY: test lint\r\n\r\ntest:\r\n\tpytest -q\r\n\r\nlint:\r\n\truff check .\r\n"
_ROUTES = "\n\n".join(
    f"def list_{name}(request):\n"
    "    check_auth(request)\n"
    f'    rows = db.fetch("{name}")\n'
    "    return render(rows)\n"
    for name in ("users", "teams", "roles")
)


def _file_edit_cases() -> list[dict[str, Any]]:
    """Natural file-edit tasks; any offered file-edit Tool can solve them."""

    def case(case_id: str, task: str, files: dict, expected: dict, **extra: Any) -> dict:
        return {
            "id": case_id,
            "tool": "search_files",
            "expected_tool": "file_edit",
            "task": task + " Tell me when it is done.",
            "files": files,
            "expected_files": expected,
            **extra,
        }

    return [
        case(
            "edit_rename_function",
            "In src/inventory.py, rename calc_total to order_total everywhere in that file, "
            "and change the last line of invoice to `return round(net + net * tax, 2)`. "
            "Keep everything else unchanged.",
            {"src/inventory.py": _INVENTORY},
            {
                "src/inventory.py": _INVENTORY.replace("calc_total", "order_total").replace(
                    "return net + net * tax", "return round(net + net * tax, 2)"
                )
            },
        ),
        case(
            "edit_version_bump",
            "Bump the version from 1.4.2 to 1.5.0 in pyproject.toml and src/pkg/__init__.py. "
            "In CHANGELOG.md, add a section '## 1.5.0' with the line '- Add CSV export.' "
            "above the 1.4.2 section, separated from it by a blank line.",
            {
                "pyproject.toml": '[project]\nname = "pkg"\nversion = "1.4.2"\n',
                "src/pkg/__init__.py": '"""Package."""\n\n__version__ = "1.4.2"\n',
                "CHANGELOG.md": "# Changelog\n\n## 1.4.2\n- Fix login timeout.\n",
            },
            {
                "pyproject.toml": '[project]\nname = "pkg"\nversion = "1.5.0"\n',
                "src/pkg/__init__.py": '"""Package."""\n\n__version__ = "1.5.0"\n',
                "CHANGELOG.md": "# Changelog\n\n## 1.5.0\n- Add CSV export.\n\n"
                "## 1.4.2\n- Fix login timeout.\n",
            },
        ),
        case(
            "edit_second_section",
            "In config.ini, change the replica's port to 5433. Leave the primary unchanged.",
            {
                "config.ini": "[primary]\nhost = localhost\nport = 5432\n\n"
                "[replica]\nhost = localhost\nport = 5432\n"
            },
            {
                "config.ini": "[primary]\nhost = localhost\nport = 5432\n\n"
                "[replica]\nhost = localhost\nport = 5433\n"
            },
        ),
        case(
            "edit_indented_insert",
            "In src/handlers.py, make handle route POST requests to self.post(request): "
            'directly after the GET branch, add `if request.method == "POST":` returning '
            "self.post(request), in the same style as the GET branch.",
            {"src/handlers.py": _HANDLER},
            {
                "src/handlers.py": _HANDLER.replace(
                    "        return self.reject",
                    '        if request.method == "POST":\n'
                    "            return self.post(request)\n"
                    "        return self.reject",
                )
            },
        ),
        case(
            "edit_every_occurrence",
            "In docs/style.md, use American spelling: replace every 'colour' with 'color'.",
            {"docs/style.md": _STYLE},
            {"docs/style.md": _STYLE.replace("colour", "color")},
        ),
        case(
            "create_module",
            "Create src/pkg/constants.py with exactly two lines: `MAX_RETRIES = 3` and "
            "`TIMEOUT_SECONDS = 30`.",
            {},
            {"src/pkg/constants.py": "MAX_RETRIES = 3\nTIMEOUT_SECONDS = 30\n"},
            loose_final_newline=["src/pkg/constants.py"],
        ),
        case(
            "rewrite_file",
            "Replace the entire content of todo.md with three list items, one per line: "
            "'- buy milk', '- call Sam', '- book flights'.",
            {"todo.md": "# Todo\n\n- renew passport\n- water plants\n"},
            {"todo.md": "- buy milk\n- call Sam\n- book flights\n"},
            loose_final_newline=["todo.md"],
        ),
        case(
            "edit_scattered_long",
            "In src/settings.py, set retries to 5 in queue_settings and email_settings, "
            "set timeout to 90 in export_settings, and set enabled to False in "
            "audit_settings. Keep everything else unchanged.",
            {"src/settings.py": _settings_module({})},
            {
                "src/settings.py": _settings_module(
                    {
                        "queue": {"retries": "5"},
                        "email": {"retries": "5"},
                        "export": {"timeout": "90"},
                        "audit": {"enabled": "False"},
                    }
                )
            },
        ),
        case(
            "edit_tabs_crlf",
            "In Makefile, change the test target's command from `pytest -q` to `pytest -q -x`.",
            {"Makefile": _MAKEFILE},
            {"Makefile": _MAKEFILE.replace("pytest -q", "pytest -q -x")},
        ),
        case(
            "edit_similar_blocks",
            "In src/routes.py, make list_teams return render(rows, page_size=50) instead of "
            "render(rows). Leave the other functions unchanged.",
            {"src/routes.py": _ROUTES},
            {
                "src/routes.py": _ROUTES.replace(
                    'db.fetch("teams")\n    return render(rows)',
                    'db.fetch("teams")\n    return render(rows, page_size=50)',
                )
            },
        ),
    ]


def first_use_cases() -> list[dict[str, Any]]:
    return [
        {
            "id": "search_symbols",
            "tool": "search_files",
            "task": (
                "Find lines containing add_recipe( in Python files under src. "
                "Include file paths and line numbers."
            ),
            "rows": {"src/recipes.py:2:def add_recipe(title):"},
        },
        {
            "id": "search_word",
            "tool": "search_files",
            "task": (
                "Find the whole word alpha in Python files under src and tests. "
                "Show the matching lines with their locations."
            ),
            "rows": {"src/a.py:1:alpha alpha", "tests/b.PY:1:alpha"},
        },
        {
            "id": "search_names",
            "tool": "search_files",
            "task": (
                "List every Python file below this directory, ignoring "
                "capitalization of the filename extension. Return paths only."
            ),
            "rows": {"check.py", "src/a.py", "src/recipes.py", "tests/b.PY"},
        },
        {
            "id": "search_dirs",
            "tool": "search_files",
            "task": (
                "Find directories named empty anywhere below this directory, "
                "including ones containing no files."
            ),
            "rows": {"src/empty/"},
        },
        {
            "id": "search_count",
            "tool": "search_files",
            "task": (
                "How many individual occurrences of alpha does each file contain? "
                "Return per-file occurrence counts; two on the same line count "
                "twice."
            ),
            "rows": {"src/a.py:2", "tests/b.PY:1"},
        },
        {
            "id": "search_absent",
            "tool": "search_files",
            "task": (
                "Find references to retired_handler anywhere under src and report "
                "their locations, or tell me if there are none."
            ),
            "rows": {"No results."},
        },
        {
            "id": "search_pages",
            "tool": "search_files",
            "task": (
                "List every EVENT line in events.log, with line numbers. I need the complete list."
            ),
            "files": {"events.log": "".join(f"EVENT item-{i:03}\n" for i in range(1, 136))},
            "rows": {f"events.log:{i}:EVENT item-{i:03}" for i in range(1, 136)},
        },
        {
            "id": "search_other_cwd",
            "tool": "search_files",
            "outside_cwd": True,
            "task": (
                "The repository is {repo}. Find lines containing add_recipe( in its "
                "src directory, with file paths and line numbers."
            ),
            "rows": {"src/recipes.py:2:def add_recipe(title):"},
        },
        {
            "id": "read_control",
            "tool": "search_files",
            "expected_tool": "read",
            "task": "Read README.md and tell me the release tag.",
            "final_contains": "ORCHID-73",
        },
        {
            "id": "search_hidden_config",
            "tool": "search_files",
            "task": (
                "Find where cache_timeout is configured in this directory, including dotfiles. "
                "Show matching lines with paths and line numbers."
            ),
            "files": {".settings.ini": "cache_timeout = 37\n"},
            "rows": {".settings.ini:1:cache_timeout = 37"},
        },
        {
            "id": "search_exclusions",
            "tool": "search_files",
            "task": (
                "Find every ERROR line in this directory except those under vendor. "
                "Include file paths and line numbers."
            ),
            "files": {"app.log": "ERROR startup\n", "vendor/build.log": "ERROR external\n"},
            "rows": {"app.log:1:ERROR startup"},
            "final_excludes": ["ERROR external"],
        },
        {
            "id": "edit_control",
            "tool": "search_files",
            "expected_tool": "file_edit",
            "task": (
                "In notes.md, move the 'Validation' section below 'Release', keeping the "
                "section text and everything else unchanged. Tell me when it is done."
            ),
            "files": {
                "notes.md": "# Notes\n\n## Validation\nRun the smoke check.\n\n"
                "## Release\nPublish the signed build.\n"
            },
            "expected_files": {
                "notes.md": "# Notes\n\n## Release\nPublish the signed build.\n\n"
                "## Validation\nRun the smoke check.\n"
            },
        },
        *_file_edit_cases(),
        {
            "id": "shell_control",
            "tool": "search_files",
            "expected_tool": "bash",
            "task": (
                "Run python check.py in this directory and tell me its output and "
                "whether it exited successfully."
            ),
            "final_contains": "CHECK-42",
            "expected_files": {"check-result.txt": "CHECK-42 passed\n"},
        },
        {
            "id": "delegate_self",
            "tool": "subagent",
            "task": (
                "Delegate an independent read-only review of src/recipes.py to a "
                "copy of yourself, using your Agent configuration. Ask for findings "
                "with line numbers, and include reference REVIEW-17 in the brief. "
                "Confirm dispatch; do not do the review yourself."
            ),
            "brief": ["src/recipes.py", "REVIEW-17"],
            "agents": ["parent"],
        },
        {
            "id": "delegate_named",
            "tool": "subagent",
            "task": (
                "Have reviewer independently check src/a.py for errors, read-only. "
                "Include reference REVIEW-29 and ask for a concise report. Confirm "
                "that you dispatched it."
            ),
            "brief": ["src/a.py", "REVIEW-29"],
            "agents": ["reviewer"],
        },
        {
            "id": "delegate_parallel",
            "tool": "subagent",
            "task": (
                "Delegate two separate read-only reviews to reviewer: src/a.py with "
                "reference REVIEW-A, and tests/b.PY with reference REVIEW-B. Each "
                "review should return findings with line numbers. Confirm both "
                "dispatches; do not review the files yourself."
            ),
            "briefs": [["src/a.py", "REVIEW-A"], ["tests/b.PY", "REVIEW-B"]],
            "agents": ["reviewer", "reviewer"],
        },
        {
            "id": "delegate_nested",
            "tool": "subagent",
            "nested": True,
            "task": (
                "Delegate a read-only check of src/recipes.py to reviewer, include "
                "reference REVIEW-31, and relay its result."
            ),
            "brief": ["src/recipes.py", "REVIEW-31"],
            "agents": ["reviewer"],
            "final_contains": "CHECK-42",
        },
        {
            "id": "delegate_status",
            "tool": "subagent",
            "seed": True,
            "task": (
                "What is the current state of the review you dispatched? Check once "
                "and tell me; keep it running."
            ),
            "operation": "status",
        },
        {
            "id": "delegate_cancel",
            "tool": "subagent",
            "seed": True,
            "task": "Cancel the review you dispatched. Confirm when it has stopped.",
            "operation": "cancel",
        },
        {
            "id": "delegate_continue",
            "tool": "subagent",
            "seed": True,
            "task": (
                "Send the same reviewer Session a follow-up: also inspect "
                "tests/b.PY, read-only, and include reference FOLLOWUP-53. Keep its "
                "existing context."
            ),
            "brief": ["tests/b.PY", "FOLLOWUP-53"],
            "agents": ["reviewer"],
            "continuation": True,
        },
        {
            "id": "delegate_unknown",
            "tool": "subagent",
            "task": (
                "Delegate a read-only review to auditor-missing. If that Agent is "
                "unavailable, tell me instead of choosing a replacement."
            ),
            "unavailable": True,
        },
    ]


def assess(
    case: dict, fixture: Any, calls: list[dict], final: str, initial_received: int
) -> tuple[bool, dict]:
    """Judge effects, not JSON identity or a fixed number/order of Tool calls."""
    successful = [c for c in calls if (c.get("result") or {}).get("ok")]
    details: dict[str, Any] = {}
    if case["tool"] == "search_files":
        expected_tool = case.get("expected_tool", "search_files")
        expected_names = (
            {"apply_patch", "edit", "write"} if expected_tool == "file_edit" else {expected_tool}
        )
        selected = [c for c in successful if c["name"] in expected_names]
        if "rows" in case:
            actual = set()
            raw_lines = []
            for call in selected:
                content = call["result"]["data"].get("content", "")
                content = content.replace(fixture.repo.as_posix() + "/", "")
                raw_lines.extend(content.splitlines())
                for line in content.splitlines():
                    line = line.removeprefix("./")
                    # --column is a valid alternative presentation of matching lines.
                    line = re.sub(r"^(.*?:\d+):\d+:(?=\D)", r"\1:", line)
                    actual.add(line)
            # Reading a known file is also valid evidence for matching-line tasks.
            # The oracle checks the requested facts, not an exact Tool sequence.
            for call in successful:
                if call["name"] == "bash":
                    # Only these full-file reads are allowed by this fixture;
                    # arbitrary script output is not evidence of reading a file.
                    command = call["arguments"].get("command", "")
                    match = re.search(r"(?:README\.md|src[/\\]recipes\.py)", command, re.I)
                    if match and command.lower().startswith(("get-content ", "cat ")):
                        label = match.group().replace("\\", "/")
                        for number, line in enumerate(
                            call["result"]["data"].get("output", "").splitlines(), 1
                        ):
                            actual.add(f"{label}:{number}:{line}")
                    continue
                if call["name"] != "read":
                    continue
                path = (fixture.cwd / call["arguments"]["path"]).resolve()
                if not path.is_relative_to(fixture.repo):
                    continue
                label = path.relative_to(fixture.repo).as_posix()
                for line in call["result"]["data"].get("content", "").splitlines():
                    match = re.match(r"(\d+)\| (.*)", line)
                    if match:
                        actual.add(f"{label}:{match[1]}:{match[2]}")
            if (
                case["id"] == "search_count"
                and raw_lines
                and all(re.match(r"^.+?:\d+(?::\d+)?:alpha$", line) for line in raw_lines)
            ):
                counts = Counter(re.sub(r":\d+(?::\d+)?:alpha$", "", line) for line in raw_lines)
                actual = {f"{path}:{count}" for path, count in counts.items()}
            outcome = case["rows"].issubset(actual)
            if case["id"] == "search_absent":
                # Empty output alone proves nothing about the intended query/scope.
                scope = fixture.repo / "src"
                absence_verified = False
                for call in selected:
                    data = call["result"]["data"]
                    try:
                        covers_pattern = any(
                            re.search(pattern, "retired_handler")
                            for pattern in data.get("patterns", [])
                        )
                    except re.error:
                        covers_pattern = False
                    covers_scope = any(
                        scope.is_relative_to(Path(root)) for root in data.get("searched_paths", [])
                    )
                    absence_verified |= bool(
                        covers_pattern
                        and covers_scope
                        and data.get("complete")
                        and (data.get("matched") is False or data.get("content") == "No results.")
                    )
                outcome = absence_verified
            answer = final.replace("\\", "/").replace("`", "")
            final_ok = True
            for row in case["rows"]:
                if row == "No results.":
                    final_ok &= bool(
                        re.search(
                            r"\bno\b|not found|none|zero|0 (matches|references)", answer, re.I
                        )
                    )
                elif case["id"] == "search_count":
                    path, count = row.rsplit(":", 1)
                    final_ok &= bool(
                        re.search(re.escape(path) + r"[^\n]*\b" + count + r"\b", answer)
                    )
                else:
                    path, *rest = row.split(":", 2)
                    final_ok &= path.rstrip("/") in answer
                    if len(rest) == 2:
                        final_ok &= (
                            bool(re.search(r"\b" + rest[0] + r"\b", answer)) and rest[1] in answer
                        )
            outcome = outcome and final_ok
            outcome = outcome and all(
                value not in final for value in case.get("final_excludes", [])
            )
            details = {
                "missing_rows": sorted(case["rows"] - actual),
                "unexpected_rows": sorted(actual - case["rows"]),
            }
            details["final_facts_verified"] = final_ok
        elif "final_contains" in case:
            fact = case["final_contains"]
            evidence = [
                c
                for c in successful
                if fact in str(c["result"]["data"].get("content", ""))
                or fact in str(c["result"]["data"].get("output", ""))
            ]
            outcome = bool(evidence) and fact in final
            if expected_tool == "bash":
                outcome = outcome and any(
                    c["result"]["data"].get("exit_code") == 0 for c in evidence
                )
        else:
            # The exact durable file comparison below decides edit success.
            outcome = bool(final.strip())
        outcome = outcome and bool(final.strip())
        if expected_tool == "search_files" and fixture.received:
            outcome = False
            details["unnecessary_delegation"] = True
        before = {**BASE_FILES, **case.get("files", {})}
        expected_files = {**before, **case.get("expected_files", {})}
        actual_files = {
            # Exact bytes: an edit that changes line endings does not pass.
            path.relative_to(fixture.repo).as_posix(): path.read_bytes().decode("utf-8")
            for path in fixture.repo.rglob("*")
            if path.is_file()
        }
        for name in case.get("loose_final_newline", []):
            if name in actual_files:
                actual_files[name] = actual_files[name].rstrip("\n") + "\n"
        differences = sorted(
            name
            for name in expected_files.keys() | actual_files.keys()
            if expected_files.get(name) != actual_files.get(name)
        )
        details.update(
            preferred_tool_used=bool(selected),
            shell_calls=sum(c["name"] == "bash" for c in calls),
            tool_choices=[c["name"] for c in calls],
            state_differences=differences,
            changed_files={
                name: content
                for name, content in actual_files.items()
                if before.get(name) != content
            },
        )
        outcome = outcome and not differences
    else:
        received = fixture.received[initial_received:]
        operation = case.get("operation")
        if operation:
            matching = [
                c
                for c in successful
                if c["name"] == "subagent" and c["arguments"].get("action") == operation
            ]
            outcome = bool(matching) and not received
            if operation == "cancel":
                outcome = outcome and fixture.seed_run.status.value == "cancelled"
            else:
                outcome = outcome and fixture.seed_run.status.value == "running"
        elif case.get("unavailable"):
            outcome = (
                not received
                and bool(final.strip())
                and bool(
                    re.search(
                        r"unavailable|not available|not.*(available|found|exist)|cannot|can't",
                        final,
                        re.I,
                    )
                )
            )
        else:
            briefs = case.get("briefs", [case.get("brief", [])])
            outcome = sorted(r["agent_id"] for r in received) == sorted(case["agents"])
            for brief in briefs:
                outcome = outcome and any(
                    all(value in r["content"] for value in brief)
                    and bool(
                        re.search(
                            r"read[- ]only|do not (modify|edit)|no (changes|edits)",
                            r["content"],
                            re.I,
                        )
                    )
                    for r in received
                )
            if case.get("continuation"):
                # Busy continuation is queued; its receiving Run executes after release.
                outcome = outcome and all(
                    r["session_id"] == fixture.seed_result["data"]["session_id"] for r in received
                )
            if "final_contains" in case:
                outcome = outcome and case["final_contains"] in final
            outcome = outcome and bool(final.strip())
        details["receiving_runs"] = received
    return bool(outcome), details
