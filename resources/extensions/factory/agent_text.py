"""Agent-visible Factory instructions, definitions, continuations and outcomes."""

from __future__ import annotations

import json
from typing import Any

from core.tools import tool_failure, tool_success

type Json = dict[str, Any]

_WORK_RULES = (
    "You do one stage of a Factory workflow for a Card. Do the work your stage instructions "
    "describe, then call factory_submit_work. If you need a decision or information from the "
    "user, call factory_report_blocked."
)
_CHANGES_RULES = (
    "Work in isolation according to the Project instructions. Do not integrate into the base "
    "branch before the user accepts the Card. Run the checks the Project prescribes, commit "
    "your change on a local branch and submit that branch."
)
_DOCUMENT_RULES = (
    "Your stage produces a document, not repository changes: investigate as needed, but do not "
    "change files or branches. Submit the complete document as result."
)
_REVIEW_RULES = (
    "Review the results handed to you without changing files or branches. Judge whether they "
    "achieve the Card's objective and their stage instructions, and whether the approach is "
    "sound, correct and simple. For repository changes, review the submitted commit range "
    "against the Project conventions. Check whether previous findings were addressed, then call "
    "factory_submit_review. Your recommendation tells the user whether to accept and what you "
    "would add or change."
)

_INTEGRATION_REVIEW_GATE = (
    "If integration needs code changes or would rewrite a reviewed commit, including by rebase, "
    "call factory_report_integration with outcome needs_changes before updating the base. "
    "A changed commit SHA needs review even when its patch is unchanged."
)


def rules(stage: Json) -> str:
    """A participant's standing instructions: the stage kind's rules, then the user's."""
    if stage["kind"] == "review":
        parts = [_REVIEW_RULES]
    else:
        parts = [
            _WORK_RULES,
            _CHANGES_RULES if stage["output"] == "changes" else _DOCUMENT_RULES,
        ]
    if stage.get("instructions", "").strip():
        parts.append(f"Stage instructions:\n{stage['instructions'].strip()}")
    return "\n\n".join(parts)


def _data(label: str, value: Any) -> str:
    # Delimit untrusted Card/review text once; it cannot impersonate a continuation.
    text = (
        json.dumps(value, ensure_ascii=False, indent=2)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
    )
    return f"{label} (data):\n{text}"


def _card(card: Json) -> Json:
    return {key: card.get(key, "") for key in ("id", "project_id", "title", "description")}


def output_view(output: Json) -> Json:
    """One recorded stage result as participants read it."""
    view: Json = {"stage": output["stage_name"]}
    if output.get("lanes", 1) > 1:
        view["lane"] = output["lane"] + 1
    if output["kind"] == "work":
        view["summary"] = output["summary"]
        if output.get("result"):
            view["result"] = output["result"]
        if output.get("branch"):
            view.update(
                branch=output["branch"],
                base=output["base"],
                range=f"{output['base_sha']}..{output['head_sha']}",
            )
        if output.get("checks"):
            view["checks"] = output["checks"]
    else:
        view["verdict"] = output["verdict"]
        if output.get("recommendation"):
            view["recommendation"] = output["recommendation"]
        if output.get("findings"):
            view["findings"] = output["findings"]
    return view


def _position(workflow: Json, index: int, lane: int) -> str:
    stages = workflow["stages"]
    stage = stages[index]
    text = (
        f"Do stage {stage['name']!r} ({index + 1} of {len(stages)}) of the Factory workflow "
        f"{workflow['name']!r} for this Card."
    )
    if len(stage["lanes"]) > 1:
        text += (
            f" You are lane {lane + 1} of {len(stage['lanes'])}: the other lanes do the same "
            "task independently, and a later stage sees all results."
        )
    return text


def work_input(
    card: Json, workflow: Json, index: int, lane: int, comments: list[Json], earlier: list[Json]
) -> str:
    stage = workflow["stages"][index]
    parts = [_position(workflow, index, lane), _data("Card", _card(card))]
    if stage["output"] == "changes":
        parts.append(
            _data("Default base branch", card["runtime"]["settings"].get("base_branch", "main"))
        )
    parts.append(_data("User comments", comments))
    if earlier:
        parts.append(_data("Results of earlier stages", [output_view(item) for item in earlier]))
    return "\n\n".join(parts)


def review_input(
    card: Json,
    workflow: Json,
    index: int,
    *,
    under_review: list[Json],
    previous: list[Json],
    integration: bool,
) -> str:
    stage = workflow["stages"][index]
    target = next(item for item in workflow["stages"] if item["id"] == stage["target"])
    landing = card["runtime"].get("integration_landing")
    after_landing = (
        integration and isinstance(landing, dict) and landing.get("head_sha") == card["head_sha"]
    )
    if after_landing:
        heading = "Independently review this already integrated change."
    elif integration:
        heading = "Independently review the changed integration of this Factory Card."
    else:
        heading = f"Independently review the results of stage {target['name']!r} for this Card."
    parts = [heading, _data("Card", _card(card))]
    if integration:
        parts.append(
            _data(
                "Submission",
                {
                    key: card.get(key, "")
                    for key in ("base", "branch", "base_sha", "head_sha", "summary", "checks")
                }
                | {"range": f"{card['base_sha']}..{card['head_sha']}"},
            )
        )
    else:
        parts.append(_data("Results under review", [output_view(item) for item in under_review]))
    parts.append(
        _data(
            "Previous review findings",
            [
                {"round": item["pass"], "findings": item.get("findings", [])}
                for item in previous
                if item.get("findings")
            ],
        )
    )
    if after_landing:
        parts.append(_data("Integration landing", landing))
    return "\n\n".join(parts)


def note(kind: str, card: Json, text: str = "", earlier: list[Json] | None = None) -> str:
    notes = {
        "keep_working": (
            "Continue your stage's work, then call factory_submit_work. If you need the user's "
            "decision or information, call factory_report_blocked."
        ),
        "keep_integrating": (
            f"{_INTEGRATION_REVIEW_GATE} Otherwise continue the accepted integration, "
            "then call factory_report_integration."
        ),
        "review_nudge": (
            "Finish the independent review with factory_submit_review; an ordinary answer does "
            "not submit the verdict."
        ),
        "rework": (
            "A review requested changes to your stage's result. Address the findings, run the "
            "checks your stage needs, then call factory_submit_work. The Card has not been "
            "accepted for integration."
        ),
        "send_back": (
            "The user sent the Card back to your stage. Act on their comment, then call "
            "factory_submit_work. The Card has not been accepted for integration."
        ),
        "answer": (
            "The user answered your question. Continue with their answer and finish with the "
            "Factory Tool your current step needs."
        ),
        "refresh": (
            "Results of earlier stages changed after your last hand-in. Redo your part with the "
            "updated results, then call factory_submit_work."
        ),
        "restart": (
            "vBot restarted. Continue this Card from its saved phase and use the matching Factory "
            "Tool to finish the hand-in."
        ),
        "discard": (
            "The Card was discarded. Remove your worktree and branch as the Project instructions "
            "say. Do not integrate the discarded work; this cleanup gets one turn."
        ),
        "integration_changes": (
            "Make the integration changes in isolation, run the prescribed checks, then call "
            "factory_submit_work. The changed result needs independent review before integration."
        ),
        "integrate": (
            f"The user accepted commit {card.get('head_sha', '')} of {card.get('branch', '')}. "
            f"{_INTEGRATION_REVIEW_GATE} Otherwise integrate the reviewed commit according to the "
            "Project instructions, then clean up as they say, then call factory_report_integration."
        ),
    }
    content = notes[kind]
    if kind in {"integrate", "keep_integrating"}:
        landing = card["runtime"].get("integration_landing")
        if isinstance(landing, dict) and landing.get("head_sha") == card.get("head_sha"):
            content = (
                f"The user accepted the change already integrated at {landing['landing_sha']}. "
                "Leave the base unchanged, finish cleanup according to the Project instructions, "
                "then call factory_report_integration."
            )
        if kind == "integrate" and (commit := card["runtime"].get("integration_base_sha")):
            content += "\n\n" + _data(
                "Target base at acceptance", {"base": card["base"], "commit": commit}
            )
    if text:
        content += "\n\n" + _data("Comment or findings", text)
    if earlier:
        content += "\n\n" + _data(
            "Results of earlier stages", [output_view(item) for item in earlier]
        )
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
                "reviewing",
                "waiting",
                "integrating",
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
        "Hand in your stage's result and end this turn; the Factory passes it to the next stage. "
        "This does not authorize integration.",
        {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "What you did and what came of it, for later stages and "
                    "the user.",
                },
                "result": {
                    "type": "string",
                    "description": (
                        "Required when your stage produces a document: the complete document, "
                        "such as the plan. Omit when your stage changes the repository."
                    ),
                },
                "branch": {
                    "type": "string",
                    "description": (
                        "Required when your stage changes the repository: the local branch "
                        "containing the finished change. Omit for a document."
                    ),
                },
                "base": {
                    "type": "string",
                    "description": "Required with branch: the branch the change is based on.",
                },
                "checks": {
                    "type": "string",
                    "description": (
                        "Required with branch: which prescribed checks ran and their outcomes; "
                        "name any not run."
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
    "factory_submit_review": (
        "Submit your independent verdict and end this turn. Approval passes the results on; "
        "requested changes return them to their stage within the review limit, otherwise to "
        "the user. After landing, requested changes go directly to the user.",
        {
            "type": "object",
            "properties": {
                "verdict": {"type": "string", "enum": ["approve", "changes_requested"]},
                "recommendation": {
                    "type": "string",
                    "description": (
                        "Required for approve: whether the idea and implementation are good, "
                        "and what you would add or change."
                    ),
                },
                "findings": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Required and non-empty for changes_requested: concrete issues the "
                        "authors can address. Omit when there are no findings."
                    ),
                },
            },
            "required": ["verdict"],
            "additionalProperties": False,
        },
    ),
    "factory_report_integration": (
        "Report the accepted change's integration and end this turn. "
        "Changed commits go through independent review again.",
        {
            "type": "object",
            "properties": {
                "outcome": {"type": "string", "enum": ["integrated", "needs_changes", "failed"]},
                "reference": {
                    "type": "string",
                    "description": (
                        "For integrated: the merge commit or PR URL. After rebasing, supply the "
                        "resulting commit if cleanup removed the branch. Omit for the submitted "
                        "commit when no other reference exists."
                    ),
                },
                "note": {
                    "type": "string",
                    "description": (
                        "For needs_changes or failed: what prevents the reviewed change from "
                        "integrating. Omit when nothing needs explaining."
                    ),
                },
                "base_commit": {
                    "type": "string",
                    "description": (
                        "For integrated after changed commits landed: the target commit before "
                        "integration. Omit when the reviewed commits were unchanged."
                    ),
                },
            },
            "required": ["outcome"],
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
        "reviewed": "Review recorded; the Factory handles the next step. This turn ends here.",
        "integrated": "Integration recorded. This turn ends here.",
        "integration_changes": (
            "Integration changes require another submission and review. This turn ends here."
        ),
        "integration_review": (
            "Changed integration recorded; an independent Reviewer takes over. This turn ends here."
        ),
        "post_integration_review": (
            "Changed integration is already on the base; a post-integration Reviewer takes over. "
            "This turn ends here."
        ),
        "integration_failed": (
            "Integration could not finish; the Card waits for the user. This turn ends here."
        ),
        "integration_waiting": (
            "Changed integration recorded; the Card waits for the user to review it. "
            "This turn ends here."
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


def integration_base_required() -> Json:
    return tool_failure(
        "integration_base_required",
        "The Card now records that the changed integration has already landed; no review was "
        "started. Its pre-integration base is unknown. Call factory_report_integration with "
        "outcome integrated and base_commit set to the exact target commit before integration, "
        "or call factory_report_blocked if you cannot establish it.",
    )


ERROR_MESSAGES = {
    "participant_required": (
        "Nothing was changed. This Tool is available only to the Factory participant assigned "
        "to the current Card phase."
    ),
    "stale_run": (
        "Nothing was changed. This Run no longer owns the Card phase. "
        "Read the Card on the Board for its current state."
    ),
    "wrong_state": (
        "Nothing was changed. This action does not apply to the Card's current state. "
        "Read the Card on the Board for its available actions."
    ),
    "missing_text": "Nothing was changed. Supply non-empty text for {field}.",
    "missing_result": (
        "Nothing was changed. Your stage produces a document: supply result with the complete "
        "document."
    ),
    "missing_changes": (
        "Nothing was changed. Your stage changes the repository: commit the change on a local "
        "branch and supply branch, base and checks."
    ),
    "invalid_verdict": (
        "Nothing was changed. Use verdict approve with a recommendation, "
        "or changes_requested with concrete findings."
    ),
    "invalid_outcome": "Nothing was changed. Use outcome integrated, needs_changes or failed.",
    "invalid_reference": (
        "Nothing was changed. Supply a public http or https integration URL, "
        "or the merge commit reference."
    ),
    "invalid_ref": (
        "Nothing was changed. {value!r} is not a branch name. "
        "Submit the exact branch name shown by git branch."
    ),
    "missing_branch": (
        "Nothing was changed. Local branch {value!r} does not exist. "
        "Commit the isolated change on a local branch and submit that branch."
    ),
    "missing_base": (
        "Nothing was changed. Base {value!r} cannot be resolved. "
        "Submit a base branch that exists in this repository."
    ),
    "no_commits": (
        "Nothing was changed. The submitted branch has no commits beyond its base. "
        "Commit the isolated change before submitting it."
    ),
    "unrelated_history": (
        "Nothing was changed. The branch and base have no common commit. "
        "Submit a branch based on this Project's base branch."
    ),
    "review_base_changed": (
        "Nothing was changed. The captured review base is no longer an ancestor of this head. "
        "If integration changes the reviewed commits, call factory_report_integration with "
        "outcome needs_changes before landing them, then submit the changed work for review."
    ),
    "git_unavailable": (
        "Nothing was changed. Git could not inspect the Project repository. "
        "Make Git and the repository available, then submit again."
    ),
    "git_timeout": (
        "Nothing was changed. Git inspection exceeded its time limit. "
        "Check that the repository is available before submitting again."
    ),
    "git_output_limit": (
        "Nothing was changed. Git inspection returned more data than its bounded result allows. "
        "Check the repository before submitting again."
    ),
    "git_failed": (
        "Nothing was changed. Git could not verify the submitted references. "
        "Check the branch and base in the Project repository before submitting again."
    ),
    "integration_not_found": (
        "The reviewed commit was not found on the base branch. "
        "The Card waits for you to check the integration."
    ),
    "integration_branch_missing": (
        "The submitted branch is missing and the reviewed commit is not on the base branch. "
        "Check the integration before retrying."
    ),
    "budget_exhausted": (
        "The participant reached its turn budget without a hand-in. "
        "Send the Card back with guidance to continue."
    ),
    "run_failed": (
        "The participant Run failed or was cancelled. "
        "Inspect its Session, then retry or send the Card back."
    ),
    "review_failed": (
        "The review did not produce a verdict after a nudge and one fresh reviewer. "
        "Inspect their Sessions, then retry the review."
    ),
    "integration_unreviewed": (
        "Integration changed the reviewed commits, and no review stage of this workflow reviews "
        "the integrating stage. Review the changed commits before accepting."
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
        "This Card started before Workflows and cannot resume its review or integration. "
        "Retry starts its changes stage again with a new participant, Send back reworks it "
        "with your guidance, and Discard removes its worktree."
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
        (
            "The outcome is not confirmed. Read this Card on the Board "
            "before repeating the phase hand-in."
        )
        if participant
        else (
            "The outcome is not confirmed. Read the Board with factory_card action list "
            "before repeating a create call."
        ),
    )
