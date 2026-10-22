import asyncio
from types import SimpleNamespace
from typing import Any, cast

import pytest
import respx

from core.model_tasks.constants import TASK_DECISION
from core.model_tasks.decision_types import DecisionError
from core.model_tasks.decisions import ITEM_LIMIT, NOT_CONFIGURED_MESSAGE, DecisionService
from core.model_tasks.model_tasks import TaskModelService
from core.providers.providers import AuthConfig, ConnectionConfig, ProviderConfig
from core.providers.token_getter import StaticTokenGetter
from tests.core.model_tasks.model_tasks_test_support import (
    _Credentials,
    _model,
    _Models,
    _Providers,
    _Storage,
)

QUESTIONS = [{"id": "q", "type": "noul", "instructions": "Relevant?"}]
DECISIONS_URL = "https://openrouter.ai/api/alpha/decisions"


def service(
    *, provider: str = "openrouter", usable: bool = True, window: int | None = 262_144
) -> DecisionService:
    """A Decision binding whose Model reports *window* for its Connection ``api_key``.

    Its runtime reaches the real OpenRouter wire, which tests mock with respx.
    """
    model = SimpleNamespace(context_window_for={"api_key": window}.get)
    bindings = SimpleNamespace(
        binding_is_usable=lambda task: usable,
        binding_for=lambda task: SimpleNamespace(target=f"{provider}/typesafe/jev-1.13::api_key"),
        model_for_target=lambda ref: model,
    )
    connection = ConnectionConfig(
        id="api_key",
        type="api_key",
        label="API Key",
        auth=AuthConfig(header="Authorization", prefix="Bearer ", credential_key="TEST_KEY"),
    )
    openrouter = ProviderConfig(
        id="openrouter",
        name="OpenRouter",
        adapter="openrouter",
        base_url="https://openrouter.ai/api/v1",
        connections=[connection],
    )
    runtime = SimpleNamespace(
        providers=SimpleNamespace(get=lambda _provider: openrouter),
        get_connection_token_getter=lambda _connection: StaticTokenGetter("test-token"),
        provider_debug_recorder=lambda **_: None,
    )
    return DecisionService(cast(TaskModelService, bindings), cast(Any, runtime))


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
    structured = {
        "type": "TRUE",
        "score": "3.0",
        "image": "just a field",
        "nested": [{"false": False}],
    }
    items = [structured, "broken", *[f"item {n}" for n in range(20)]]

    outcomes = await owner.classify(items, QUESTIONS, context="the task")

    assert sorted(states, key=lambda state: items.index(state["item"])) == [
        {"context": "the task", "item": item} for item in items
    ]
    copied = next(state["item"] for state in states if isinstance(state["item"], dict))
    assert copied is not structured and copied["nested"] is not structured["nested"]
    assert outcomes[0] == {"answers": {"q": {"type": "noul", "noul": 0.4}}}
    assert isinstance(outcomes[1], DecisionError) and outcomes[1].code == "provider_error"
    assert [outcome["answers"]["q"]["noul"] for outcome in outcomes[2:]] == [0.6] * 10 + [0.7] * 10
    assert 1 < peak <= 8


class _Calibration:
    def __init__(self, factor: float) -> None:
        self.factor = factor
        self.samples: list[tuple[str, int, int]] = []

    def input_estimate_factor(self, model: str) -> float:
        return self.factor

    def record_input_estimate(self, model: str, *, measured: int, estimated: int) -> None:
        self.samples.append((model, measured, estimated))


@pytest.mark.asyncio
@pytest.mark.parametrize("window", [1_000, None], ids=["reported-window", "no-reported-window"])
@pytest.mark.parametrize(
    ("item", "code", "needs_window"),
    [
        pytest.param("word " * 2_000, "too_large", True, id="context-window"),
        pytest.param({"x": float("nan")}, "invalid_request", False, id="non-finite-json"),
        pytest.param("x" * 1_000_001, "too_large", False, id="input-byte-limit"),
    ],
)
async def test_an_invalid_item_fails_alone_unsent(monkeypatch, window, item, code, needs_window):
    owner = service(window=window)
    sent: list[Any] = []

    async def evaluate(target, state, questions, *, http_client=None, usage_context=None):
        sent.append(state)
        return {"answers": {"q": {"type": "noul", "noul": 0.5}}}

    monkeypatch.setattr(owner, "_evaluate", evaluate)
    small, beyond = await owner.classify(["small", item], QUESTIONS)

    assert small["answers"]["q"]["noul"] == 0.5
    if needs_window and window is None:
        # Without a reported window the Provider decides what is too large.
        # Worker preparation may complete in either order; both requests must be sent.
        assert sorted(sent) == sorted(["small", item])
        assert beyond["answers"]["q"]["noul"] == 0.5
    else:
        assert sent == ["small"]
        assert isinstance(beyond, DecisionError) and beyond.code == code
        if needs_window:
            assert "at most 1000 tokens" in str(beyond)


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize(
    ("status", "error", "code"),
    [
        (413, {"code": 413, "message": "Payload Too Large"}, "too_large"),
        (
            400,
            {
                "code": 400,
                "message": "Provider returned error",
                "metadata": {"raw": '{"error":{"type":"max_tokens_exceeded"}}'},
            },
            "too_large",
        ),
        (
            400,
            {"code": 400, "message": "This endpoint's maximum context length is 262144 tokens."},
            "too_large",
        ),
        (400, {"code": 400, "message": "Invalid request parameters"}, "provider_error"),
    ],
    ids=["body-too-large", "upstream-max-tokens", "router-context-length", "other-bad-request"],
)
async def test_a_size_rejection_by_the_provider_is_too_large(status, error, code):
    route = respx.post(DECISIONS_URL).respond(status, json={"error": error})

    [outcome] = await service().classify(["x"], QUESTIONS)

    assert isinstance(outcome, DecisionError) and outcome.code == code
    assert ("at most 262144 tokens" in str(outcome)) is (code == "too_large")
    assert route.call_count == 1


@pytest.mark.asyncio
async def test_reported_input_tokens_correct_the_size_check(monkeypatch):
    calibration = _Calibration(factor=2.0)
    owner = service(window=1_000)
    owner._usage_recorder = cast(Any, calibration)

    async def evaluate(target, state, questions, *, http_client=None, usage_context=None):
        return {"answers": {}, "usage": {"input_tokens": 1234}}

    monkeypatch.setattr(owner, "_evaluate", evaluate)
    # About 0.8 of the window: fits as estimated, not as corrected.
    halfway = "word " * 600

    rejected, answered = await owner.classify([halfway, "small"], QUESTIONS)

    assert isinstance(rejected, DecisionError) and rejected.code == "too_large"
    assert answered["usage"]["input_tokens"] == 1234
    [(model, measured, estimated)] = calibration.samples
    assert model == "openrouter/typesafe/jev-1.13::api_key" and measured == 1234
    assert 0 < estimated < 100


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("owner", "items", "questions", "code"),
    [
        (service(), ["x"] * (ITEM_LIMIT + 1), QUESTIONS, "invalid_request"),
        (service(), [], QUESTIONS, "invalid_request"),
        (service(), [None], QUESTIONS, "invalid_request"),
        (service(), [True], QUESTIONS, "invalid_request"),
        (service(), [1], QUESTIONS, "invalid_request"),
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
        "null-item",
        "boolean-item",
        "integer-item",
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


@pytest.mark.asyncio
async def test_a_stored_binding_to_another_decision_model_reads_as_not_configured(monkeypatch):
    stored = {TASK_DECISION: {"target": "openrouter/cloudflare/clef::api-key", "options": {}}}
    storage = _Storage(stored)
    model_tasks = TaskModelService(
        _Providers(),
        _Models([_model("cloudflare/clef", (TASK_DECISION,), name="Cloudflare: Clef")]),
        _Credentials(),
        storage,
    )
    owner = DecisionService(model_tasks, cast(Any, None))

    async def evaluate(*args, **kwargs):
        raise AssertionError("no request may be sent")

    monkeypatch.setattr(owner, "_evaluate", evaluate)

    with pytest.raises(DecisionError) as error:
        await owner.classify(["x"], QUESTIONS)

    assert not owner.available()
    assert (error.value.code, str(error.value)) == ("not_configured", NOT_CONFIGURED_MESSAGE)
    assert storage.load_model_task_settings() == stored
