import asyncio
from types import SimpleNamespace
from typing import Any, cast

import pytest

from core.model_tasks.decision_types import DecisionError
from core.model_tasks.decisions import INPUT_TOKEN_LIMIT, ITEM_LIMIT, DecisionService
from core.model_tasks.model_tasks import TaskModelService

QUESTIONS = [{"id": "q", "type": "noul", "instructions": "Relevant?"}]


def service(*, provider: str = "openrouter", usable: bool = True) -> DecisionService:
    bindings = SimpleNamespace(
        binding_is_usable=lambda task: usable,
        binding_for=lambda task: SimpleNamespace(target=f"{provider}/typesafe/jev-1.13::api_key"),
    )
    return DecisionService(cast(TaskModelService, bindings), cast(Any, None))


@pytest.mark.asyncio
async def test_each_item_is_one_bounded_request_answered_in_item_order(monkeypatch):
    owner = service()
    states: list[Any] = []
    running = peak = 0

    async def evaluate(target, state, questions, *, http_client=None, usage_context=None):
        nonlocal running, peak
        states.append(state)
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0)
        running -= 1
        if state["item"] == "broken":
            raise DecisionError("Provider refused.", code="provider_error")
        return {"answers": {"q": {"type": "noul", "noul": len(state["item"]) / 10}}}

    monkeypatch.setattr(owner, "_evaluate", evaluate)
    items = ["a", "broken", *[f"item {n}" for n in range(20)]]

    outcomes = await owner.classify(items, QUESTIONS, context="the task")

    assert sorted(states, key=lambda state: items.index(state["item"])) == [
        {"context": "the task", "item": item} for item in items
    ]
    assert outcomes[0] == {"answers": {"q": {"type": "noul", "noul": 0.1}}}
    assert isinstance(outcomes[1], DecisionError) and outcomes[1].code == "provider_error"
    assert [outcome["answers"]["q"]["noul"] for outcome in outcomes[2:]] == [0.6] * 10 + [0.7] * 10
    assert 1 < peak <= 8


@pytest.mark.asyncio
async def test_an_oversized_item_fails_alone_without_a_request(monkeypatch):
    owner = service()
    sent: list[Any] = []

    async def evaluate(target, state, questions, *, http_client=None, usage_context=None):
        sent.append(state)
        return {"answers": {"q": {"type": "noul", "noul": 0.5}}}

    monkeypatch.setattr(owner, "_evaluate", evaluate)

    small, large = await owner.classify(["small", "word " * INPUT_TOKEN_LIMIT], QUESTIONS)

    assert sent == ["small"] and small["answers"]["q"]["noul"] == 0.5
    assert isinstance(large, DecisionError) and large.code == "too_large"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("owner", "items", "questions", "code"),
    [
        (service(), ["x"] * (ITEM_LIMIT + 1), QUESTIONS, "invalid_request"),
        (service(), [], QUESTIONS, "invalid_request"),
        (service(), ["x"], [{"id": "q", "type": "noul"}], "invalid_request"),
        (
            service(),
            ["x"],
            [{"id": "q", "type": "score", "instructions": "How?", "criteria": ["only"]}],
            "invalid_request",
        ),
        (service(usable=False), ["x"], QUESTIONS, "not_configured"),
        (service(provider="openai"), ["x"], QUESTIONS, "not_configured"),
    ],
    ids=[
        "too-many-items",
        "no-items",
        "no-instructions",
        "one-level",
        "unusable",
        "not-openrouter",
    ],
)
async def test_invalid_calls_and_missing_bindings_send_nothing(
    monkeypatch, owner, items, questions, code
):
    async def evaluate(*args, **kwargs):
        raise AssertionError("no request may be sent")

    monkeypatch.setattr(owner, "_evaluate", evaluate)

    with pytest.raises(DecisionError) as error:
        await owner.classify(items, questions)

    assert error.value.code == code
