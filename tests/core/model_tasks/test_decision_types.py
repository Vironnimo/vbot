import copy

import pytest

from core.model_tasks.decision_types import (
    DecisionError,
    validate_answers,
    validate_draft,
    validate_input,
)

QUESTIONS = [
    {
        "id": "category",
        "type": "choice",
        "instructions": "Classify the message",
        "criteria": {"bug": "Broken behavior", "other": "Other"},
    },
    {
        "id": "severity",
        "type": "score",
        "instructions": "Rate severity",
        "criteria": ["Minor", "Major"],
    },
    {"id": "clear", "type": "noul", "instructions": "Is the message clear?"},
]
RESULT = {
    "model": "typesafe/jev-version",
    "usage": {"input_tokens": 12, "output_tokens": 3},
    "answers": {
        "category": {
            "type": "choice",
            "choice": "bug",
            "probabilities": {"bug": 0.8, "other": 0.2},
            "confidence": 0.6,
        },
        "severity": {"type": "score", "score": 0.7},
        "clear": {"type": "noul", "noul": 0.9},
    },
}


def test_round_trip_preserves_application_data_and_optional_absence():
    state = {"type": "TRUE", "score": "3.0", "image": "just a field", "nested": [{"false": False}]}
    validated, questions = validate_input(state, QUESTIONS)
    assert validated == state and validated is not state
    assert questions == QUESTIONS
    assert validate_answers(RESULT, questions) == RESULT
    assert "confidence" not in validate_answers(RESULT, questions)["answers"]["severity"]


@pytest.mark.parametrize(
    "mutation",
    [
        lambda q: q.append(copy.deepcopy(q[0])),
        lambda q: q[0].update(criteria={"one": "Only one"}),
        lambda q: q[1].update(criteria=[]),
        lambda q: q[2].update(criteria={"yes": "Yes", "no": "No"}),
        lambda q: q[0].update(instructions=""),
        lambda q: q[0].update(unknown="Do something else"),
    ],
)
def test_rejects_invalid_complete_questions(mutation):
    questions = copy.deepcopy(QUESTIONS)
    mutation(questions)
    with pytest.raises(DecisionError):
        validate_input("state", questions)


@pytest.mark.parametrize(
    "state",
    [None, True, 1, {"x": float("nan")}, "x" * 1_000_001],
    ids=["null", "boolean", "integer", "nan", "oversize"],
)
def test_rejects_invalid_state(state):
    with pytest.raises(DecisionError):
        validate_input(state, QUESTIONS)


@pytest.mark.parametrize(
    "answer",
    [
        {"type": "choice", "choice": "missing"},
        {"type": "choice", "choice": "bug", "confidence": True},
        {"type": "choice", "choice": "bug", "probabilities": {"bug": 0.8}},
        {"type": "choice", "choice": "bug", "probabilities": {"bug": 0.8, "other": 0.8}},
    ],
)
def test_rejects_incomplete_or_unusable_provider_answers(answer):
    payload = copy.deepcopy(RESULT)
    payload["answers"]["category"] = answer
    with pytest.raises(DecisionError) as error:
        validate_answers(payload, QUESTIONS)
    assert error.value.code == "invalid_result"


def test_drafts_allow_incomplete_fields_but_not_unrenderable_shapes():
    draft = {
        "title": "Draft",
        "state": "",
        "questions": [{"id": "", "type": "choice", "instructions": "", "criteria": {"": ""}}],
    }
    assert validate_draft(draft) == draft
    draft["questions"][0]["criteria"] = None
    with pytest.raises(DecisionError):
        validate_draft(draft)


def test_control_drafts_allow_incomplete_commands_but_reject_unrenderable_actions():
    draft = {
        "title": "Control",
        "state": "",
        "questions": [],
        "control": {
            "instructions": "",
            "observe": {"argv": [""], "cwd": ""},
            "actions": {"": {"description": "", "command": None}},
            "interval_ms": "",
            "max_steps": 0,
            "timeout_seconds": 10,
        },
    }
    assert validate_draft(draft) == draft
    draft["control"]["actions"][""]["command"] = {"argv": "python", "cwd": ""}
    with pytest.raises(DecisionError):
        validate_draft(draft)
