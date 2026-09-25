"""Read ``evaluate`` calls, including other question shapes, and refuse unclear ones.

Models write judgment questions in several shapes: the Decisions wire's map of id to
question, one question object or bare text instead of a list, question fields at the
top level, ``question``/``prompt`` for the instructions, ``options``/``labels`` for
choice criteria, ``yes_no``/``boolean`` for noul, and ``yes``/``no`` noul criteria.
This owner maps them onto the canonical call. It never infers a question type or
shifts score levels: when readings would differ, it refuses before the Provider is
called and shows the corrected question.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from core.tools._argument_repair import normalize_call_arguments
from core.tools._call_vocabulary import SpellingAliases, is_placeholder, spelling
from core.tools.contracts import ToolContract, ToolContractError

REFUSAL_PREFIX = "evaluate was not run: "
QUESTION_FIELDS = ("id", "type", "instructions", "criteria")
QUESTION_TYPES = ("choice", "score", "noul")

_LONG_TEXT = 120
_TOP_ALIASES = SpellingAliases(
    {
        "state": ("input", "content", "data", "context", "document", "subject", "text"),
        "questions": ("question", "checks", "judgments", "evaluations"),
    }
)
_QUESTION_ALIASES = SpellingAliases(
    {
        "id": ("name", "key", "question_id"),
        "type": ("kind", "question_type", "answer_type", "mode"),
        "instructions": ("question", "prompt", "instruction", "text", "ask", "description"),
        "criteria": ("options", "choices", "labels", "categories", "levels", "scale", "rubric"),
    }
)
_TYPE_WORDS = SpellingAliases(
    {
        "noul": (
            "yes_no",
            "yes/no",
            "boolean",
            "bool",
            "binary",
            "probability",
            "true_false",
            "likelihood",
        ),
        "choice": (
            "classify",
            "classification",
            "category",
            "categorical",
            "select",
            "multiple_choice",
            "enum",
            "label",
        ),
        "score": ("rating", "rate", "scale", "likert", "grade", "ordinal", "level"),
    }
)
_YES_NO_KEYS = {"true": "true", "yes": "true", "false": "false", "no": "false"}
_EXAMPLE_CRITERIA = {
    "choice": {"<label>": "<what it means>", "<other label>": "<what it means>"},
    "score": ["<lowest level>", "<next level>", "<highest level>"],
}
_TYPE_MEANINGS = (
    '"noul" for the probability of yes, "choice" to pick one of your labels, or "score" '
    "to rate on your levels"
)


def normalize_evaluate_arguments(contract: ToolContract, arguments: Any) -> Any:
    """Return the canonical ``evaluate`` arguments for one Model call."""
    normalized = normalize_call_arguments(
        contract,
        arguments,
        field_aliases=_TOP_ALIASES,
        field_normalizers={"questions": _question_list, "state": _state_value},
    )
    if not isinstance(normalized, dict):
        return normalized
    loose = {
        key: normalized[key]
        for key in list(normalized)
        if key not in ("state", "questions") and _QUESTION_ALIASES.get(key, key) in QUESTION_FIELDS
    }
    if loose:
        for key in loose:
            del normalized[key]
        normalized["questions"] = _with_loose_fields(contract, normalized.get("questions"), loose)
    if "state" not in normalized or is_placeholder(normalized["state"]):
        raise ToolContractError(
            f'{REFUSAL_PREFIX}it needs "state": the text or JSON the questions are about.'
        )
    questions = normalized.get("questions")
    if questions is None or questions == []:
        raise ToolContractError(
            f'{REFUSAL_PREFIX}it needs "questions", at least one, such as '
            '[{"id":"q1","type":"noul","instructions":"<the question>"}].'
        )
    if isinstance(questions, list):
        normalized["questions"] = _checked(questions)
    return normalized


def _with_loose_fields(contract: ToolContract, questions: Any, loose: dict[str, Any]) -> list[Any]:
    """Question fields written straight into the call belong to its only question."""
    fields = {_QUESTION_ALIASES.get(key, key): value for key, value in loose.items()}
    names = " and ".join(f'"{name}"' for name in fields)
    if questions is None:
        single: dict[str, Any] = {}
    elif isinstance(questions, list) and len(questions) == 1 and isinstance(questions[0], dict):
        single = questions[0]
        differing = {name for name in fields if name in single and single[name] != fields[name]}
        if differing:
            inside = question_call(single)
            outside = question_call(single, **{name: fields[name] for name in differing})
            raise ToolContractError(
                f"{REFUSAL_PREFIX}the call gives {names} both inside and outside its question, "
                f"with different values. Send the question you mean: {inside} or {outside}"
            )
    else:
        count = len(questions) if isinstance(questions, list) else 0
        raise ToolContractError(
            f"{REFUSAL_PREFIX}{names} {'is' if len(fields) == 1 else 'are'} outside "
            f'"questions", and the call has {count} questions, so it is not clear which one '
            f"{'it belongs' if len(fields) == 1 else 'they belong'} to. Put "
            f"{'it' if len(fields) == 1 else 'them'} inside each question "
            f"{'it applies' if len(fields) == 1 else 'they apply'} to."
        )
    merged = contract.normalize_arguments({"questions": _question_list([{**single, **loose}])})
    return list(merged["questions"])


def _state_value(value: Any) -> Any:
    """A number or boolean is judged as its text."""
    return json.dumps(value) if isinstance(value, bool | int | float) else value


def question_call(question: Mapping[str, Any], **overrides: Any) -> str:
    """Render one question compactly, in field order, with long text as a stand-in."""
    merged = {**question, **overrides}
    ordered = {name: merged[name] for name in QUESTION_FIELDS if merged.get(name) is not None}
    instructions = ordered.get("instructions")
    if isinstance(instructions, str) and len(instructions) > _LONG_TEXT:
        ordered["instructions"] = "<the instructions from this call>"
    return json.dumps(ordered, ensure_ascii=False, separators=(",", ":"))


def _question_list(value: Any) -> Any:
    """A list, one question, bare text, or the wire's id-to-question map becomes a list."""
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except ValueError:
            decoded = None
        value = decoded if isinstance(decoded, list | dict) else {"instructions": value}
    if isinstance(value, dict):
        if (
            value
            and all(isinstance(item, dict) for item in value.values())
            and not any(_QUESTION_ALIASES.get(key, key) in QUESTION_FIELDS for key in value)
        ):
            value = [{"id": key, **item} for key, item in value.items()]
        else:
            value = [value]
    if not isinstance(value, list):
        return value
    questions = [_question(item) for item in value]
    taken = {item.get("id") for item in questions if isinstance(item, dict)}
    number = 0
    for item in questions:
        if isinstance(item, dict) and is_placeholder(item.get("id")):
            number += 1
            while f"q{number}" in taken:
                number += 1
            item["id"] = f"q{number}"
    return questions


def _question(item: Any) -> Any:
    if isinstance(item, str):
        return {"instructions": item}
    if not isinstance(item, dict):
        return item
    question: dict[str, Any] = {}
    for key, entry in item.items():
        name = _QUESTION_ALIASES.get(key, key)
        if name in question and question[name] != entry:
            raise ToolContractError(
                f'{REFUSAL_PREFIX}a question gives "{name}" twice with different values. Send '
                f"the question you mean: {question_call(question)} or "
                f"{question_call(question, **{name: entry})}"
            )
        question[name] = entry
    kind = question.get("type")
    if isinstance(kind, str):
        question["type"] = _TYPE_WORDS.get(
            kind, spelling(kind) if spelling(kind) in QUESTION_TYPES else kind
        )
    if isinstance(question.get("id"), int | float) and not isinstance(question.get("id"), bool):
        question["id"] = str(question["id"])
    question["criteria"] = _criteria(question.get("type"), question.get("criteria"))
    if question["criteria"] is None:
        del question["criteria"]
    return question


def _criteria(kind: Any, criteria: Any) -> Any:
    if is_placeholder(criteria) or criteria in ([], {}):
        return None
    if kind == "choice" and isinstance(criteria, list):
        labels: dict[str, Any] = {}
        for entry in criteria:
            if isinstance(entry, str):
                labels[entry] = entry  # A bare label describes itself.
            elif isinstance(entry, dict):
                fields = {spelling(key): value for key, value in entry.items()}
                label = next(
                    (fields[key] for key in ("label", "name", "id", "value") if key in fields),
                    None,
                )
                meaning = next(
                    (
                        fields[key]
                        for key in ("description", "meaning", "definition", "when")
                        if key in fields
                    ),
                    label,
                )
                if not isinstance(label, str):
                    return criteria
                labels[label] = meaning
            else:
                return criteria
        return labels
    if kind == "score" and isinstance(criteria, dict):
        try:
            levels = {int(str(key)): value for key, value in criteria.items()}
        except ValueError:
            return criteria
        if sorted(levels) == list(range(len(levels))):
            return [levels[index] for index in range(len(levels))]
        return criteria
    if kind == "noul" and isinstance(criteria, dict):
        mapped = {
            _YES_NO_KEYS.get(spelling(str(key)), key): value for key, value in criteria.items()
        }
        if len(mapped) == len(criteria):
            return mapped
    return criteria


def _checked(questions: list[Any]) -> list[Any]:
    """Refuse questions the decision model cannot answer, naming the corrected question."""
    seen: set[str] = set()
    for question in questions:
        if not isinstance(question, dict):
            raise ToolContractError(
                f"{REFUSAL_PREFIX}each question is an object such as "
                '{"id":"q1","type":"noul","instructions":"<the question>"}.'
            )
        identifier = question.get("id")
        name = f'question "{identifier}"'
        extra = sorted(set(question) - set(QUESTION_FIELDS))
        if extra:
            fields = " and ".join(f'"{key}"' for key in extra)
            raise ToolContractError(
                f"{REFUSAL_PREFIX}{name} has {fields}, which evaluate cannot use: a question "
                "takes only id, type, instructions and criteria, and answers carry no "
                f"explanations. Send it without {'it' if len(extra) == 1 else 'them'}: "
                f"{question_call(question)}"
            )
        if identifier in seen:
            raise ToolContractError(
                f'{REFUSAL_PREFIX}two questions have the id "{identifier}"; give each its own id.'
            )
        seen.add(str(identifier))
        instructions = question.get("instructions")
        if not isinstance(instructions, str) or not instructions.strip():
            raise ToolContractError(
                f'{REFUSAL_PREFIX}{name} needs "instructions": the question itself. Send: '
                f"{question_call(question, instructions='<the question>')}"
            )
        kind = question.get("type")
        if kind is None:
            examples = " or ".join(
                question_call(question, type=option, criteria=_EXAMPLE_CRITERIA.get(option))
                for option in ("noul", "choice", "score")
            )
            raise ToolContractError(
                f'{REFUSAL_PREFIX}{name} needs "type": {_TYPE_MEANINGS}. Send one of: {examples}'
            )
        if kind not in QUESTION_TYPES:
            raise ToolContractError(
                f'{REFUSAL_PREFIX}{name} has type "{kind}"; use {_TYPE_MEANINGS}.'
            )
        _check_criteria(question, name)
    return questions


def _check_criteria(question: dict[str, Any], name: str) -> None:
    kind, criteria = question["type"], question.get("criteria")
    if kind == "choice" and not (isinstance(criteria, dict) and len(criteria) >= 2):
        raise ToolContractError(
            f'{REFUSAL_PREFIX}{name} is a choice, so "criteria" names at least two labels and '
            "what each means. Send: "
            f"{question_call(question, criteria=_EXAMPLE_CRITERIA['choice'])}"
        )
    if kind == "score" and not isinstance(criteria, list):
        levels = _numbered_levels(criteria)
        if levels:
            raise ToolContractError(
                f"{REFUSAL_PREFIX}{name} is a score, and score levels are numbered from 0, so "
                f'level 0 would be "{levels[0]}". If that is meant, send: '
                f"{question_call(question, criteria=levels)}"
            )
        raise ToolContractError(
            f'{REFUSAL_PREFIX}{name} is a score, so "criteria" lists what each level means, '
            "lowest first; the answer is a level number from 0. Send: "
            f"{question_call(question, criteria=_EXAMPLE_CRITERIA['score'])}"
        )
    if (
        kind == "noul"
        and criteria is not None
        and (not isinstance(criteria, dict) or set(criteria) != {"true", "false"})
    ):
        raise ToolContractError(
            f'{REFUSAL_PREFIX}{name} is a noul question; its optional "criteria" say what yes '
            "and no mean. Send: "
            + question_call(
                question, criteria={"true": "<when yes applies>", "false": "<when no applies>"}
            )
        )


def _numbered_levels(criteria: Any) -> list[Any] | None:
    """Levels numbered from another start than 0, in order; None when not numbered."""
    if not isinstance(criteria, dict) or not criteria:
        return None
    try:
        numbered = {int(str(key)): value for key, value in criteria.items()}
    except ValueError:
        return None
    first = min(numbered)
    if sorted(numbered) != list(range(first, first + len(numbered))):
        return None
    return [numbered[number] for number in sorted(numbered)]


__all__ = [
    "QUESTION_FIELDS",
    "QUESTION_TYPES",
    "REFUSAL_PREFIX",
    "normalize_evaluate_arguments",
    "question_call",
]
