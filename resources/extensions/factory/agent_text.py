"""Agent-visible Factory instructions, definitions, continuations and outcomes."""

from __future__ import annotations

import json
from typing import Any

from core.tools import tool_failure, tool_success

type Json = dict[str, Any]

WORKER_RULES = (
    "Work in isolation according to the Project instructions. Do not integrate into the base "
    "branch before the user accepts the Card. Run the checks the Project prescribes, then call "
    "factory_submit_work. If you need a decision or information from the user, call "
    "factory_report_blocked."
)

REVIEWER_RULES = (
    "Review the submitted commit range without changing files or branches. Judge whether the "
    "idea is worth having and whether the implementation is correct, simple and fits the Project "
    "conventions. Check whether previous findings were addressed, then call factory_submit_review. "
    "Your recommendation tells the user whether to accept and what you would add or change."
)

_INTEGRATION_REVIEW_GATE = (
    "If integration needs code changes or would rewrite a reviewed commit, including by rebase, "
    "call factory_report_integration with outcome needs_changes before updating the base. "
    "A changed commit SHA needs review even when its patch is unchanged."
)


def _data(label: str, value: Any) -> str:
    # Delimit untrusted Card/review text once; it cannot impersonate a continuation.
    text = (
        json.dumps(value, ensure_ascii=False, indent=2)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
    )
    return f"{label} (data):\n{text}"


def worker_input(card: Json) -> str:
    return "\n\n".join(
        (
            "Implement this Factory Card.",
            _data(
                "Card",
                {key: card.get(key, "") for key in ("id", "project_id", "title", "description")},
            ),
            _data("Default base branch", card["runtime"]["setup"].get("base_branch", "main")),
            _data("User comments", card.get("comments", [])),
        )
    )


def reviewer_input(card: Json, reviews: list[Json]) -> str:
    landing = card["runtime"].get("integration_landing")
    after_landing = isinstance(landing, dict) and landing.get("head_sha") == card["head_sha"]
    parts = [
        "Independently review this already integrated change."
        if after_landing
        else "Independently review this Factory submission.",
        _data(
            "Card",
            {key: card.get(key, "") for key in ("id", "project_id", "title", "description")},
        ),
        _data(
            "Submission",
            {
                key: card.get(key, "")
                for key in (
                    "base",
                    "branch",
                    "base_sha",
                    "head_sha",
                    "summary",
                    "checks",
                )
            }
            | {"range": f"{card['base_sha']}..{card['head_sha']}"},
        ),
        _data(
            "Previous review findings",
            [
                {"round": review["round"], "findings": review.get("findings", [])}
                for review in reviews
            ],
        ),
    ]
    if after_landing:
        parts.append(_data("Integration landing", landing))
    return "\n\n".join(parts)


def note(kind: str, card: Json, text: str = "") -> str:
    notes = {
        "keep_working": (
            "Continue implementing the Card, then call factory_submit_work. If you need the user's "
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
        "review_retry": (
            "The previous Reviewer did not submit a verdict. Review the submission independently "
            "and call factory_submit_review."
        ),
        "rework": (
            "The Reviewer requested changes. Address the findings in your isolated change, run "
            "the prescribed checks, then call factory_submit_work. The Card has not been accepted "
            "for integration."
        ),
        "send_back": (
            "The user sent the Card back. Act on their comment in your isolated change, then call "
            "factory_submit_work. The Card has not been accepted for integration."
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
    return f"{content}\n\n{_data('Comment or findings', text)}" if text else content


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
        "Submit your isolated change for independent review and end this turn; "
        "this does not authorize integration.",
        {
            "type": "object",
            "properties": {
                "branch": {
                    "type": "string",
                    "description": "The local branch containing the finished change.",
                },
                "base": {"type": "string", "description": "The branch the change is based on."},
                "summary": {"type": "string"},
                "checks": {
                    "type": "string",
                    "description": (
                        "Which prescribed checks ran and their outcomes; name any not run."
                    ),
                },
            },
            "required": ["branch", "base", "summary", "checks"],
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
        "Submit your independent verdict and end this turn. An approval sends your recommendation "
        "to the user; requested changes return to the Worker within the review limit, but go "
        "directly to the user after landing.",
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
                        "Required and non-empty for changes_requested: concrete issues the Worker "
                        "can address. Omit when there are no findings."
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
            "Card created and queued; work continues independently within the WIP limit. "
            "Progress and review results appear on the Board, not in this Session."
        ),
        "proposal": "Proposal saved; work waits for the user's approval on the Board.",
        "submitted": (
            "Submission recorded; an independent Reviewer takes over. This turn ends here."
        ),
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
    }
    data: Json = {"message": text or messages.get(kind, "Factory action recorded.")}
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
        "The Worker reached its turn budget without a hand-in. "
        "Send the Card back with guidance to continue."
    ),
    "run_failed": (
        "The participant Run failed or was cancelled. "
        "Inspect its Session, then retry or send the Card back."
    ),
    "review_failed": (
        "The Reviewers did not produce a verdict after a nudge and one fresh Reviewer. "
        "Inspect their Sessions, then retry the review."
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
