"""Agent-visible Factory instructions, definitions, continuations and outcomes."""

from __future__ import annotations

import json
from typing import Any

from core.tools import tool_failure, tool_success

type Json = dict[str, Any]

_STEP_RULES = (
    "You do one step of a Factory workflow for a Card. Do your task, then call "
    "factory_submit_work. If you need a decision or information from the user, call "
    "factory_report_blocked."
)
_CHANGES_RULES = (
    "When your task changes the repository, leave the base branch unchanged: work in isolation "
    "as the Project instructions describe, or on a local branch if they describe none. Run the "
    "checks the Project prescribes, commit, and name the branch with your changes and the "
    "checks' outcomes in your summary. Integrate into the base branch only when your task "
    "says so."
)
_RETURN_RULES = (
    "When the result of step {target} needs changes, call factory_request_changes with "
    "concrete findings instead of factory_submit_work. Check whether findings you or earlier "
    "runs of your step sent back were addressed."
)


def _name(workflow: Json, step_id: str) -> str:
    return json.dumps(next(item["name"] for item in workflow["steps"] if item["id"] == step_id))


def rules(workflow: Json, step: Json) -> str:
    """A participant's standing instructions: the Factory's rules, then the step's task."""
    parts = [_STEP_RULES, _CHANGES_RULES]
    if step["returns_to"]:
        parts.append(_RETURN_RULES.format(target=_name(workflow, step["returns_to"])))
    parts.append(f"Your task:\n{step['task'].strip()}")
    return "\n\n".join(parts)


def _data(label: str, value: Any) -> str:
    # Delimit untrusted Card/result text once; it cannot impersonate a continuation.
    text = (
        json.dumps(value, ensure_ascii=False, indent=2)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
    )
    return f"{label} (data):\n{text}"


def _card(card: Json) -> Json:
    return {key: card.get(key, "") for key in ("id", "project_id", "title", "description")}


def output_view(output: Json) -> Json:
    """One recorded hand-in as participants read it."""
    view: Json = {"step": output["step_name"], "summary": output["summary"]}
    if output.get("result"):
        view["result"] = output["result"]
    if output.get("branch"):
        # Hand-ins recorded by an earlier Factory named their branch only here.
        view["branch"] = output["branch"]
    return view


def step_input(
    card: Json,
    step: Json,
    *,
    comments: list[Json],
    earlier: list[Json],
    findings: list[Json],
    note: Json,
) -> str:
    """The first message of a step's participant."""
    workflow = card["runtime"]["workflow"]
    position = next(
        index for index, item in enumerate(workflow["steps"], 1) if item["id"] == step["id"]
    )
    heading = (
        f"Do step {json.dumps(step['name'])} ({position} of {len(workflow['steps'])}) of the "
        f"Factory workflow {json.dumps(workflow['name'])} for this Card."
    )
    parts = [heading, _data("Card", _card(card)), _data("User comments", comments)]
    if earlier:
        parts.append(_data("Results of earlier steps", [output_view(item) for item in earlier]))
    if findings:
        parts.append(
            _data(
                "Findings your step sent back earlier",
                [{"run": item["pass"], "findings": item["findings"]} for item in findings],
            )
        )
    if note.get("kind") in {"rework", "send_back"}:
        parts.append(note_text(note["kind"], card, note.get("text", ""), source=note.get("source")))
    return "\n\n".join(parts)


def note(
    kind: str,
    card: Json,
    text: str = "",
    earlier: list[Json] | None = None,
    *,
    step: Json | None = None,
    source: str | None = None,
) -> str:
    """A continuation for a participant whose Session already exists."""
    content = note_text(kind, card, text, step=step, source=source)
    if earlier:
        content += "\n\n" + _data(
            "Results of earlier steps", [output_view(item) for item in earlier]
        )
    return content


def note_text(
    kind: str,
    card: Json,
    text: str = "",
    *,
    step: Json | None = None,
    source: str | None = None,
) -> str:
    if kind == "keep_working" and step is not None and step["returns_to"]:
        workflow = card["runtime"]["workflow"]
        keep_working = (
            "Finish your step with factory_submit_work, or with factory_request_changes if the "
            f"result of step {_name(workflow, step['returns_to'])} needs changes. An ordinary "
            "answer hands nothing in."
        )
    else:
        keep_working = (
            "Continue your step's task, then call factory_submit_work. If you need the user's "
            "decision or information, call factory_report_blocked."
        )
    notes = {
        "keep_working": keep_working,
        "rework": (
            f"Step {json.dumps(source or '')} sent your result back with the findings below. "
            "Address them, run the checks your task needs, then call factory_submit_work."
        ),
        "send_back": (
            "The user sent the Card back to your step. Act on their comment below, then call "
            "factory_submit_work."
        ),
        "answer": (
            "The user answered your question. Continue with their answer and finish with the "
            "Factory Tool your current task needs."
        ),
        "refresh": (
            "Results of earlier steps changed after your last hand-in. Redo your task with the "
            "updated results below, then call factory_submit_work."
        ),
        "restart": f"vBot restarted while you worked on this step. {keep_working}",
    }
    content = notes[kind]
    if text:
        content += "\n\n" + _data("Comment or findings", text)
    return content


PUBLIC_DESCRIPTION = (
    "Create proposed work and read Cards on the Software Factory Board. Work starts automatically "
    "for the user's requests and authorized automations; "
    "your own ideas wait for the user's approval."
)
PUBLIC_PARAMETERS: Json = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["create", "list", "get"]},
        "project": {
            "type": "string",
            "description": (
                "Required for create: a Project id with an enabled Factory setup. "
                "Omit for list to include every Project."
            ),
        },
        "title": {"type": "string", "description": "Required for create."},
        "description": {
            "type": "string",
            "description": (
                "Required for create: the user's objective and acceptance criteria, "
                "without extra requirements."
            ),
        },
        "workflow": {
            "type": "string",
            "description": (
                "For create: the name of one of the Project's Factory workflows, such as Plan "
                "or Implement. Omit to use the Project's default workflow."
            ),
        },
        "state": {
            "type": "string",
            "enum": [
                "proposal",
                "queued",
                "working",
                "waiting",
                "done",
                "discarded",
            ],
            "description": "For list. Omit for every state.",
        },
        "id": {
            "type": "string",
            "description": "Required for get: the Card id from create or list.",
        },
    },
    "required": ["action"],
    "additionalProperties": False,
}

PRIVATE_TOOLS: dict[str, tuple[str, Json]] = {
    "factory_submit_work": (
        "Hand in your step's result and end this turn; the Factory starts the next step.",
        {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "What you did and what came of it, for later steps and "
                    "the user.",
                },
                "result": {
                    "type": "string",
                    "description": (
                        "The complete written result when your task asks for one, such as a plan "
                        "or a review. Omit when summary says everything."
                    ),
                },
            },
            "required": ["summary"],
            "additionalProperties": False,
        },
    ),
    "factory_report_blocked": (
        "Ask for a decision or information from the user, pause the Card and end this turn.",
        {
            "type": "object",
            "properties": {"question": {"type": "string"}},
            "required": ["question"],
            "additionalProperties": False,
        },
    ),
    "factory_request_changes": (
        "Send the result of an earlier step back for rework with your findings and end this "
        "turn. Within your step's send-back limit that step reworks it and the steps after it "
        "run again; past the limit, the user decides.",
        {
            "type": "object",
            "properties": {
                "findings": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Concrete issues the earlier step can fix, one per item.",
                },
            },
            "required": ["findings"],
            "additionalProperties": False,
        },
    ),
}


def tool_result(kind: str, card: Json | None = None, text: str = "") -> Json:
    messages = {
        "created": (
            "Card created and queued with workflow {workflow}; work continues independently "
            "within the WIP limit. Progress and results appear on the Board, not in this Session."
        ),
        "proposal": "Proposal saved; work waits for the user's approval on the Board.",
        "submitted": "Result recorded; the Factory handles the next step. This turn ends here.",
        "blocked": (
            "Question sent to the user; the Card waits for their answer. This turn ends here."
        ),
        "changes_requested": (
            "Findings recorded; the Factory sends the work back or, past the limit, asks the "
            "user. This turn ends here."
        ),
    }
    message = text or messages.get(kind, "Factory action recorded.")
    if card is not None and "{workflow}" in message:
        message = message.format(workflow=json.dumps(card.get("workflow_name", "default")))
    data: Json = {"message": message}
    if card is not None:
        data.update(id=card["id"], state=card["state"])
        if card.get("waiting_reason"):
            data["waiting_reason"] = card["waiting_reason"]
    return tool_success(data)


ERROR_MESSAGES = {
    "participant_required": (
        "Nothing was changed. This Tool is available only to the Factory participant assigned "
        "to the Card's current step."
    ),
    "stale_run": (
        "Nothing was changed. This Run no longer owns the Card's current step. "
        "Read the Card on the Board for its current state."
    ),
    "wrong_state": (
        "Nothing was changed. This action does not apply to the Card's current state. "
        "Read the Card on the Board for its available actions."
    ),
    "missing_text": "Nothing was changed. Supply non-empty text for {field}.",
    "invalid_findings": (
        "Nothing was changed. Supply findings as a non-empty list of concrete issues, one per "
        "item. To hand in your assessment without sending work back, call factory_submit_work."
    ),
    "budget_exhausted": (
        "The participant reached its turn budget without a hand-in. "
        "Send the Card back with guidance to continue."
    ),
    "run_failed": (
        "The participant Run failed or was cancelled. "
        "Inspect its Session, then retry or send the Card back."
    ),
    "restart": (
        "The Card was interrupted again without a hand-in. "
        "Inspect its Sessions, then retry when ready."
    ),
    "launch_failed": (
        "The Card could not start its next participant. "
        "Check the Project setup and participant Session, then retry."
    ),
    "cleanup_failed": (
        "The Card's cleanup or archival did not finish. "
        "Inspect its participant Session and retry deletion when ready."
    ),
    "workflow_upgrade": (
        "This Card started under an earlier Factory version and cannot resume where it stopped. "
        "Its results are kept: Accept continues with its next step or finishes it, and Send back "
        "reworks a step with your guidance."
    ),
}


def error_message(code: str, **values: Any) -> str:
    return ERROR_MESSAGES[code].format(**values)


def refusal(code: str, message: str) -> Json:
    text = (
        message if message.startswith("Nothing was changed.") else f"Nothing was changed. {message}"
    )
    return tool_failure(code, text)


def unknown_outcome(*, participant: bool = False) -> Json:
    return tool_failure(
        "factory_failed",
        ("The outcome is not confirmed. Read this Card on the Board before repeating the hand-in.")
        if participant
        else (
            "The outcome is not confirmed. Read the Board with factory_card action list "
            "before repeating a create call."
        ),
    )
