"""Factory Workflows: validated definitions, starter templates and step navigation.

A Workflow is a list of steps. Each step names the Agent that does it, its task
and when it starts: with the Card, after the steps listed in ``after``, and with
``approval`` only once the user approves. A step with ``returns_to`` can send
an earlier step's work back for rework, at most ``rounds`` times before the user
decides. Steps run one at a time in list order, which ``after`` keeps
consistent: a step can only follow steps listed before it.
"""

from __future__ import annotations

import re
import secrets
from copy import deepcopy
from typing import Any

from .store import FactoryError

type Json = dict[str, Any]

SOURCES = ("agent", "team")
MAX_WORKFLOWS = 30
MAX_STEPS = 20
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
_DOCUMENT_TASK = (
    "Do the work the Card asks for without changing files or branches and hand in the "
    "complete document as result."
)


def _new_id(prefix: str) -> str:
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
    agent: Json | None = None,
    rounds: int = DEFAULT_ROUNDS,
) -> Json:
    chosen = agent or {}
    return {
        "id": step_id,
        "name": name,
        "source": chosen.get("source") or "agent",
        "agent": chosen.get("agent") or "",
        "model": chosen.get("model") or "",
        "effort": chosen.get("effort") or "",
        "task": task,
        "after": after,
        "approval": approval,
        "returns_to": returns_to,
        "rounds": rounds,
    }


def templates() -> list[Json]:
    """Starter Workflows with no Agents chosen; the page copies one into a setup."""
    return [
        {
            "id": "implement",
            "name": "Implement",
            "description": (
                "Implement the Card and review it; it is integrated after your approval."
            ),
            "steps": [
                _step("implement", "Implement", IMPLEMENT_TASK, []),
                _step("review", "Review", REVIEW_TASK, ["implement"], returns_to="implement"),
                _step("integrate", "Integrate", INTEGRATE_TASK, ["review"], approval=True),
            ],
        },
        {
            "id": "plan",
            "name": "Plan",
            "description": "Write a plan, then an independent review of the plan.",
            "steps": [
                _step("plan", "Plan", PLAN_TASK, []),
                _step("review", "Review", REVIEW_TASK, ["plan"], returns_to="plan"),
            ],
        },
        {
            "id": "drafts",
            "name": "Plan from drafts",
            "description": "Three independent draft plans, merged into one, then reviewed.",
            "steps": [
                _step("draft1", "Draft 1", PLAN_TASK, []),
                _step("draft2", "Draft 2", PLAN_TASK, []),
                _step("draft3", "Draft 3", PLAN_TASK, []),
                _step("merge", "Merge plans", MERGE_TASK, ["draft1", "draft2", "draft3"]),
                _step("review", "Review", REVIEW_TASK, ["merge"], returns_to="merge"),
            ],
        },
        {
            "id": "planimpl",
            "name": "Plan, then implement",
            "description": (
                "A reviewed plan waits for your approval, then implementation and review; "
                "the change is integrated after your approval."
            ),
            "steps": [
                _step("plan", "Plan", PLAN_TASK, []),
                _step("planreview", "Plan review", REVIEW_TASK, ["plan"], returns_to="plan"),
                _step("implement", "Implement", IMPLEMENT_TASK, ["planreview"], approval=True),
                _step("review", "Review", REVIEW_TASK, ["implement"], returns_to="implement"),
                _step("integrate", "Integrate", INTEGRATE_TASK, ["review"], approval=True),
            ],
        },
    ]


def legacy_workflow(row: Json) -> Json:
    """The Workflow equivalent of a setup saved with one Worker and one Reviewer."""

    def agent(role: str) -> Json:
        return {
            "source": row.get(f"{role}_source") or "team",
            "agent": row.get(f"{role}_profile") or "",
            "model": row.get(f"{role}_model") or "",
            "effort": row.get(f"{role}_effort") or "",
        }

    rounds = max(1, int(row.get("review_rounds") or 3) - 1)
    return {
        "id": "implement",
        "name": "Implement",
        "description": "Implement the Card and review it; it is integrated after your approval.",
        "steps": [
            _step("implement", "Implement", IMPLEMENT_TASK, [], agent=agent("worker")),
            _step(
                "review",
                "Review",
                REVIEW_TASK,
                ["implement"],
                returns_to="implement",
                agent=agent("reviewer"),
                rounds=rounds,
            ),
            _step(
                "integrate",
                "Integrate",
                INTEGRATE_TASK,
                ["review"],
                approval=True,
                agent=agent("worker"),
            ),
        ],
    }


def from_stages(workflow: Json) -> Json:
    """Convert a Workflow saved as stages with lanes into steps.

    Every lane becomes a step after all lanes of the previous stage. A review
    returns to its target's first lane, a stage's approval gate moves to the
    steps after it, and review rounds become send-backs (the last round used to
    end in the user's decision). A Workflow that changed the repository used to
    integrate after the user accepted; it ends in an Integrate step that waits
    for approval, done by the Agent of the last changes stage.
    """
    steps: list[Json] = []
    previous: list[str] = []
    first_lane: dict[str, str] = {}
    names: set[str] = set()
    gate = False
    integrator: Json | None = None
    for stage in workflow.get("stages") or []:
        if not isinstance(stage, dict):
            continue
        lanes = [lane for lane in stage.get("lanes") or [] if isinstance(lane, dict)] or [{}]
        stage_id = str(stage.get("id") or _new_id("s"))[:22]
        name = str(stage.get("name") or "Step")
        instructions = str(stage.get("instructions") or "").strip()
        review = stage.get("kind") == "review"
        if instructions:
            task = instructions
        elif review:
            task = REVIEW_TASK
        elif stage.get("output") == "document":
            task = _DOCUMENT_TASK
        else:
            task = IMPLEMENT_TASK
        if not review and stage.get("output") != "document":
            integrator = lanes[0]
        ids = []
        for index, lane in enumerate(lanes, 1):
            step_id = stage_id if len(lanes) == 1 else f"{stage_id}{index}"
            label = name if len(lanes) == 1 else f"{name} {index}"
            while label.casefold() in names:
                label += " 2"
            names.add(label.casefold())
            step = _step(
                step_id,
                label,
                task,
                list(previous),
                approval=gate,
                agent=lane,
            )
            if review:
                step["returns_to"] = first_lane.get(str(stage.get("target") or ""), "")
                rounds = stage.get("rounds", 3)
                step["rounds"] = max(1, min(20, int(rounds) - 1 if isinstance(rounds, int) else 2))
            ids.append(step_id)
            steps.append(step)
        first_lane[str(stage.get("id") or "")] = ids[0]
        previous = ids
        gate = stage.get("approval") is True
    if integrator is not None and steps:
        step_id = "integrate"
        while step_id in {step["id"] for step in steps}:
            step_id = _new_id("s")
        label = "Integrate"
        while label.casefold() in names:
            label += " 2"
        steps.append(
            _step(step_id, label, INTEGRATE_TASK, list(previous), approval=True, agent=integrator)
        )
    return {
        "id": workflow.get("id", ""),
        "name": workflow.get("name", ""),
        "description": workflow.get("description", ""),
        "steps": steps,
    }


def _refuse(where: str, problem: str) -> FactoryError:
    return FactoryError("invalid_workflow", f"{where}: {problem}")


def _text(value: Any, where: str, field: str, maximum: int, *, required: bool) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise _refuse(where, f"{field} must be text.")
    text: str = value.strip() if field in {"name", "id"} else value
    if required and not text.strip():
        raise _refuse(where, f"{field} is empty. Give it a {field}.")
    if len(text) > maximum:
        raise _refuse(where, f"{field} exceeds {maximum} characters. Shorten it.")
    return text


def normalize(workflows: Any) -> list[Json]:
    """Validate the structure of a Project's Workflows; Agents and Models are checked later."""
    if not isinstance(workflows, list) or not workflows:
        raise FactoryError(
            "invalid_workflow", "A setup needs at least one Workflow. Add one in the setup."
        )
    if len(workflows) > MAX_WORKFLOWS:
        raise FactoryError(
            "invalid_workflow", f"A setup holds at most {MAX_WORKFLOWS} Workflows. Remove some."
        )
    result: list[Json] = []
    workflow_ids: set[str] = set()
    names: set[str] = set()
    for position, raw in enumerate(workflows, 1):
        if not isinstance(raw, dict):
            raise _refuse(f"Workflow {position}", "it must be an object.")
        if "steps" not in raw and "stages" in raw:
            raw = {**raw, **from_stages(raw)}
        name = _text(raw.get("name"), f"Workflow {position}", "name", 80, required=True)
        where = f"Workflow {name!r}"
        if name.casefold() in names:
            raise _refuse(where, "another Workflow has the same name. Rename one.")
        names.add(name.casefold())
        workflow_id = raw.get("id")
        if not isinstance(workflow_id, str) or not _ID.fullmatch(workflow_id):
            workflow_id = _new_id("w")
        while workflow_id in workflow_ids:
            workflow_id = _new_id("w")
        workflow_ids.add(workflow_id)
        steps = raw.get("steps")
        if not isinstance(steps, list) or not steps:
            raise _refuse(where, "it has no steps. Add a step.")
        if len(steps) > MAX_STEPS:
            raise _refuse(where, f"it has more than {MAX_STEPS} steps.")
        result.append(
            {
                "id": workflow_id,
                "name": name,
                "description": _text(
                    raw.get("description"), where, "description", 2000, required=False
                ),
                "steps": _steps(steps, where),
            }
        )
    return result


def _steps(steps: list[Any], where: str) -> list[Json]:
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
            step_id = _new_id("s")
            while step_id in taken:
                step_id = _new_id("s")
        source = raw.get("source") or "agent"
        if source not in SOURCES:
            raise _refuse(here, "source must be agent or team.")

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
        returns_to = ""
        if raw.get("returns_to") not in (None, ""):
            returns_to = _resolve(known, raw["returns_to"], "returns_to", here)
            if returns_to not in _closure(result, after):
                raise _refuse(
                    here,
                    "it can only send work back to a step it comes after, directly or through "
                    "other steps. Add that step to after, or choose another one.",
                )
        rounds = raw.get("rounds", DEFAULT_ROUNDS)
        if isinstance(rounds, float) and rounds.is_integer():
            rounds = int(rounds)
        if not isinstance(rounds, int) or isinstance(rounds, bool) or not 1 <= rounds <= 20:
            raise _refuse(here, "rounds must be a whole number from 1 to 20.")
        step = {
            "id": step_id,
            "name": name,
            "source": source,
            "agent": _text(raw.get("agent"), here, "agent", 200, required=False).strip(),
            "model": _text(raw.get("model"), here, "model", 300, required=False).strip(),
            "effort": _text(raw.get("effort"), here, "effort", 40, required=False).strip(),
            "task": _text(raw.get("task"), here, "task", 20000, required=True),
            "after": after,
            "approval": raw.get("approval") is True,
            "returns_to": returns_to,
            "rounds": rounds,
        }
        result.append(step)
        names.add(name.casefold())
        known[step_id] = step_id
        known.setdefault(name.casefold(), step_id)
        if isinstance(raw.get("id"), str) and raw["id"].strip():
            known.setdefault(raw["id"].strip(), step_id)
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


def steps(workflows: list[Json]) -> list[tuple[str, Json, Json]]:
    """Every step with its location: (label, workflow, step)."""
    return [
        (f"{workflow['name']} / {step['name']}", workflow, step)
        for workflow in workflows
        for step in workflow["steps"]
    ]


def model_key(workflow: Json, step: Json) -> str:
    return f"{workflow['id']}/{step['id']}"


def profile(step: Json) -> Json:
    """The ``TemporaryAgentConfig`` Profile fields of a step's chosen Agent."""
    if step.get("source", "agent") == "agent":
        return {"identity_profile": step["agent"]}
    return {"repository_profile": step["agent"]}


def choose(setup: Json, requested: str | None) -> Json:
    """The Workflow a new Card uses: by id or name, else the Project's default."""
    workflows = setup.get("workflows") or []
    if requested:
        wanted = requested.strip().casefold()
        found = next(
            (
                item
                for item in workflows
                if item["id"] == requested.strip() or item["name"].casefold() == wanted
            ),
            None,
        )
        if found is None:
            names = ", ".join(item["name"] for item in workflows) or "(none)"
            raise FactoryError(
                "workflow_not_found",
                f"Workflow {requested!r} does not exist in this Project. Use one of: {names}, "
                "or omit workflow for the Project's default.",
            )
        return deepcopy(found)
    default = setup.get("default_workflow", "")
    found = next((item for item in workflows if item["id"] == default), None)
    if found is None and workflows:
        found = workflows[0]
    if found is None:
        raise FactoryError(
            "workflow_not_found",
            "This Project's Factory setup has no Workflow. Add one in Setups.",
        )
    return deepcopy(found)


# Navigation over a Card's Workflow snapshot.


def step(workflow: Json, step_id: str) -> Json:
    return next(item for item in workflow["steps"] if item["id"] == step_id)


def position(workflow: Json, step_id: str) -> int:
    return next(index for index, item in enumerate(workflow["steps"]) if item["id"] == step_id)


def ancestors(workflow: Json, step_id: str) -> set[str]:
    """Every step this step follows, directly or through other steps."""
    return _closure(workflow["steps"], step(workflow, step_id)["after"])


def dependents(workflow: Json, step_id: str) -> set[str]:
    """Every step that follows this step, directly or through other steps."""
    return {item["id"] for item in workflow["steps"] if step_id in ancestors(workflow, item["id"])}


def next_step(workflow: Json, done: list[str]) -> Json | None:
    """The first step not done; every step it follows is listed before it, hence done."""
    return next((item for item in workflow["steps"] if item["id"] not in done), None)


def participant(step: Json, sequence: int) -> str:
    """A step's participant: one Session for its whole Card, or a fresh one per run
    for a step that judges earlier work."""
    return f"{step['id']}-r{sequence}" if step["returns_to"] else step["id"]


def step_id(participant_id: str) -> str:
    return participant_id.split("-")[0]
