import copy

import pytest

from core.model_tasks.decision_types import (
    DecisionError,
    validate_answers,
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
        lambda q: q[1].update(criteria=["Only"]),
        lambda q: q[1].update(criteria=[str(n) for n in range(11)]),
        lambda q: q[0].update(criteria={str(n): "Option" for n in range(256)}),
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
