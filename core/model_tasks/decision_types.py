"""Provider-neutral inputs and validated results for structured decisions."""

from __future__ import annotations

import json
import math
from typing import Any, cast

from core.utils.errors import TaskError


class DecisionError(TaskError):
    """An expected decision operation failure with a stable public code."""

    def __init__(self, message: str, *, code: str = "invalid_request") -> None:
        super().__init__(message)
        self.code = code


def json_copy(value: Any) -> Any:
    """Detach finite JSON data, rejecting oversized requests before transport."""
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (ValueError, TypeError, RecursionError) as exc:
        raise DecisionError("Supply finite JSON values.") from exc
    if len(encoded.encode("utf-8")) > 1_000_000:
        raise DecisionError("The decision input exceeds the 1 MB limit.")
    return json.loads(encoded)


def text(value: Any, label: str, *, maximum: int = 100_000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise DecisionError(f"{label} must be non-empty text, at most {maximum} characters.")
    return value


def validate_input(state: Any, questions: Any) -> tuple[Any, list[dict[str, Any]]]:
    """Keep state content verbatim; validate complete independent questions."""
    if not isinstance(state, str | dict | list):
        raise DecisionError("state must be text, a JSON object, or a JSON array.")
    if not isinstance(questions, list) or not questions:
        raise DecisionError("questions must contain at least one question.")
    state, questions = json_copy([state, questions])
    ids: set[str] = set()
    for question in questions:
        if not isinstance(question, dict) or set(question) - {
            "id",
            "type",
            "instructions",
            "criteria",
        }:
            raise DecisionError("Each question accepts id, type, instructions, and criteria only.")
        identifier = text(question.get("id"), "Question id", maximum=128)
        if identifier in ids:
            raise DecisionError(f"Question id {identifier!r} is duplicated.")
        ids.add(identifier)
        text(question.get("instructions"), f"Question {identifier} instructions")
        kind = question.get("type")
        criteria = question.get("criteria")
        if kind == "choice":
            if not isinstance(criteria, dict) or len(criteria) < 2:
                raise DecisionError(
                    f"Question {identifier}: choice needs at least two named criteria."
                )
            for key, description in criteria.items():
                text(key, "Choice id", maximum=128)
                text(description, "Choice description")
        elif kind == "score":
            if not isinstance(criteria, list) or not criteria:
                raise DecisionError(
                    f"Question {identifier}: score needs ordered criteria, lowest first."
                )
            for level in criteria:
                text(level, "Score level")
        elif kind == "noul":
            if "criteria" in question:
                if not isinstance(criteria, dict) or set(criteria) != {"true", "false"}:
                    raise DecisionError(
                        f"Question {identifier}: noul criteria need true and false descriptions."
                    )
                for description in criteria.values():
                    text(description, "Noul criterion")
        else:
            raise DecisionError(f"Question {identifier}: type must be choice, score, or noul.")
    return state, questions


def validate_draft(draft: Any) -> dict[str, Any]:
    """Persist incomplete editor values, but never an unrenderable shape."""
    if (
        not isinstance(draft, dict)
        or not {"title", "state", "questions"} <= set(draft)
        or set(draft) - {"title", "state", "questions", "control"}
    ):
        raise DecisionError("An experiment draft needs title, state, and questions.")
    text(draft["title"], "Experiment title", maximum=200)
    if not isinstance(draft["state"], str | dict | list) or not isinstance(
        draft["questions"], list
    ):
        raise DecisionError("Draft state must be text or JSON; questions must be an array.")
    draft = json_copy(draft)
    if "control" in draft:
        _validate_control_draft(draft["control"])
    for question in draft["questions"]:
        if (
            not isinstance(question, dict)
            or set(question) - {"id", "type", "instructions", "criteria"}
            or not isinstance(question.get("id"), str)
            or not isinstance(question.get("instructions"), str)
        ):
            raise DecisionError("Each draft question needs text id and instructions.")
        kind, criteria = question.get("type"), question.get("criteria")
        if kind == "score":
            valid = isinstance(criteria, list) and all(isinstance(v, str) for v in criteria)
        elif kind == "choice":
            valid = isinstance(criteria, dict) and all(
                isinstance(v, str) for v in criteria.values()
            )
        elif kind == "noul":
            valid = "criteria" not in question or (
                isinstance(criteria, dict)
                and set(criteria) == {"true", "false"}
                and all(isinstance(v, str) for v in criteria.values())
            )
        else:
            valid = False
        if not valid:
            raise DecisionError(
                "Draft criteria must match the question type: choice, score, or noul."
            )
    return cast(dict[str, Any], draft)


def _validate_control_draft(value: Any) -> None:
    """Check editor structure; completeness belongs to explicit start validation."""
    if (
        not isinstance(value, dict)
        or set(value)
        != {"instructions", "observe", "actions", "interval_ms", "max_steps", "timeout_seconds"}
        or not isinstance(value["instructions"], str)
        or not isinstance(value["actions"], dict)
    ):
        raise DecisionError("Control setup has an invalid shape.")
    commands = [value["observe"]]
    for action in value["actions"].values():
        if (
            not isinstance(action, dict)
            or set(action) != {"description", "command"}
            or not isinstance(action["description"], str)
        ):
            raise DecisionError("Each action needs a text description and a command or null.")
        if action["command"] is not None:
            commands.append(action["command"])
    for command in commands:
        if (
            not isinstance(command, dict)
            or set(command) != {"argv", "cwd"}
            or not isinstance(command["cwd"], str)
            or not isinstance(command["argv"], list)
            or not command["argv"]
            or not all(isinstance(arg, str) for arg in command["argv"])
        ):
            raise DecisionError(
                "Each command needs a text cwd and a non-empty argv array of strings."
            )
    for key in ("interval_ms", "max_steps", "timeout_seconds"):
        if value[key] != "" and not finite_number(value[key]):
            raise DecisionError(f"{key} must be a non-negative number or an empty draft field.")


def finite_number(value: Any, *, minimum: float = 0, maximum: float = math.inf) -> bool:
    return (
        isinstance(value, int | float)
        and not isinstance(value, bool)
        and math.isfinite(value)
        and minimum <= value <= maximum
    )


def validate_answers(payload: Any, questions: list[dict[str, Any]]) -> dict[str, Any]:
    """Reject mismatched, incomplete, or out-of-range Provider decisions."""

    def invalid() -> DecisionError:
        return DecisionError(
            "The Provider returned an invalid or incomplete decision result.", code="invalid_result"
        )

    if not isinstance(payload, dict):
        raise invalid()
    answers = payload.get("answers")
    if not isinstance(answers, dict) or set(answers) != {q["id"] for q in questions}:
        raise invalid()
    result: dict[str, Any] = {}
    for question in questions:
        identifier, kind = question["id"], question["type"]
        answer = answers[identifier]
        if not isinstance(answer, dict) or answer.get("type") != kind:
            raise invalid()
        normalized: dict[str, Any] = {"type": kind}
        if kind == "noul":
            if not finite_number(answer.get("noul"), maximum=1):
                raise invalid()
            normalized["noul"] = answer["noul"]
        else:
            criteria = question["criteria"]
            labels = set(criteria) if kind == "choice" else {str(i) for i in range(len(criteria))}
            if kind == "choice":
                if not isinstance(answer.get("choice"), str) or answer["choice"] not in labels:
                    raise invalid()
                normalized["choice"] = answer["choice"]
            else:
                if not finite_number(answer.get("score"), maximum=len(criteria) - 1):
                    raise invalid()
                normalized["score"] = answer["score"]
            if "confidence" in answer:
                if not finite_number(answer["confidence"], maximum=1):
                    raise invalid()
                normalized["confidence"] = answer["confidence"]
            if "probabilities" in answer:
                probabilities = answer["probabilities"]
                if (
                    not isinstance(probabilities, dict)
                    or set(probabilities) != labels
                    or not all(finite_number(p, maximum=1) for p in probabilities.values())
                    or not math.isclose(sum(probabilities.values()), 1, abs_tol=0.02)
                ):
                    raise invalid()
                normalized["probabilities"] = dict(probabilities)
        result[identifier] = normalized
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        raise invalid()
    normalized_usage: dict[str, Any] = {}
    for key in ("input_tokens", "output_tokens"):
        value = usage.get(key)
        if not isinstance(value, int) or not finite_number(value):
            raise invalid()
        normalized_usage[key] = value
    if "cost" in usage:
        if not finite_number(usage["cost"]):
            raise invalid()
        normalized_usage["cost"] = usage["cost"]
    if not isinstance(payload.get("model"), str) or not payload["model"]:
        raise invalid()
    return {"answers": result, "model": payload["model"], "usage": normalized_usage}
