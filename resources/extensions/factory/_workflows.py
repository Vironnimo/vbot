"""Factory Workflows: validated definitions, starter templates and step navigation.

A Workflow is global. It optionally fixes the Project its Cards work in, holds
triggers that create Cards on a schedule or webhook, and is a graph of steps.
A step starts once every step in its ``after`` is done, so independent steps
run in parallel; with ``approval`` it first waits for the user. An ``agent``
step has an Agent do its task; with ``returns_to`` it can send an earlier
step's work back, at most ``rounds`` times before the user decides. A
``workflow`` step starts another Workflow as a child Card and, with ``wait``,
is done when that Card is. ``final_approval`` makes a finished Card wait for
the user's acceptance.
"""

from __future__ import annotations

import re
import secrets
from typing import Any

from . import _limits
from .store import FactoryError

type Json = dict[str, Any]

SOURCES = ("agent", "team")
KINDS = ("agent", "workflow")
TRIGGER_TYPES = ("schedule", "webhook")
#: A trigger's ``project`` that resolves the Project from the event's repository.
REPOSITORY_PROJECT = "@repository"
MAX_WORKFLOWS = 200
MAX_STEPS = 30
MAX_TRIGGERS = 10
MAX_DEPTH = 5
DEFAULT_ROUNDS = 2
_ID = re.compile(r"[a-z0-9]{1,24}")

IMPLEMENT_TASK = (
    "Implement the Card's objective in the repository, run the checks the Project "
    "prescribes and commit the finished change."
)
INTEGRATE_TASK = (
    "Integrate the reviewed change into the base branch as the Project instructions "
    "describe, then clean up the worktree and branch it leaves behind. Name what landed "
    "in your summary."
)
REVIEW_TASK = (
    "Review the result of the step before you without changing files or branches. Judge "
    "whether it achieves the Card's objective and whether the approach is sound, correct "
    "and simple; review repository changes against the Project conventions. Hand in your "
    "assessment and what you would add or change as result, or request changes with "
    "concrete findings."
)
PUSH_REVIEW_TASK = (
    "Review the commits of the push described in the trigger event without changing files "
    "or branches. Check out nothing; read the commits with git. Judge correctness, risks "
    "and whether the change follows the Project conventions. Hand in your findings as "
    "result, most important first."
)
PLAN_TASK = (
    "Write an implementation plan for the Card: the approach, the parts it changes, the "
    "risks and how to verify the result. Investigate as needed, but do not change files or "
    "branches. Hand in the complete plan as result."
)
MERGE_TASK = (
    "Combine the draft plans into one plan. Keep the strongest approach, take the good "
    "ideas of the others and resolve their contradictions. Hand in the complete plan as "
    "result."
)
DO_TASK = "Do what the Card asks for and hand in the result."


def new_id(prefix: str) -> str:
    alphabet = "abcdefghijklmnopqrstuvwxyz0123456789"
    return prefix + "".join(secrets.choice(alphabet) for _ in range(7))


def _step(
    step_id: str,
    name: str,
    task: str,
    after: list[str],
    *,
    returns_to: str = "",
    approval: bool = False,
    rounds: int = DEFAULT_ROUNDS,
) -> Json:
    return {
        "id": step_id,
        "name": name,
        "kind": "agent",
        "source": "agent",
        "agent": "",
        "model": "",
        "effort": "",
        "task": task,
        "after": after,
        "approval": approval,
        "returns_to": returns_to,
        "rounds": rounds,
    }


def _template(
    template_id: str,
    name: str,
    description: str,
    steps: list[Json],
    *,
    final_approval: bool = False,
    triggers: list[Json] | None = None,
) -> Json:
    return {
        "id": template_id,
        "name": name,
        "description": description,
        "enabled": True,
        "project_id": "",
        "final_approval": final_approval,
        "steps": steps,
        "triggers": triggers or [],
    }


def templates() -> list[Json]:
    """Starter Workflows with no Agents chosen; the page copies one into a new Workflow."""
    return [
        _template(
            "single",
            "One Agent",
            "One Agent does the Card's task.",
            [_step("do", "Do it", DO_TASK, [])],
        ),
        _template(
            "pushreview",
            "Review pushes",
            "Each push to the repository gets a review. Point the repository's webhook "
            "at this Workflow's trigger.",
            [_step("review", "Review", PUSH_REVIEW_TASK, [])],
            triggers=[
                {
                    "id": "push",
                    "type": "webhook",
                    "schedule": "",
                    "enabled": True,
                    "project": REPOSITORY_PROJECT,
                    "title": "Review push",
                    "task": "Review this push.",
                }
            ],
        ),
        _template(
            "implement",
            "Implement",
            "Implement the Card and review it; it is integrated after your approval.",
            [
                _step("implement", "Implement", IMPLEMENT_TASK, []),
                _step("review", "Review", REVIEW_TASK, ["implement"], returns_to="implement"),
                _step("integrate", "Integrate", INTEGRATE_TASK, ["review"], approval=True),
            ],
        ),
        _template(
            "plan",
            "Plan",
            "Write a plan, then an independent review of the plan.",
            [
                _step("plan", "Plan", PLAN_TASK, []),
                _step("review", "Review", REVIEW_TASK, ["plan"], returns_to="plan"),
            ],
            final_approval=True,
        ),
        _template(
            "drafts",
            "Plan from drafts",
            "Three independent draft plans in parallel, merged into one, then reviewed.",
            [
                _step("draft1", "Draft 1", PLAN_TASK, []),
                _step("draft2", "Draft 2", PLAN_TASK, []),
                _step("draft3", "Draft 3", PLAN_TASK, []),
                _step("merge", "Merge plans", MERGE_TASK, ["draft1", "draft2", "draft3"]),
                _step("review", "Review", REVIEW_TASK, ["merge"], returns_to="merge"),
            ],
            final_approval=True,
        ),
    ]


def _refuse(where: str, problem: str) -> FactoryError:
    return FactoryError("invalid_workflow", f"{where}: {problem}")


def _text(value: Any, where: str, field: str, maximum: int, *, required: bool) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise _refuse(where, f"{field} must be text.")
    text: str = value.strip() if field in {"name", "id", "title"} else value
    if required and not text.strip():
        raise _refuse(where, f"{field} is empty. Give it a {field}.")
    if len(text) > maximum:
        raise _refuse(where, f"{field} exceeds {maximum} characters. Shorten it.")
    return text


def _flag(value: Any, default: bool) -> bool:
    return default if value is None else value is True


def normalize(raw: Any, *, workflow_id: str = "") -> Json:
    """Validate a Workflow's structure; Agents, Models, Projects and targets are checked later.

    ``workflow_id`` keeps the id of a saved Workflow; a new one gets a fresh id
    unless it brings a valid one.
    """
    if not isinstance(raw, dict):
        raise FactoryError("invalid_workflow", "A Workflow must be an object.")
    name = _text(raw.get("name"), "Workflow", "name", 80, required=True)
    where = f"Workflow {name!r}"
    if not workflow_id:
        candidate = raw.get("id")
        workflow_id = (
            candidate if isinstance(candidate, str) and _ID.fullmatch(candidate) else new_id("w")
        )
    steps = raw.get("steps")
    if not isinstance(steps, list) or not steps:
        raise _refuse(where, "it has no steps. Add a step.")
    if len(steps) > MAX_STEPS:
        raise _refuse(where, f"it has more than {MAX_STEPS} steps.")
    project_id = _text(raw.get("project_id"), where, "project_id", 200, required=False).strip()
    normalized: Json = {
        "id": workflow_id,
        "name": name,
        "description": _text(raw.get("description"), where, "description", 2000, required=False),
        "enabled": _flag(raw.get("enabled"), True),
        "project_id": project_id,
        "final_approval": _flag(raw.get("final_approval"), False),
        "limits": _limits.normalize(raw.get("limits")),
        "steps": _steps(steps, where, workflow_id),
        "triggers": _triggers(raw.get("triggers"), where),
    }
    if not normalized["limits"]:
        normalized.pop("limits")
    return normalized


def _steps(steps: list[Any], where: str, workflow_id: str) -> list[Json]:
    result: list[Json] = []
    # References may use a step's id or its name; both resolve to the final id.
    known: dict[str, str] = {}
    names: set[str] = set()
    for position, raw in enumerate(steps, 1):
        if not isinstance(raw, dict):
            raise _refuse(f"{where}, step {position}", "it must be an object.")
        name = _text(raw.get("name"), f"{where}, step {position}", "name", 80, required=True)
        here = f"{where}, step {name!r}"
        if name.casefold() in names:
            raise _refuse(here, "another step has the same name. Rename one.")
        step_id = raw.get("id")
        taken = {item["id"] for item in result}
        if not isinstance(step_id, str) or not _ID.fullmatch(step_id) or step_id in taken:
            step_id = new_id("s")
            while step_id in taken:
                step_id = new_id("s")
        kind = raw.get("kind") or "agent"
        if kind not in KINDS:
            raise _refuse(here, "kind must be agent or workflow.")
        after_value = raw.get("after")
        if after_value is None:
            after = [result[-1]["id"]] if result else []
        elif isinstance(after_value, list):
            after = []
            for item in after_value:
                resolved = _resolve(known, item, "after", here)
                if resolved not in after:
                    after.append(resolved)
        else:
            raise _refuse(here, "after must be a list of earlier steps.")
        step: Json = {
            "id": step_id,
            "name": name,
            "kind": kind,
            "after": after,
            "approval": raw.get("approval") is True,
        }
        if kind == "agent":
            source = raw.get("source") or "agent"
            if source not in SOURCES:
                raise _refuse(here, "source must be agent or team.")
            returns_to = ""
            if raw.get("returns_to") not in (None, ""):
                returns_to = _resolve(known, raw["returns_to"], "returns_to", here)
                if returns_to not in _closure(result, after):
                    raise _refuse(
                        here,
                        "it can only send work back to a step it comes after, directly or "
                        "through other steps. Add that step to after, or choose another one.",
                    )
            rounds = raw.get("rounds", DEFAULT_ROUNDS)
            if isinstance(rounds, float) and rounds.is_integer():
                rounds = int(rounds)
            if not isinstance(rounds, int) or isinstance(rounds, bool) or not 1 <= rounds <= 20:
                raise _refuse(here, "rounds must be a whole number from 1 to 20.")
            step.update(
                source=source,
                agent=_text(raw.get("agent"), here, "agent", 200, required=False).strip(),
                model=_text(raw.get("model"), here, "model", 300, required=False).strip(),
                effort=_text(raw.get("effort"), here, "effort", 40, required=False).strip(),
                task=_text(raw.get("task"), here, "task", 20000, required=True),
                returns_to=returns_to,
                rounds=rounds,
            )
            limits = _limits.normalize(raw.get("limits"))
            if limits:
                step["limits"] = limits
        else:
            target = _text(raw.get("workflow"), here, "workflow", 24, required=True).strip()
            if target == workflow_id:
                raise _refuse(here, "a Workflow cannot start itself. Choose another Workflow.")
            step.update(
                workflow=target,
                wait=_flag(raw.get("wait"), True),
                task=_text(raw.get("task"), here, "task", 20000, required=False),
            )
        result.append(step)
        names.add(name.casefold())
        known[step_id] = step_id
        known.setdefault(name.casefold(), step_id)
        if isinstance(raw.get("id"), str) and raw["id"].strip():
            known.setdefault(raw["id"].strip(), step_id)
    return result


def _triggers(value: Any, where: str) -> list[Json]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise _refuse(where, "triggers must be a list.")
    if len(value) > MAX_TRIGGERS:
        raise _refuse(where, f"it has more than {MAX_TRIGGERS} triggers.")
    result: list[Json] = []
    for position, raw in enumerate(value, 1):
        here = f"{where}, trigger {position}"
        if not isinstance(raw, dict):
            raise _refuse(here, "it must be an object.")
        kind = raw.get("type")
        if kind not in TRIGGER_TYPES:
            raise _refuse(here, "type must be schedule or webhook.")
        trigger_id = raw.get("id")
        taken = {item["id"] for item in result}
        if not isinstance(trigger_id, str) or not _ID.fullmatch(trigger_id) or trigger_id in taken:
            trigger_id = new_id("t")
        schedule = ""
        if kind == "schedule":
            schedule = _text(raw.get("schedule"), here, "schedule", 200, required=True).strip()
            if schedule.casefold() == "webhook":
                raise _refuse(here, "use type webhook for a webhook trigger.")
        result.append(
            {
                "id": trigger_id,
                "type": kind,
                "schedule": schedule,
                "enabled": _flag(raw.get("enabled"), True),
                "project": _text(raw.get("project"), here, "project", 200, required=False).strip(),
                "title": _text(raw.get("title"), here, "title", 200, required=False),
                "task": _text(raw.get("task"), here, "task", 20000, required=False),
            }
        )
    return result


def _resolve(known: dict[str, str], value: Any, field: str, here: str) -> str:
    reference = value.strip() if isinstance(value, str) else ""
    found = known.get(reference) or known.get(reference.casefold())
    if found is None:
        raise _refuse(
            here,
            f"{field} names {value!r}, which is not a step before it. "
            "A step can only follow or return to steps listed above it.",
        )
    return found


def _closure(steps: list[Json], start: list[str]) -> set[str]:
    by_id = {step["id"]: step for step in steps}
    found: set[str] = set()
    todo = list(start)
    while todo:
        current = todo.pop()
        if current in found or current not in by_id:
            continue
        found.add(current)
        todo.extend(by_id[current]["after"])
    return found


def model_key(workflow: Json, step: Json) -> str:
    return f"{workflow['id']}/{step['id']}"


def profile(step: Json) -> Json:
    """The ``TemporaryAgentConfig`` Profile fields of a step's chosen Agent."""
    if step.get("source", "agent") == "agent":
        return {"identity_profile": step["agent"]}
    return {"repository_profile": step["agent"]}


def find(workflows: list[Json], requested: str) -> Json:
    """A Workflow by id or name."""
    wanted = requested.strip()
    found = next((item for item in workflows if item["id"] == wanted), None) or next(
        (item for item in workflows if item["name"].casefold() == wanted.casefold()), None
    )
    if found is None:
        names = ", ".join(item["name"] for item in workflows if item["enabled"]) or "(none)"
        raise FactoryError(
            "workflow_not_found",
            f"Workflow {requested!r} does not exist. Use one of: {names}.",
        )
    return found


# Navigation over a Card's Workflow snapshot.


def step(workflow: Json, step_id: str) -> Json:
    return next(item for item in workflow["steps"] if item["id"] == step_id)


def ancestors(workflow: Json, step_id: str) -> set[str]:
    """Every step this step follows, directly or through other steps."""
    return _closure(workflow["steps"], step(workflow, step_id)["after"])


def dependents(workflow: Json, step_id: str) -> set[str]:
    """Every step that follows this step, directly or through other steps."""
    return {item["id"] for item in workflow["steps"] if step_id in ancestors(workflow, item["id"])}


def sinks(workflow: Json) -> list[Json]:
    """The steps no other step follows: a finished Card's results."""
    followed = {item for step_ in workflow["steps"] for item in step_["after"]}
    return [item for item in workflow["steps"] if item["id"] not in followed]


def participant(step_: Json, sequence: int) -> str:
    """A step's participant: one Session for its whole Card, or a fresh one per run
    for a step that judges earlier work."""
    return f"{step_['id']}-r{sequence}" if step_.get("returns_to") else step_["id"]


def group_id(card_id: str, step_id: str) -> str:
    """The execution group of one step of a Card."""
    return f"{card_id}.{step_id}"


def split_group(value: str) -> tuple[str, str]:
    """(card id, step id) of a step's execution group; a legacy Card group has no step."""
    card_id, _, step_id = value.rpartition(".")
    return (card_id, step_id) if card_id else (value, "")
