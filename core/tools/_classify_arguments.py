"""Read ``classify`` calls, including other shapes, and refuse unclear ones.

Models write classification calls in several shapes: one text as ``state``/``text``
instead of ``items``, the Decisions wire's map of id to question, one question object
or bare text instead of a list, question fields at the top level, ``question``/
``prompt`` for the instructions, ``criteria`` for options or levels, a list of option
texts too long for labels (numbered from 1), ``noul``/``boolean`` for yes_no, and
``yes``/``no`` descriptions. This owner maps them onto the canonical call. It never
infers a question type or shifts score levels: when readings would differ, it
refuses before the Provider is called and shows the corrected question.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from core.model_tasks.decision_types import CHOICE_LABEL_LIMIT
from core.tools.call_syntax import (
    SpellingAliases,
    is_placeholder,
    normalize_call_arguments,
    spelling,
)
from core.tools.contracts import ToolContract, ToolContractError

REFUSAL_PREFIX = "classify was not run: "
QUESTION_FIELDS = ("id", "type", "instructions", "options", "levels", "criteria")
QUESTION_TYPES = ("yes_no", "choice", "score")
# The question field that carries each type's answer descriptions.
DESCRIPTION_FIELD = {"choice": "options", "score": "levels", "yes_no": "criteria"}

_LONG_TEXT = 120
_TOP_ALIASES = SpellingAliases(
    {
        "items": ("item", "texts", "inputs", "candidates", "entries", "documents", "files"),
        "context": ("task", "goal", "background", "shared_context", "query"),
        "questions": ("question", "checks", "judgments", "evaluations"),
        # One text to judge; it becomes the only item, or the context beside items.
        "state": ("input", "content", "data", "document", "subject", "text"),
    }
)
_QUESTION_ALIASES = SpellingAliases(
    {
        "id": ("name", "key", "question_id"),
        "type": ("kind", "question_type", "answer_type", "mode"),
        "instructions": ("question", "prompt", "instruction", "text", "ask", "description"),
        "options": ("choices", "labels", "categories"),
        "levels": ("scale", "rubric", "grades"),
        "criteria": ("criterion",),
    }
)
_TYPE_WORDS = SpellingAliases(
    {
        "yes_no": (
            "noul",
            "yes/no",
            "yesno",
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
            "option",
        ),
        "score": ("rating", "rate", "scale", "likert", "grade", "ordinal", "level"),
    }
)
_YES_NO_KEYS = {"true": "true", "yes": "true", "false": "false", "no": "false"}
_EXAMPLE_DESCRIPTIONS = {
    "choice": {"<label>": "<what it means>", "<other label>": "<what it means>"},
    "score": ["<lowest level>", "<next level>", "<highest level>"],
}
_TYPE_MEANINGS = (
    '"yes_no" for the probability of yes, "choice" to pick one of your options, or "score" '
    "to rate on your levels"
)


def normalize_classify_arguments(contract: ToolContract, arguments: Any) -> Any:
    """Return the canonical ``classify`` arguments for one Model call."""
    normalized = normalize_call_arguments(
        contract,
        arguments,
        field_aliases=_TOP_ALIASES,
        field_normalizers={
            "questions": _question_list,
            "items": _item_list,
            "state": _text_value,
            "context": _text_value,
        },
    )
    if not isinstance(normalized, dict):
        return normalized
    _place_state(normalized)
    loose = {
        key: normalized[key]
        for key in list(normalized)
        if key not in ("items", "context", "questions")
        and _QUESTION_ALIASES.get(key, key) in QUESTION_FIELDS
    }
    if loose:
        for key in loose:
            del normalized[key]
        normalized["questions"] = _with_loose_fields(contract, normalized.get("questions"), loose)
    if is_placeholder(normalized.get("context")):
        normalized.pop("context", None)
    items = normalized.get("items")
    if not items or not isinstance(items, list) or all(is_placeholder(item) for item in items):
        raise ToolContractError(
            f'{REFUSAL_PREFIX}it needs "items": the texts to classify, such as '
            '["<first text>", "<second text>"].'
        )
    questions = normalized.get("questions")
    if questions is None or questions == []:
        raise ToolContractError(
            f'{REFUSAL_PREFIX}it needs "questions", at least one, such as '
            '[{"type":"yes_no","instructions":"<the question>"}].'
        )
    if isinstance(questions, list):
        normalized["questions"] = _checked(questions)
    return normalized


def _place_state(normalized: dict[str, Any]) -> None:
    """One text to judge is the only item; beside items it is their shared context."""
    if "state" not in normalized:
        return
    state = normalized.pop("state")
    if is_placeholder(state):
        return
    if "items" not in normalized:
        normalized["items"] = [state]
    elif "context" not in normalized:
        normalized["context"] = state
    elif normalized["context"] != state:
        raise ToolContractError(
            f'{REFUSAL_PREFIX}the call has "items", "context" and a third text, so it is not '
            'clear what the third text is. Put shared text in "context" and the texts to '
            'classify in "items".'
        )


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


def _text_value(value: Any) -> Any:
    """A number, boolean or JSON value is judged as its JSON text."""
    if isinstance(value, bool | int | float | dict | list):
        return json.dumps(value, ensure_ascii=False)
    return value


def _item_list(value: Any) -> Any:
    """One text, a JSON array text or one object becomes the list of items."""
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except ValueError:
            decoded = None
        value = decoded if isinstance(decoded, list) else [value]
    elif isinstance(value, dict):
        value = [value]
    if not isinstance(value, list):
        return value
    return [_text_value(item) for item in value]


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
        if spelling(key) in ("yes", "no", "true", "false"):
            criteria = question.setdefault("criteria", {})
            if isinstance(criteria, dict):
                criteria[_YES_NO_KEYS[spelling(key)]] = entry
                continue
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
    _place_descriptions(question)
    return question


def _place_descriptions(question: dict[str, Any]) -> None:
    """Move the answer descriptions to the field of the question's type."""
    kind = question.get("type")
    field = DESCRIPTION_FIELD.get(kind) if isinstance(kind, str) else None
    if field is None:
        return
    given = {
        name: question.pop(name)
        for name in ("options", "levels", "criteria")
        if name in question and not _empty(question[name])
    }
    for name in ("options", "levels", "criteria"):
        question.pop(name, None)
    values = list(given.values())
    if any(value != values[0] for value in values[1:]):
        names = " and ".join(f'"{name}"' for name in given)
        raise ToolContractError(
            f"{REFUSAL_PREFIX}a question gives {names} with different values; a "
            f'{question["type"]} question takes only "{field}". Send the question with one '
            f'"{field}".'
        )
    if not values:
        return
    levels = _numbered_levels(values[0]) if question["type"] == "score" else None
    if levels and "0" not in {str(key) for key in values[0]}:
        # Levels numbered from another start: shifting them would change the answer.
        raise ToolContractError(
            f'{REFUSAL_PREFIX}question "{question.get("id")}" is a score, and levels are '
            f'numbered from 0, so level 0 would be "{levels[0]}". If that is meant, send: '
            f"{question_call(question, levels=levels)}"
        )
    question[field] = _descriptions(question["type"], values[0])


def _empty(value: Any) -> bool:
    return is_placeholder(value) or value in ([], {})


def _descriptions(kind: str, value: Any) -> Any:
    if kind == "choice" and isinstance(value, list):
        if all(isinstance(entry, str) for entry in value) and any(
            len(entry) > CHOICE_LABEL_LIMIT for entry in value
        ):
            # Texts too long for labels are the options themselves: number them.
            return {str(number): entry for number, entry in enumerate(value, 1)}
        labels: dict[str, Any] = {}
        for entry in value:
            if isinstance(entry, str):
                labels[entry] = entry  # A bare label describes itself.
            elif isinstance(entry, dict):
                fields = {spelling(key): item for key, item in entry.items()}
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
                    return value
                labels[label] = meaning
            else:
                return value
        return labels
    if kind == "score" and isinstance(value, dict):
        try:
            levels = {int(str(key)): item for key, item in value.items()}
        except ValueError:
            return value
        if sorted(levels) == list(range(len(levels))):
            return [levels[index] for index in range(len(levels))]
        return value
    if kind == "yes_no" and isinstance(value, dict):
        mapped = {_YES_NO_KEYS.get(spelling(str(key)), key): item for key, item in value.items()}
        if len(mapped) == len(value):
            return mapped
    return value


def _checked(questions: list[Any]) -> list[Any]:
    """Refuse questions the Decision Model cannot answer, naming the corrected question."""
    seen: set[str] = set()
    for question in questions:
        if not isinstance(question, dict):
            raise ToolContractError(
                f"{REFUSAL_PREFIX}each question is an object such as "
                '{"type":"yes_no","instructions":"<the question>"}.'
            )
        identifier = question.get("id")
        name = f'question "{identifier}"'
        kind = question.get("type")
        allowed = {"id", "type", "instructions"}
        if isinstance(kind, str) and kind in DESCRIPTION_FIELD:
            allowed.add(DESCRIPTION_FIELD[kind])
        extra = sorted(set(question) - allowed)
        if extra and kind in QUESTION_TYPES:
            fields = " and ".join(f'"{key}"' for key in extra)
            raise ToolContractError(
                f"{REFUSAL_PREFIX}{name} has {fields}, which classify cannot use: a "
                f"{kind} question takes only id, type, instructions"
                + (f" and {DESCRIPTION_FIELD[kind]}" if kind != "yes_no" else "")
                + ", and answers carry no explanations. Send it without "
                f"{'it' if len(extra) == 1 else 'them'}: "
                f"{question_call({k: v for k, v in question.items() if k in allowed})}"
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
        if kind is None:
            examples = " or ".join(
                question_call(
                    question,
                    type=option,
                    **(
                        {DESCRIPTION_FIELD[option]: _EXAMPLE_DESCRIPTIONS[option]}
                        if option in _EXAMPLE_DESCRIPTIONS
                        else {}
                    ),
                )
                for option in QUESTION_TYPES
            )
            raise ToolContractError(
                f'{REFUSAL_PREFIX}{name} needs "type": {_TYPE_MEANINGS}. Send one of: {examples}'
            )
        if kind not in QUESTION_TYPES:
            raise ToolContractError(
                f'{REFUSAL_PREFIX}{name} has type "{kind}"; use {_TYPE_MEANINGS}.'
            )
        _check_descriptions(question, name)
    return questions


def _check_descriptions(question: dict[str, Any], name: str) -> None:
    kind = question["type"]
    if kind == "choice" and not (
        isinstance(question.get("options"), dict) and len(question["options"]) >= 2
    ):
        raise ToolContractError(
            f'{REFUSAL_PREFIX}{name} is a choice, so "options" names at least two labels and '
            "what each means. Send: "
            f"{question_call(question, options=_EXAMPLE_DESCRIPTIONS['choice'])}"
        )
    if kind == "score" and not isinstance(question.get("levels"), list):
        raise ToolContractError(
            f'{REFUSAL_PREFIX}{name} is a score, so "levels" lists what each level means, '
            "lowest first; the answer is a level number from 0. Send: "
            f"{question_call(question, levels=_EXAMPLE_DESCRIPTIONS['score'])}"
        )
    criteria = question.get("criteria")
    if (
        kind == "yes_no"
        and criteria is not None
        and (not isinstance(criteria, dict) or set(criteria) != {"true", "false"})
    ):
        raise ToolContractError(
            f"{REFUSAL_PREFIX}{name} is a yes_no question; describe what yes and no mean in "
            f"its instructions instead. Send: {question_call(question, criteria=None)}"
        )


def _numbered_levels(levels: Any) -> list[Any] | None:
    """Levels numbered from another start than 0, in order; None when not numbered."""
    if not isinstance(levels, dict) or not levels:
        return None
    try:
        numbered = {int(str(key)): value for key, value in levels.items()}
    except ValueError:
        return None
    first = min(numbered)
    if sorted(numbered) != list(range(first, first + len(numbered))):
        return None
    return [numbered[number] for number in sorted(numbered)]


def wire_question(question: Mapping[str, Any]) -> dict[str, Any]:
    """The Decisions wire form of one checked question."""
    wire = {
        "id": question["id"],
        "type": "noul" if question["type"] == "yes_no" else question["type"],
        "instructions": question["instructions"],
    }
    descriptions = question.get(DESCRIPTION_FIELD[question["type"]])
    if descriptions is not None:
        wire["criteria"] = descriptions
    return wire


__all__ = [
    "QUESTION_TYPES",
    "REFUSAL_PREFIX",
    "normalize_classify_arguments",
    "question_call",
    "wire_question",
]
