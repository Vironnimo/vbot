"""Dispatch Tool calls through the production path and show what the Agent gets back.

A case file seeds a Workspace and lists cases. Every case runs in a fresh copy
of that Workspace, through the production executor with the Agent's compiled
input contracts, so argument repair, validation, the handler and the result
envelope behave as in a Run. The output shows each call, the Model-visible
result with its token estimate, and the files a case names after the calls.

Case file (JSON)::

    {
      "files": {
        "src/app.py": "exact text; \\r\\n in the JSON gives CRLF",
        "README.md": {"repeat": "line {n}", "count": 3000, "newline": "\\n"}
      },
      "cases": [
        {"name": "read with an alias", "tool": "read", "arguments": {"file_path": "src/app.py"},
         "expect": {"ok": true, "contains": ["def main"]}},
        {"name": "background then status",
         "steps": [
           {"tool": "bash",
            "arguments": {"command": "python -c \\"print(1)\\"", "mode": "background"}},
           {"tool": "process", "arguments": {"action": "status", "process_id": "$1.process_id"}}
         ]},
        {"name": "edit", "tool": "apply_patch", "arguments": {"patch": "..."},
         "show": ["src/app.py"], "expect": {"files": {"src/app.py": "expected text"}}}
      ]
    }

A string argument ``$N.path`` is replaced by that dotted path inside step N's
result ``data``. Expectations apply to the last step: ``ok``, ``error_code``,
``contains`` and ``absent`` (texts in the Model-visible result) and ``files``
(exact content, or ``null`` for a file that must not exist).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.providers.adapter import tool_result_text
from core.tools.tools import ToolCall, ToolExecutionConfig, ToolExecutor
from core.utils.tokens import estimate_tokens
from scripts.tool_lab._lab_runtime import lab_runtime
from scripts.tool_lab.definitions import tool_definitions

_VBOT_ROOT = Path(__file__).resolve().parents[2]
_STEP_REFERENCE = re.compile(r"^\$(\d+)\.([\w.]+)$")


class CaseFileError(ValueError):
    """The case file cannot be run as written."""


@dataclass(frozen=True, slots=True)
class Step:
    tool: str
    arguments: Any


@dataclass(frozen=True, slots=True)
class Case:
    name: str
    steps: tuple[Step, ...]
    show: tuple[str, ...] = ()
    expect: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class StepOutcome:
    step: Step
    arguments: Any
    envelope: dict[str, Any]
    model_view: str


@dataclass(slots=True)
class CaseOutcome:
    case: Case
    steps: list[StepOutcome]
    files: dict[str, str | None]
    failures: list[str]


def load_cases(path: Path) -> tuple[dict[str, Any], list[Case]]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CaseFileError(f"cannot read {path}: {error}") from error
    if not isinstance(document, dict) or not isinstance(document.get("cases"), list):
        raise CaseFileError(f"{path}: expected an object with a 'cases' list")
    files = document.get("files") or {}
    if not isinstance(files, dict):
        raise CaseFileError(f"{path}: 'files' must map Workspace paths to contents")
    cases = [_case(raw, index) for index, raw in enumerate(document["cases"], start=1)]
    return files, cases


def _case(raw: Any, index: int) -> Case:
    if not isinstance(raw, dict):
        raise CaseFileError(f"case {index}: expected an object")
    name = str(raw.get("name") or f"case {index}")
    raw_steps = raw.get("steps")
    if raw_steps is None:
        raw_steps = [{"tool": raw.get("tool"), "arguments": raw.get("arguments", {})}]
    if not isinstance(raw_steps, list) or not raw_steps:
        raise CaseFileError(f"{name}: 'steps' must be a non-empty list")
    steps = []
    for step in raw_steps:
        if not isinstance(step, dict) or not isinstance(step.get("tool"), str):
            raise CaseFileError(f"{name}: every step needs a 'tool' name")
        steps.append(Step(step["tool"], step.get("arguments", {})))
    return Case(
        name=name,
        steps=tuple(steps),
        show=tuple(str(item) for item in raw.get("show") or ()),
        expect=dict(raw.get("expect") or {}),
    )


def seed(workspace: Path, files: dict[str, Any]) -> None:
    for relative, spec in files.items():
        target = workspace / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(spec, str):
            text = spec
        elif isinstance(spec, dict) and "repeat" in spec:
            newline = str(spec.get("newline", "\n"))
            count = int(spec.get("count", 1))
            text = (
                newline.join(str(spec["repeat"]).format(n=n) for n in range(1, count + 1)) + newline
            )
        else:
            raise CaseFileError(f"file {relative}: expected text or a 'repeat' object")
        with target.open("w", encoding="utf-8", newline="") as handle:
            handle.write(text)


def model_view(envelope: dict[str, Any]) -> str:
    """The Tool Result text the Model receives for a persisted envelope."""
    content = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
    return str(tool_result_text(content))


async def run_cases(
    files: dict[str, Any],
    cases: list[Case],
    *,
    agent_id: str,
) -> list[CaseOutcome]:
    outcomes: list[CaseOutcome] = []
    async with lab_runtime() as (runtime, root):
        registry = runtime.tools
        _offered, definitions = tool_definitions(runtime, agent_id, include_all=True)
        contracts = registry.contracts_for_provider_definitions(list(definitions.values()))
        names = tuple(definitions)
        executor = ToolExecutor(registry)
        for number, case in enumerate(cases, start=1):
            workspace = root / "cases" / str(number)
            workspace.mkdir(parents=True)
            seed(workspace, files)
            config = ToolExecutionConfig(
                agent_id=agent_id,
                session_id=f"tool-lab-{number}",
                run_id=f"tool-lab-run-{number}",
                workspace=workspace,
                vbot_root=_VBOT_ROOT,
                data_root=root / "data",
                cwd=workspace,
                allowed_tools=names,
                session_tool_grants=names,
                input_contracts=contracts,
            )
            steps: list[StepOutcome] = []
            for index, step in enumerate(case.steps):
                arguments = _resolve_references(step.arguments, steps)
                call = ToolCall(
                    id=f"call-{number}-{index + 1}", name=step.tool, arguments=arguments
                )
                [envelope] = await executor.execute_many([call], config)
                steps.append(StepOutcome(step, arguments, envelope, model_view(envelope)))
            shown = sorted(set(case.show) | set((case.expect.get("files") or {}).keys()))
            files_after = {relative: _read(workspace / relative) for relative in shown}
            outcomes.append(CaseOutcome(case, steps, files_after, _check(case, steps, files_after)))
    return outcomes


def _resolve_references(value: Any, previous: list[StepOutcome]) -> Any:
    if isinstance(value, dict):
        return {key: _resolve_references(item, previous) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve_references(item, previous) for item in value]
    if not isinstance(value, str):
        return value
    match = _STEP_REFERENCE.match(value)
    if match is None:
        return value
    number = int(match.group(1))
    if not 1 <= number <= len(previous):
        raise CaseFileError(f"{value}: step {number} has not run yet")
    current: Any = previous[number - 1].envelope.get("data")
    for part in match.group(2).split("."):
        if not isinstance(current, dict) or part not in current:
            raise CaseFileError(f"{value}: step {number} data has no {match.group(2)}")
        current = current[part]
    return current


def _read(path: Path) -> str | None:
    try:
        with path.open(encoding="utf-8", newline="") as handle:
            return handle.read()
    except FileNotFoundError:
        return None


def _check(case: Case, steps: list[StepOutcome], files: dict[str, str | None]) -> list[str]:
    expect = case.expect
    last = steps[-1]
    error = last.envelope.get("error") or {}
    failures: list[str] = []
    if "ok" in expect and last.envelope.get("ok") is not expect["ok"]:
        failures.append(f"ok is {last.envelope.get('ok')}, expected {expect['ok']}")
    if "error_code" in expect and error.get("code") != expect["error_code"]:
        failures.append(f"error code is {error.get('code')}, expected {expect['error_code']}")
    for text in expect.get("contains") or ():
        if text not in last.model_view:
            failures.append(f"result lacks {text!r}")
    for text in expect.get("absent") or ():
        if text in last.model_view:
            failures.append(f"result contains {text!r}")
    for relative, wanted in (expect.get("files") or {}).items():
        if files.get(relative) != wanted:
            failures.append(f"{relative} differs from the expected content")
    return failures


def report(outcomes: list[CaseOutcome], *, max_chars: int, visible: bool) -> str:
    lines: list[str] = []
    for number, outcome in enumerate(outcomes, start=1):
        lines.append(f"=== [{number}] {outcome.case.name}")
        for step in outcome.steps:
            arguments = json.dumps(step.arguments, ensure_ascii=False)
            error = step.envelope.get("error") or {}
            verdict = "ok" if step.envelope.get("ok") else f"error {error.get('code')}"
            tokens = estimate_tokens(step.model_view)[0]
            lines.append(f"--> {step.step.tool} {_clip(arguments, max_chars)}")
            lines.append(f"<-- {verdict}, {len(step.model_view)} chars, {tokens} tokens")
            lines.append(_clip(_shown(step.model_view, visible), max_chars))
        for relative, content in outcome.files.items():
            lines.append(f"--- {relative}")
            if content is None:
                lines.append("(does not exist)")
            else:
                lines.append(_clip(_shown(content, visible), max_chars))
        if outcome.case.expect:
            lines.append("FAIL: " + "; ".join(outcome.failures) if outcome.failures else "PASS")
        lines.append("")
    checked = [outcome for outcome in outcomes if outcome.case.expect]
    if checked:
        failed = sum(1 for outcome in checked if outcome.failures)
        lines.append(f"{len(checked) - failed}/{len(checked)} checked cases pass")
    return "\n".join(lines)


def _shown(text: str, visible: bool) -> str:
    # A raw carriage return would overwrite the terminal line, so it always shows.
    text = text.replace("\r", "\\r")
    return text.replace("\t", "\\t") if visible else text


def _clip(text: str, limit: int) -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    return f"{text[:limit]}... [{len(text) - limit} more chars]"
