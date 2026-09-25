"""Agent access to the shared structured-decision executor."""

from __future__ import annotations

from typing import Any

from core.model_tasks import TaskUsageContext
from core.model_tasks.decision_types import DecisionError
from core.model_tasks.decisions import DecisionService
from core.tools.tools import ToolContext, ToolDisplay, ToolRegistry, tool_failure, tool_success

EVALUATE_DESCRIPTION = (
    "Evaluate focused questions about supplied text or JSON using the configured decision model. "
    "Returns typed answers and available probabilities, not explanations. Ask independent "
    "questions together; each sees only the supplied state. Use results as judgments, not proof."
)
EVALUATE_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "state": {
            "description": (
                "Content and context to evaluate. Supply text, a JSON "
                "object, or a JSON array; images are not supported."
            ),
            "oneOf": [{"type": "string"}, {"type": "object"}, {"type": "array", "items": {}}],
        },
        "questions": {
            "type": "array",
            "description": (
                "Independent questions evaluated together against state. "
                "Answers use these ids; question ids are not instructions."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "Unique answer id within this call."},
                    "type": {
                        "type": "string",
                        "enum": ["choice", "score", "noul"],
                        "description": (
                            "Choice selects a label; score rates ordered "
                            "levels; noul estimates the probability of yes."
                        ),
                    },
                    "instructions": {
                        "type": "string",
                        "description": (
                            "One complete, focused question about state. "
                            "Name relevant fields when state is structured."
                        ),
                    },
                    "criteria": {
                        "description": (
                            "Choice: at least two label-to-description "
                            'entries, e.g. {"bug":"Broken '
                            'functionality","other":"Anything else"}. Score: '
                            "descriptions ordered from lowest to highest, "
                            "indexed from zero. Noul: omit unless clarifying "
                            'yes/no; then supply {"true":"When yes '
                            'applies","false":"When no applies"}.'
                        ),
                        "oneOf": [
                            {"type": "object"},
                            {"type": "array", "items": {"type": "string"}},
                        ],
                    },
                },
                "required": ["id", "type", "instructions"],
            },
        },
    },
    "required": ["state", "questions"],
}


def register_evaluate_tool(registry: ToolRegistry, service: DecisionService) -> None:
    async def handler(context: ToolContext, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            result = await service.evaluate(
                arguments.get("state"),
                arguments.get("questions"),
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
        return tool_success({key: result[key] for key in ("answers", "model", "usage")})

    registry.register(
        "evaluate",
        EVALUATE_DESCRIPTION,
        EVALUATE_PARAMETERS,
        handler,
        family="execution",
        open_input_schema=True,
        ready=service.available,
        readiness_hint="Configure an available Decision model in Settings > Specialized Models.",
        result_schema={"type": "object", "required": ["answers", "model", "usage"]},
        display=ToolDisplay(
            summary_builder=lambda args: f"{len(args.get('questions', []))} questions"
        ),
    )
