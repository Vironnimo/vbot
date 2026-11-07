"""Factory Workflows: validated definitions, starter templates and stage navigation.

A Workflow is an ordered list of Stages. A ``work`` Stage hands in either a
``document`` (read-only on the repository) or ``changes`` (commits on a local
branch, verified with Git). A ``review`` Stage judges an earlier work Stage and
can return it for rework. Every Stage has one or more Lanes: independent
participants, each with its own Agent, Model and effort, that run one after
another in the Project directory without seeing each other's work.
"""

from __future__ import annotations

import re
import secrets
from copy import deepcopy
from typing import Any

from .store import FactoryError

type Json = dict[str, Any]

STAGE_KINDS = ("work", "review")
OUTPUTS = ("changes", "document")
SOURCES = ("agent", "team")
MAX_WORKFLOWS = 30
MAX_STAGES = 20
MAX_LANES = 10
_ID = re.compile(r"[a-z0-9]{1,24}")
_REVIEW_PARTICIPANT = re.compile(r"[a-z0-9]{1,24}-\d+-r\d+")


def _new_id(prefix: str) -> str:
    alphabet = "abcdefghijklmnopqrstuvwxyz0123456789"
    return prefix + "".join(secrets.choice(alphabet) for _ in range(7))


def _lane(agent: str = "", source: str = "agent") -> Json:
    return {"source": source, "agent": agent, "model": "", "effort": ""}


def _work(stage_id: str, name: str, output: str, instructions: str = "", lanes: int = 1) -> Json:
    return {
        "id": stage_id,
        "name": name,
        "kind": "work",
        "output": output,
        "instructions": instructions,
        "lanes": [_lane() for _ in range(lanes)],
        "approval": False,
    }


def _review(stage_id: str, name: str, target: str, rounds: int = 3) -> Json:
    return {
        "id": stage_id,
        "name": name,
        "kind": "review",
        "target": target,
        "rounds": rounds,
        "instructions": "",
        "lanes": [_lane()],
        "approval": False,
    }


_PLAN = (
    "Write an implementation plan for the Card: the approach, the parts it changes, "
    "the risks and how to verify the result."
)
_MERGE = (
    "Combine the draft plans into one plan. Keep the strongest approach, take the good "
    "ideas of the others and resolve their contradictions."
)


def templates() -> list[Json]:
    """Starter Workflows with no Agents chosen; the page copies one into a setup."""
    plan_then_implement = [
        _work("plan", "Plan", "document", _PLAN),
        _review("planreview", "Plan review", "plan", 2),
        _work("implement", "Implement", "changes"),
        _review("review", "Review", "implement"),
    ]
    plan_then_implement[1]["approval"] = True
    return [
        {
            "id": "implement",
            "name": "Implement",
            "description": "Implement the Card, then an independent review.",
            "stages": [
                _work("implement", "Implement", "changes"),
                _review("review", "Review", "implement"),
            ],
        },
        {
            "id": "plan",
            "name": "Plan",
            "description": "Write a plan, then an independent review of the plan.",
            "stages": [
                _work("plan", "Plan", "document", _PLAN),
                _review("review", "Review", "plan", 2),
            ],
        },
        {
            "id": "drafts",
            "name": "Plan from drafts",
            "description": "Three independent draft plans, merged into one, then reviewed.",
            "stages": [
                _work("drafts", "Draft plans", "document", _PLAN, lanes=3),
                _work("merge", "Merge plans", "document", _MERGE),
                _review("review", "Review", "merge", 2),
            ],
        },
        {
            "id": "planimpl",
            "name": "Plan, then implement",
            "description": (
                "A reviewed plan waits for your approval, then implementation and review."
            ),
            "stages": plan_then_implement,
        },
    ]


def legacy_workflow(row: Json) -> Json:
    """The Workflow equivalent of a setup saved with one Worker and one Reviewer."""

    def lane(role: str) -> Json:
        return {
            "source": row.get(f"{role}_source") or "team",
            "agent": row.get(f"{role}_profile") or "",
            "model": row.get(f"{role}_model") or "",
            "effort": row.get(f"{role}_effort") or "",
        }

    implement = _work("implement", "Implement", "changes")
    implement["lanes"] = [lane("worker")]
    review = _review("review", "Review", "implement", int(row.get("review_rounds") or 3))
    review["lanes"] = [lane("reviewer")]
    return {
        "id": "implement",
        "name": "Implement",
        "description": "Implement the Card, then an independent review.",
        "stages": [implement, review],
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
        stages = raw.get("stages")
        if not isinstance(stages, list) or not stages:
            raise _refuse(where, "it has no stages. Add a work stage.")
        if len(stages) > MAX_STAGES:
            raise _refuse(where, f"it has more than {MAX_STAGES} stages.")
        normalized_stages = _stages(stages, where)
        result.append(
            {
                "id": workflow_id,
                "name": name,
                "description": _text(
                    raw.get("description"), where, "description", 2000, required=False
                ),
                "stages": normalized_stages,
            }
        )
    return result


def _stages(stages: list[Any], where: str) -> list[Json]:
    result: list[Json] = []
    ids: dict[str, Json] = {}
    for position, raw in enumerate(stages, 1):
        if not isinstance(raw, dict):
            raise _refuse(f"{where}, stage {position}", "it must be an object.")
        name = _text(raw.get("name"), f"{where}, stage {position}", "name", 80, required=True)
        here = f"{where}, stage {name!r}"
        kind = raw.get("kind")
        if kind not in STAGE_KINDS:
            raise _refuse(here, "kind must be work or review.")
        stage_id = raw.get("id")
        if not isinstance(stage_id, str) or not _ID.fullmatch(stage_id) or stage_id in ids:
            stage_id = _new_id("s")
            while stage_id in ids:
                stage_id = _new_id("s")
        lanes = raw.get("lanes")
        if not isinstance(lanes, list) or not lanes:
            raise _refuse(here, "it has no Agent. Add at least one lane.")
        if len(lanes) > MAX_LANES:
            raise _refuse(here, f"it has more than {MAX_LANES} lanes.")
        stage: Json = {
            "id": stage_id,
            "name": name,
            "kind": kind,
            "instructions": _text(
                raw.get("instructions"), here, "instructions", 20000, required=False
            ),
            "lanes": [
                _lane_value(lane, f"{here}, lane {index}") for index, lane in enumerate(lanes, 1)
            ],
            "approval": raw.get("approval") is True,
        }
        if kind == "work":
            output = raw.get("output", "changes")
            if output not in OUTPUTS:
                raise _refuse(here, "output must be changes or document.")
            stage["output"] = output
        else:
            target = raw.get("target")
            earlier = [item for item in result if item["kind"] == "work"]
            if not earlier:
                raise _refuse(here, "a review needs an earlier work stage to review.")
            chosen = next((item for item in earlier if target in {item["id"], item["name"]}), None)
            if chosen is None:
                if target not in (None, ""):
                    raise _refuse(here, "it reviews a stage that is not an earlier work stage.")
                chosen = earlier[-1]
            rounds = raw.get("rounds", 3)
            if isinstance(rounds, float) and rounds.is_integer():
                rounds = int(rounds)
            if not isinstance(rounds, int) or isinstance(rounds, bool) or not 1 <= rounds <= 20:
                raise _refuse(here, "rounds must be a whole number from 1 to 20.")
            stage.update(target=chosen["id"], rounds=rounds)
        ids[stage_id] = stage
        result.append(stage)
    changes = [
        stage for stage in result if stage["kind"] == "work" and stage["output"] == "changes"
    ]
    if changes and len(changes[-1]["lanes"]) != 1:
        raise _refuse(
            f"{where}, stage {changes[-1]['name']!r}",
            "the last stage that changes the repository integrates the result after "
            "your acceptance, so it needs exactly one lane. Add a single-lane stage "
            "after it that combines the lanes.",
        )
    return result


def _lane_value(raw: Any, where: str) -> Json:
    if not isinstance(raw, dict):
        raise _refuse(where, "it must be an object.")
    source = raw.get("source", "agent")
    if source not in SOURCES:
        raise _refuse(where, "source must be agent or team.")
    return {
        "source": source,
        "agent": _text(raw.get("agent"), where, "agent", 200, required=False).strip(),
        "model": _text(raw.get("model"), where, "model", 300, required=False).strip(),
        "effort": _text(raw.get("effort"), where, "effort", 40, required=False).strip(),
    }


def lanes(workflows: list[Json]) -> list[tuple[str, Json, Json, int, Json]]:
    """Every lane with its location: (label, workflow, stage, index, lane)."""
    result = []
    for workflow in workflows:
        for stage in workflow["stages"]:
            for index, lane in enumerate(stage["lanes"]):
                label = f"{workflow['name']} / {stage['name']}"
                if len(stage["lanes"]) > 1:
                    label += f" / lane {index + 1}"
                result.append((label, workflow, stage, index, lane))
    return result


def lane_key(workflow: Json, stage: Json, index: int) -> str:
    return f"{workflow['id']}/{stage['id']}/{index}"


def profile(lane: Json) -> Json:
    """The ``TemporaryAgentConfig`` Profile fields of a lane's chosen Agent."""
    if lane.get("source", "agent") == "agent":
        return {"identity_profile": lane["agent"]}
    return {"repository_profile": lane["agent"]}


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


# Stage navigation over a Card's Workflow snapshot.


def work_participant(stage: Json, lane: int) -> str:
    return f"{stage['id']}-{lane + 1}"


def review_participant(stage: Json, lane: int, sequence: int) -> str:
    return f"{stage['id']}-{lane + 1}-r{sequence}"


def is_review(participant: str | None) -> bool:
    return bool(participant) and _REVIEW_PARTICIPANT.fullmatch(str(participant)) is not None


def stage_index(workflow: Json, stage_id: str) -> int:
    return next(index for index, stage in enumerate(workflow["stages"]) if stage["id"] == stage_id)


def integrator(workflow: Json) -> int | None:
    """Index of the last stage that changes the repository; its lane integrates."""
    found = None
    for index, stage in enumerate(workflow["stages"]):
        if stage["kind"] == "work" and stage["output"] == "changes":
            found = index
    return found


def integration_review(workflow: Json) -> int | None:
    """Index of the last review stage that reviews the integrating stage."""
    index = integrator(workflow)
    if index is None:
        return None
    target = workflow["stages"][index]["id"]
    found = None
    for position, stage in enumerate(workflow["stages"]):
        if stage["kind"] == "review" and stage["target"] == target:
            found = position
    return found


def send_back_stage(workflow: Json, position: int) -> int:
    """The work stage a user's send-back returns to from the stage at ``position``."""
    stages = workflow["stages"]
    position = min(max(position, 0), len(stages) - 1)
    stage = stages[position]
    if stage["kind"] == "review":
        return stage_index(workflow, stage["target"])
    return position
