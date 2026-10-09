"""Agent access to the Decision Model: typed answers about each of several texts."""

from __future__ import annotations

import json
from functools import cache
from typing import Any

from core.model_tasks import TaskUsageContext
from core.model_tasks.decision_types import DecisionError
from core.model_tasks.decisions import ITEM_LIMIT, NOT_CONFIGURED_MESSAGE, DecisionService
from core.tools._classify_arguments import normalize_classify_arguments, wire_question
from core.tools.contracts import ToolContract, compile_tool_contract
from core.tools.tools import ToolContext, ToolDisplay, ToolRegistry, tool_failure, tool_success

CLASSIFY_DESCRIPTION = (
    "Classify texts with a separate, fast decision model. For each item it answers your "
    "questions with a probability of yes, one of your options, or a level on your scale. Use "
    "it to filter, sort or label many texts, or to get a probability instead of judging "
    "yourself. It sees only what you pass, gives no reasons, and cannot count, calculate or "
    "compare dates."
)
CLASSIFY_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": ITEM_LIMIT,
            "description": (
                "Texts to classify, such as one file path with its first lines per item. Each "
                "item is judged on its own with every question."
            ),
        },
        "context": {
            "type": "string",
            "description": (
                "Text every item is judged against, such as the task or the user's request. "
                "Omit when the questions say everything needed."
            ),
        },
        "questions": {
            "type": "array",
            "description": "Questions asked about every item.",
            "items": {
                "type": "object",
                "properties": {
                    "id": {
                        "type": "string",
                        "description": "Name of this answer in the result. Omit to use q1, q2, ...",
                    },
                    "type": {
                        "type": "string",
                        "enum": ["yes_no", "choice", "score"],
                        "description": (
                            "yes_no: probability of yes, 0 to 1. choice: one label from "
                            "options. score: a level number from levels, starting at 0."
                        ),
                    },
                    "instructions": {
                        "type": "string",
                        "description": "The complete question about one item.",
                    },
                    "options": {
                        "type": "object",
                        "description": (
                            "Required for choice: each label mapped to what it means, e.g. "
                            '{"bug":"Broken functionality","other":"Anything else"}. Include '
                            "a catch-all label when no option might fit."
                        ),
                    },
                    "levels": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Required for score: what each level means, lowest first; 2 to 10."
                        ),
                    },
                },
                "required": ["type", "instructions"],
            },
        },
    },
    "required": ["items", "questions"],
}

_EXCERPT_LENGTH = 50


@cache
def _repair_contract() -> ToolContract:
    return compile_tool_contract(
        name="classify", input_schema=CLASSIFY_PARAMETERS, require_closed_input=False
    )


def _normalize_classify_arguments(arguments: Any) -> Any:
    return normalize_classify_arguments(_repair_contract(), arguments)


def register_classify_tool(registry: ToolRegistry, service: DecisionService) -> None:
    async def handler(context: ToolContext, arguments: dict[str, Any]) -> dict[str, Any]:
        items = arguments["items"]
        questions = arguments["questions"]
        try:
            outcomes = await service.classify(
                items,
                [wire_question(question) for question in questions],
                context=arguments.get("context"),
                usage_context=TaskUsageContext(
                    agent_id=context.agent_id,
                    project_id=context.project_id,
                    session_id=context.session_id,
                    run_id=context.run_id,
                    owner_name=context.execution_owner.extension
                    if context.execution_owner
                    else None,
                    group_id=context.execution_owner.group_id if context.execution_owner else None,
                ),
            )
        except DecisionError as exc:
            return tool_failure(exc.code, str(exc))
        errors = [outcome for outcome in outcomes if isinstance(outcome, DecisionError)]
        if len(errors) == len(outcomes):
            first = errors[0]
            prefix = "" if len(outcomes) == 1 else "No item was classified. "
            return tool_failure(first.code, prefix + str(first))
        return tool_success({"content": _result_text(items, questions, outcomes)})

    registry.register(
        "classify",
        CLASSIFY_DESCRIPTION,
        CLASSIFY_PARAMETERS,
        handler,
        summary=(
            "Let a fast separate model label, filter or sort many texts by your yes/no, choice or "
            "scale questions."
        ),
        family="execution",
        open_input_schema=True,
        argument_normalizer=_normalize_classify_arguments,
        ready=service.available,
        readiness_hint=NOT_CONFIGURED_MESSAGE,
        result_schema={"type": "object", "required": ["content"]},
        display=ToolDisplay(summary_builder=_summary),
    )


def _summary(raw_arguments: dict[str, Any]) -> str:
    try:
        arguments = _normalize_classify_arguments(raw_arguments)
    except ValueError:
        arguments = raw_arguments
    if not isinstance(arguments, dict):
        return ""
    items = arguments.get("items")
    count = len(items) if isinstance(items, list) else 0
    return f"{count} item" if count == 1 else f"{count} items"


def _result_text(
    items: list[Any],
    questions: list[dict[str, Any]],
    outcomes: list[dict[str, Any] | DecisionError],
) -> str:
    """One line per item in item order; failed items are named for a repeated call."""
    several = len(items) > 1
    lines: list[str] = []
    failed: list[int] = []
    for number, (item, outcome) in enumerate(zip(items, outcomes, strict=True), 1):
        prefix = f"item {number} ({_excerpt(item)}): " if several else ""
        if isinstance(outcome, DecisionError):
            failed.append(number)
            lines.append(f"{prefix}not classified: {outcome}")
            continue
        answers = [
            ("" if len(questions) == 1 else f"{question['id']}: ")
            + _answer_text(question, outcome["answers"][question["id"]])
            for question in questions
        ]
        lines.append(prefix + "; ".join(answers))
    if failed:
        numbers = ", ".join(str(number) for number in failed)
        noun = "Item" if len(failed) == 1 else "Items"
        lines.append(
            f"\n{noun} {numbers} {'was' if len(failed) == 1 else 'were'} not classified; the "
            "others were. Repeat the call with only those items."
        )
    return "\n".join(lines)


def _excerpt(item: Any) -> str:
    text = item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
    line = next((part.strip() for part in text.splitlines() if part.strip()), "")
    line = " ".join(line.split())
    return line if len(line) <= _EXCERPT_LENGTH else line[: _EXCERPT_LENGTH - 3] + "..."


def _answer_text(question: dict[str, Any], answer: dict[str, Any]) -> str:
    kind = question["type"]
    if kind == "yes_no":
        return f"probability of yes {_number(answer['noul'])}"
    if kind == "choice":
        options = question["options"]
        text = str(answer["choice"])
        # Options numbered from 1 carry their meaning only in the options; the
        # answer quotes the chosen one, so the number needs no lookup.
        if list(options) == [str(number) for number in range(1, len(options) + 1)]:
            option = str(options[text])
            shown = option if len(option) <= 80 else option[:77] + "..."
            text += f" {shown!r}"
    else:
        text = f"level {_number(answer['score'])} on levels 0-{len(question['levels']) - 1}"
    details = []
    if "confidence" in answer:
        details.append(f"confidence {_number(answer['confidence'])}")
    if "probabilities" in answer:
        details.append(
            ", ".join(
                f"{label} {_number(value)}" for label, value in answer["probabilities"].items()
            )
        )
    return f"{text} ({'; '.join(details)})" if details else text


def _number(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.3g}"
    return str(value)
