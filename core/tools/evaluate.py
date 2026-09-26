"""Agent access to the shared structured-decision executor."""

from __future__ import annotations

from functools import cache
from typing import Any

from core.model_tasks import TaskUsageContext
from core.model_tasks.decision_types import DecisionError
from core.model_tasks.decisions import DecisionService
from core.tools._evaluate_arguments import normalize_evaluate_arguments
from core.tools.contracts import ToolContract, compile_tool_contract
from core.tools.tools import ToolContext, ToolDisplay, ToolRegistry, tool_failure, tool_success

EVALUATE_DESCRIPTION = (
    "Ask a separate decision model focused questions about text or JSON you supply. Answers "
    "are typed (a label, a level, or the probability of yes) and unexplained; treat them as "
    "judgments, not proof. Ask independent questions together."
)
EVALUATE_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "state": {
            "description": (
                "Text, JSON object or JSON array to judge; include everything the questions "
                "need. No images."
            ),
            "oneOf": [{"type": "string"}, {"type": "object"}, {"type": "array", "items": {}}],
        },
        "questions": {
            "type": "array",
            "description": "Questions about state, answered by id.",
            "items": {
                "type": "object",
                "properties": {
                    "id": {
                        "type": "string",
                        "description": "Unique answer id; default q1, q2, ...",
                    },
                    "type": {
                        "type": "string",
                        "enum": ["choice", "score", "noul"],
                        "description": (
                            "noul: probability of yes, 0 to 1. choice: one label from "
                            "criteria. score: a level number from criteria, from 0."
                        ),
                    },
                    "instructions": {
                        "type": "string",
                        "description": (
                            "The complete question. Name the fields it concerns when state is JSON."
                        ),
                    },
                    "criteria": {
                        "description": (
                            "choice: two or more labels with meanings, e.g. "
                            '{"bug":"Broken functionality","other":"Anything else"}. score: '
                            "level meanings, lowest first. noul: optional "
                            '{"true":"<yes means>","false":"<no means>"}.'
                        ),
                        "oneOf": [
                            {"type": "object"},
                            {"type": "array", "items": {"type": "string"}},
                        ],
                    },
                },
                "required": ["type", "instructions"],
            },
        },
    },
    "required": ["state", "questions"],
}


@cache
def _repair_contract() -> ToolContract:
    return compile_tool_contract(
        name="evaluate", input_schema=EVALUATE_PARAMETERS, require_closed_input=False
    )


def _normalize_evaluate_arguments(arguments: Any) -> Any:
    return normalize_evaluate_arguments(_repair_contract(), arguments)


def register_evaluate_tool(registry: ToolRegistry, service: DecisionService) -> None:
    async def handler(context: ToolContext, arguments: dict[str, Any]) -> dict[str, Any]:
        questions = arguments.get("questions")
        try:
            result = await service.evaluate(
                arguments.get("state"),
                questions,
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
            return tool_failure(exc.code, str(exc), retryable=False)
        return tool_success(_result_data(questions if isinstance(questions, list) else [], result))

    registry.register(
        "evaluate",
        EVALUATE_DESCRIPTION,
        EVALUATE_PARAMETERS,
        handler,
        family="execution",
        open_input_schema=True,
        argument_normalizer=_normalize_evaluate_arguments,
        ready=service.available,
        readiness_hint="Configure an available Decision model in Settings > Specialized Models.",
        result_schema={"type": "object", "required": ["model", "usage", "content"]},
        display=ToolDisplay(summary_builder=_summary),
    )


def _summary(raw_arguments: dict[str, Any]) -> str:
    try:
        arguments = _normalize_evaluate_arguments(raw_arguments)
    except ValueError:
        arguments = raw_arguments
    questions = arguments.get("questions") if isinstance(arguments, dict) else None
    count = len(questions) if isinstance(questions, list) else 0
    return f"{count} question" if count == 1 else f"{count} questions"


def _result_data(questions: list[Any], result: dict[str, Any]) -> dict[str, Any]:
    """One readable line per answer, in question order, plus the model and its usage."""
    answers = result["answers"]
    levels = {
        question["id"]: len(question.get("criteria") or ())
        for question in questions
        if isinstance(question, dict) and question.get("type") == "score"
    }
    lines = [
        f"{identifier}: {_answer_text(answer, levels.get(identifier))}"
        for identifier, answer in answers.items()
    ]
    return {
        "model": result["model"],
        "usage": _usage_text(result["usage"]),
        "content": "\n".join(lines),
    }


def _answer_text(answer: dict[str, Any], levels: int | None) -> str:
    kind = answer["type"]
    if kind == "noul":
        return f"probability of yes {_number(answer['noul'])}"
    if kind == "choice":
        text = str(answer["choice"])
    else:
        top = f" on levels 0-{levels - 1}" if levels else ""
        text = f"level {_number(answer['score'])}{top}"
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


def _usage_text(usage: dict[str, Any]) -> str:
    text = f"{usage['input_tokens']} input and {usage['output_tokens']} output tokens"
    if "cost" in usage:
        text += f", cost {_number(usage['cost'])}"
    return text


def _number(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.3g}"
    return str(value)
