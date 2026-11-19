"""Natural tasks and independent outcome oracles; expectations never reach Models."""

from __future__ import annotations

import re
from typing import Any

from core.tools._background_tasks_arguments import normalize_background_tasks_arguments

BASE_FILES = {
    "src/a.py": "alpha alpha\nbeta\nUPPER\n",
    "tests/b.PY": "alpha\n",
    "src/recipes.py": "# recipes\ndef add_recipe(title):\n    pass\n",
    "other.txt": "omega\n",
    "README.md": "Release tag: ORCHID-73.\n",
    "check.py": (
        "from pathlib import Path\n"
        "Path('check-result.txt').write_bytes(b'CHECK-42 passed\\n')\n"
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
            "tool": "file_edit",
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
        ),
        case(
            "rewrite_file",
            "Replace the entire content of todo.md with three list items, one per line: "
            "'- buy milk', '- call Sam', '- book flights'.",
            {"todo.md": "# Todo\n\n- renew passport\n- water plants\n"},
            {"todo.md": "- buy milk\n- call Sam\n- book flights\n"},
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
            "id": "read_control",
            "tool": "read",
            "task": "Read README.md and tell me the release tag.",
            "final_contains": "ORCHID-73",
        },
        {
            "id": "edit_control",
            "tool": "file_edit",
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
            "tool": "bash",
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
        # A dispatched Sub-Agent is checked and stopped with background_tasks.
        {
            "id": "delegate_status",
            "tool": "subagent",
            "expected_tool": "background_tasks",
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
            "expected_tool": "background_tasks",
            "seed": True,
            "task": "Cancel the review you dispatched. Confirm when it has stopped.",
            "operation": "stop",
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


def _background_tasks_action(arguments: Any) -> str | None:
    """The action a ``background_tasks`` call meant, read as the Tool reads it."""
    try:
        normalized = normalize_background_tasks_arguments(arguments)
    except ValueError:
        return None
    return normalized.get("action") if isinstance(normalized, dict) else None


def preferred_tools(case: dict) -> set[str]:
    """Tool names whose successful calls carry the task's evidence or effect."""
    expected_tool = case.get("expected_tool", case["tool"])
    return {"apply_patch", "edit", "write"} if expected_tool == "file_edit" else {expected_tool}


def assess(
    case: dict, fixture: Any, calls: list[dict], final: str, initial_received: int
) -> tuple[bool, dict]:
    """Judge effects, not JSON identity or a fixed number/order of Tool calls."""
    successful = [c for c in calls if (c.get("result") or {}).get("ok")]
    details: dict[str, Any] = {}
    if case["tool"] != "subagent":
        expected_tool = case["tool"]
        expected_names = preferred_tools(case)
        selected = [c for c in successful if c["name"] in expected_names]
        if "final_contains" in case:
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
        before = {**BASE_FILES, **case.get("files", {})}
        expected_files = {**before, **case.get("expected_files", {})}
        actual_files = {
            # Exact bytes: an edit that changes line endings does not pass.
            path.relative_to(fixture.repo).as_posix(): path.read_bytes().decode("utf-8")
            for path in fixture.repo.rglob("*")
            if path.is_file()
        }
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
            # Listing counts as checking; a subagent call run as background_tasks does not.
            actions = {"status": {"list", "status"}, "stop": {"stop"}}[operation]
            matching = [
                c
                for c in successful
                if c["name"] == "background_tasks"
                and _background_tasks_action(c["arguments"]) in actions
            ]
            details["subagent_calls_run_as_background_tasks"] = sum(
                c["name"] == "subagent" and "This call ran as background_tasks" in str(c["result"])
                for c in successful
            )
            outcome = bool(matching) and not received
            if operation == "stop":
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
            outcome = outcome and bool(final.strip())
        details["receiving_runs"] = received
    return bool(outcome), details
