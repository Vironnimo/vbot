"""Provider Tool probe: workflow terminal."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from core.providers.tool_schema import render_tool_definitions
from core.tools.contracts import ToolContractError
from scripts.provider_probe.common import PROJECT_ROOT


def _terminal_cases() -> list[dict[str, Any]]:
    from core.tools.terminal_manager import (
        TERMINAL_MAX_COLUMNS,
        TERMINAL_MAX_ROWS,
        TERMINAL_MIN_COLUMNS,
        TERMINAL_MIN_ROWS,
    )

    cases = []

    def add(case_id, action, error=None, **fields):
        arguments = {"action": action, **fields}
        if action not in {"start", "list"}:
            arguments["terminal_id"] = "{terminal_id}"
        cases.append({"id": case_id, "arguments": arguments, "error": error})

    add("start_default", "start")
    add("start_command", "start", command="fixture")
    add("start_args", "start", command="fixture", args=["--label", "two words"])
    add("start_text", "start", command="fixture", text="first task")
    for name, value in (("relative", "."), ("absolute", "{root}"), ("project", "project:probe")):
        add("start_workdir_" + name, "start", command="fixture", workdir=value)
    add("start_name", "start", command="fixture", name="Review")
    add("start_group", "start", command="fixture", group="Review")
    add("list", "list")
    add("attach", "attach")
    add("attach_idempotent", "attach")
    add("detach", "detach")
    add("status_default", "status")
    add("status_lines_min", "status", lines=1)
    add("status_lines_max", "status", lines=100)
    add("status_page_default", "status", start_line=0)
    add("status_page_later", "status", start_line=3, lines=2)
    add("wait_default", "wait")
    add("wait_revision", "wait", after_revision=0)
    add("wait_timeout", "wait", after_revision=999, timeout_ms=0)
    add("wait_max", "wait", timeout_ms=10000)
    add("input_noop", "input")
    add("input_text", "input", text="hello")
    add("input_submit", "input", text="hello", key="enter")
    add("input_multiline", "input", text="one\ntwo", key="enter")
    add("input_data", "input", data="\x1b[2~")
    for key in ("enter", "up", "shift_tab", "f12", "ctrl_c"):
        add("input_key_" + key, "input", key=key)
    add("input_guard", "input", text="y", expected_screen_revision="{revision}")
    add("resize_min", "resize", columns=TERMINAL_MIN_COLUMNS, rows=TERMINAL_MIN_ROWS)
    add("resize_max", "resize", columns=TERMINAL_MAX_COLUMNS, rows=TERMINAL_MAX_ROWS)
    add("kill", "kill")
    add("invalid_fields", "list", "invalid_arguments", text="extra")
    add("invalid_combination", "input", "invalid_arguments", data="x", text="y")
    add("invalid_resize", "resize", "invalid_arguments", columns=80)
    add("invalid_stale", "input", "stale_screen", text="y", expected_screen_revision=999)
    add("invalid_project", "start", "project_not_found", workdir="project:missing")
    add("invalid_args", "start", "invalid_arguments", args=["orphan"])
    add("invalid_owner", "input", "terminal_not_owned", text="y")
    cases.extend(
        [
            {
                "id": "natural_start",
                "task": (
                    "Open the default interactive shell so I can inspect it before typing anything."
                ),
                "expected": {"action": "start"},
            },
            {
                "id": "natural_submit",
                "task": "Type hello into the terminal and submit it.",
                "expected": {
                    "action": "input",
                    "text": "hello",
                    "key": "enter",
                    "terminal_id": "{terminal_id}",
                },
            },
            {
                "id": "natural_history",
                "task": "Read the first 10 lines of the retained terminal buffer.",
                "expected": {
                    "action": "status",
                    "start_line": 0,
                    "lines": 10,
                    "terminal_id": "{terminal_id}",
                },
            },
            {
                "id": "natural_notice",
                "task": (
                    "The terminal asks whether to proceed with the fixture check. "
                    "Answer y and submit it."
                ),
                "expected": {
                    "action": "input",
                    "text": "y",
                    "key": "enter",
                    "expected_screen_revision": "{revision}",
                    "terminal_id": "{terminal_id}",
                },
            },
        ]
    )
    cases.append(
        {
            "id": "natural_continue",
            "reference": "codex.md",
            "task": (
                'Continue the existing task in terminal {terminal_id}: send "hello" and submit it.'
            ),
            "expected": {
                "action": "input",
                "terminal_id": "{terminal_id}",
                "text": "hello",
                "key": "enter",
            },
        }
    )
    return cases


async def _probe_terminal_case(
    adapter: Any, args: argparse.Namespace, case: dict[str, Any]
) -> dict[str, Any]:
    # Reuse the existing disposable PTY fixture; Model output never launches a host program.
    from core.projects import ProjectStore
    from core.tools._terminal_events import _attention_body
    from core.tools._terminal_input import _input_chunks
    from core.tools.terminal import register_terminal_tool
    from core.tools.terminal_manager import TerminalManager
    from core.tools.tools import ToolRegistry
    from core.utils.tokens import estimate_json_tokens
    from tests.core.tools.terminal_helpers import make_context
    from tests.core.tools.terminal_manager_helpers import (
        AdapterFactory,
        PendingTriggerService,
        eventually,
        owner,
    )

    with TemporaryDirectory(prefix="vbot-terminal-probe-") as directory:
        root = Path(directory)
        factory = AdapterFactory()
        trigger = PendingTriggerService()
        manager = TerminalManager(trigger, adapter_factory=factory, activity_quiet_seconds=0.03)
        manager.start()
        projects = ProjectStore(root / "data")
        projects.create("probe", "Probe", root)
        registry = ToolRegistry()
        register_terminal_tool(registry, manager, projects)
        context = make_context(root)
        definitions = registry.provider_definitions(allowed_tools=["terminal"])
        try:
            seed = await registry.dispatch(
                context,
                {"action": "start", "command": case.get("expected", {}).get("command", "codex")},
                ["terminal"],
            )
            terminal_id = seed["data"]["terminal_id"]
            session = manager.get_session(terminal_id, owner())
            await manager.send_operator_input(terminal_id, "open")
            factory.adapters[0].emit(
                "Ready for next instruction> "
                if case["id"] == "natural_continue"
                else "fixture history\r\nProceed with fixture check? [y/n]"
            )
            await eventually(lambda: session.attention is not None)
            assert session.attention is not None
            revision = session.renderer.revision
            expected = dict(case.get("arguments", case.get("expected", {})))
            for key, value in expected.items():
                if value == "{terminal_id}":
                    expected[key] = terminal_id
                elif value == "{revision}":
                    expected[key] = revision
                elif value == "{root}":
                    expected[key] = root.as_posix()
            if case["id"] in {"attach", "invalid_owner"}:
                manager.detach(terminal_id, owner())
            if "task" in case:
                skill = (PROJECT_ROOT / "resources/skills/coding-agents/SKILL.md").read_text(
                    encoding="utf-8"
                )
                if case.get("reference"):
                    skill += "\n\n" + (
                        PROJECT_ROOT
                        / "resources/skills/coding-agents/references"
                        / case["reference"]
                    ).read_text(encoding="utf-8")
                observation = (
                    _attention_body(session, session.attention)
                    if case["id"] == "natural_notice"
                    else json.dumps(await manager.snapshot(terminal_id, owner()))
                )
                messages = [
                    {"role": "system", "content": skill},
                    {
                        "role": "user",
                        "content": (
                            "Previous terminal observation:\n"
                            + observation
                            + "\nUser request:\n"
                            + case["task"]
                            .replace("{root}", root.as_posix())
                            .replace("{terminal_id}", terminal_id)
                        ),
                    },
                ]
                if case["id"] == "natural_start":
                    # Opening a host shell does not activate the coding-agent playbook.
                    messages = [{"role": "user", "content": case["task"]}]
                elif case.get("reference"):
                    # Keep the earlier terminal's observation in Tool history, separate
                    # from the new user request, as in an actual ongoing conversation.
                    read_arguments = {"action": "status", "terminal_id": terminal_id}
                    observed = await registry.dispatch(context, read_arguments, ["terminal"])
                    messages = [
                        {"role": "system", "content": skill},
                        {"role": "user", "content": "Show the terminal for my earlier task."},
                        {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "previous_terminal",
                                    "name": "terminal",
                                    "arguments": read_arguments,
                                }
                            ],
                        },
                        {
                            "role": "tool",
                            "tool_call_id": "previous_terminal",
                            "name": "terminal",
                            "content": json.dumps(observed),
                        },
                        {
                            "role": "user",
                            "content": case["task"]
                            .replace("{root}", root.as_posix())
                            .replace("{terminal_id}", terminal_id),
                        },
                    ]
            else:
                messages = [
                    {
                        "role": "system",
                        "content": (
                            "Exercise exactly one Tool Call using the supplied test arguments "
                            "verbatim, including intentional invalid fields. Do not repair them "
                            "or add optional fields."
                        ),
                    },
                    {"role": "user", "content": json.dumps(expected)},
                ]
            async with asyncio.timeout(args.total_timeout):
                raw = await adapter.send(
                    messages,
                    model_id=args.model,
                    tools=definitions,
                    thinking_effort=args.thinking_effort,
                    max_tokens=args.max_tokens or 2500,
                )
            response = adapter.normalize_response(raw, model_id=args.model)
            calls = response.get("tool_calls") or []
            valid = len(calls) == 1 and calls[0].get("name") == "terminal"
            actual = calls[0].get("arguments", {}) if valid else {}
            # A guard is also a valid conservative choice for natural plain input.
            compared = dict(actual)
            if (
                case["id"] in {"natural_submit", "natural_continue"}
                and compared.get("expected_screen_revision") == revision
            ):
                compared.pop("expected_screen_revision")
            if case["id"] == "natural_start":
                compared.pop("name", None)
                if compared.get("args") == []:
                    compared.pop("args")
            valid = valid and compared == expected
            writes_before = list(factory.adapters[0].writes)
            launches_before = len(factory.calls)
            result = None
            effect_ok = False
            if valid:
                try:
                    result = await registry.dispatch(context, actual, ["terminal"])
                except ToolContractError:
                    result = {"ok": False, "error": {"code": "invalid_arguments"}}
                data = result.get("data") or {}
                if case.get("error"):
                    effect_ok = (
                        factory.adapters[0].writes == writes_before
                        and len(factory.calls) == launches_before
                    )
                elif actual["action"] == "input":
                    chunks = _input_chunks(
                        data=actual.get("data"), text=actual.get("text"), key=actual.get("key")
                    )
                    effect_ok = factory.adapters[0].writes == writes_before + list(
                        chunks
                    ) and data.get("characters_sent") == sum(map(len, chunks))
                elif actual["action"] == "start":
                    child = manager.get_session(data["terminal_id"], owner())
                    if actual.get("text"):
                        child.renderer.feed("ready> ")
                        await eventually(
                            lambda: (
                                bool(factory.adapters[-1].writes)
                                and factory.adapters[-1].writes[-1] == "\r"
                            )
                        )
                    effect_ok = (
                        len(factory.calls) == launches_before + 1
                        and child.attachment == owner()
                        and child.cwd == root
                    )
                    if "command" in actual:
                        effect_ok = effect_ok and factory.calls[-1][0] == [
                            actual["command"],
                            *actual.get("args", []),
                        ]
                    effect_ok = effect_ok and child.name == actual.get("name")
                    if actual.get("text"):
                        effect_ok = effect_ok and factory.adapters[-1].writes == [
                            actual["text"],
                            "\r",
                        ]
                    if actual.get("group"):
                        effect_ok = effect_ok and child.group_id is not None
                elif actual["action"] == "status":
                    effect_ok = ("screen" in data) == ("start_line" not in actual)
                    if "start_line" in actual:
                        effect_ok = effect_ok and data["scrollback"]["line_count"] <= actual.get(
                            "lines", 30
                        )
                elif actual["action"] == "wait":
                    effect_ok = (
                        data["timed_out"] == (case["id"] == "wait_timeout")
                        and session.adapter.is_alive()
                    )
                elif actual["action"] == "resize":
                    effect_ok = factory.adapters[0].resizes[-1] == (
                        actual["rows"],
                        actual["columns"],
                    )
                elif actual["action"] == "attach":
                    effect_ok = session.attachment == owner()
                elif actual["action"] == "detach":
                    effect_ok = session.attachment is None and session.adapter.is_alive()
                elif actual["action"] == "kill":
                    effect_ok = not session.adapter.is_alive() and data["state"] == "exited"
                else:
                    effect_ok = data["terminals"][0]["terminal_id"] == terminal_id
            code = ((result or {}).get("error") or {}).get("code")
            rendered = render_tool_definitions(definitions, profile="explicit_non_strict")
            passed = (
                valid
                and result is not None
                and bool(result["ok"]) == (case.get("error") is None)
                and code == case.get("error")
                and effect_ok
            )
            return {
                "case": case["id"],
                "passed": passed,
                "model_call_valid": valid,
                "effect_ok": effect_ok,
                "error_code": code,
                "calls": len(calls),
                "definition_tokens": estimate_json_tokens(definitions[0])[0],
                "non_strict": all(t.get("strict") is False for t in rendered),
            }
        finally:
            await manager.aclose()


async def _probe_terminal(adapter: Any, args: argparse.Namespace) -> dict[str, Any]:
    from core.tools import terminal

    expected_source = PROJECT_ROOT / "core/tools/terminal.py"
    if Path(terminal.__file__).resolve() != expected_source:
        raise RuntimeError("Terminal probe imported a different checkout")
    cases = [
        case
        for case in _terminal_cases()
        if args.terminal_case == "all" or case["id"] == args.terminal_case
    ]
    if not cases:
        raise ValueError("Unknown terminal case")
    limit = asyncio.Semaphore(4)

    async def evaluate(case):
        async with limit:
            return await _probe_terminal_case(adapter, args, case)

    rows = await asyncio.gather(*(evaluate(case) for case in cases))
    return {
        "scenario": "terminal",
        "model": args.model,
        "cases": rows,
        "passed": all(row["passed"] for row in rows),
    }
