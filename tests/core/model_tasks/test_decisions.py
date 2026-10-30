import asyncio
import json
from types import SimpleNamespace
from typing import Any, cast

import pytest
import respx

from core.model_tasks.constants import TASK_DECISION
from core.model_tasks.decision_profile import DecisionProfile
from core.model_tasks.decision_types import DecisionError
from core.model_tasks.decisions import ITEM_LIMIT, DecisionService
from core.model_tasks.model_tasks import TaskModelService, TaskModelValidationError
from core.models import ModelRegistry
from core.providers.providers import AuthConfig, ConnectionConfig, ProviderConfig
from core.providers.token_getter import StaticTokenGetter
from core.providers.wire_profile import DecisionWire
from tests.core.model_tasks.model_tasks_test_support import (
    _Credentials,
    _model,
    _Models,
    _Providers,
    _registry_model,
    _Storage,
    task_http_transport,
)

QUESTIONS = [{"id": "q", "type": "noul", "instructions": "Relevant?"}]
DECISIONS_URL = "https://openrouter.ai/api/alpha/decisions"
SYSTEM_ONE_WIRE = DecisionWire(
    protocol="system_one",
    endpoint="/api/alpha/decisions",
    allowed_options=("provider_options",),
)


def service(
    *,
    provider: str = "openrouter",
    usable: bool = True,
    window: int | None = 262_144,
    profile: DecisionProfile | None = None,
    options: dict[str, Any] | None = None,
    model_id: str = "typesafe/jev-1.13",
    base_url: str = "https://openrouter.ai/api/v1",
) -> DecisionService:
    """A configured Decision binding reaching the real mocked Provider wire."""
    selected_profile = profile or DecisionProfile(SYSTEM_ONE_WIRE, max_input_tokens=window)
    selected_options = options if options is not None else {}

    def validate_execution_target(binding):
        if not usable:
            raise TaskModelValidationError("Unavailable target")

    bindings = SimpleNamespace(
        binding_is_usable=lambda task: usable,
        binding_for=lambda task: SimpleNamespace(target=f"{provider}/{model_id}::api_key"),
        validate_execution_target=validate_execution_target,
        options_with_defaults=lambda binding: selected_options,
        decision_profile=lambda ref: selected_profile,
    )
    connection = ConnectionConfig(
        id="api_key",
        type="api_key",
        label="API Key",
        auth=AuthConfig(header="Authorization", prefix="Bearer ", credential_key="TEST_KEY"),
    )
    configured_provider = ProviderConfig(
        id=provider,
        name=provider,
        adapter="openai_compatible",
        base_url=base_url,
        connections=[connection],
    )
    runtime = SimpleNamespace(
        providers=SimpleNamespace(get=lambda _provider: configured_provider),
        prepare_provider_http=lambda connection: (
            StaticTokenGetter("test-token"),
            task_http_transport(connection),
        ),
        provider_debug_recorder=lambda **_: None,
    )
    return DecisionService(cast(TaskModelService, bindings), cast(Any, runtime))


@pytest.mark.asyncio
async def test_each_item_is_one_bounded_request_answered_in_item_order(monkeypatch):
    owner = service()
    states: list[Any] = []
    running = peak = 0
    first_batch_started = asyncio.Event()

    async def evaluate(target, state, questions, *, http_client=None, usage_context=None):
        nonlocal running, peak
        states.append(state)
        running += 1
        peak = max(peak, running)
        # Preparation runs on workers: one loop turn need not start another item.
        # Keep the first batch in flight until all eight request slots are used.
        if running == 8:
            first_batch_started.set()
        await first_batch_started.wait()
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
    assert peak == 8


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
@pytest.mark.parametrize("limit_name", ["max_request_bytes", "max_text_bytes"])
async def test_model_byte_limits_reject_only_the_item_that_exceeds_them(monkeypatch, limit_name):
    options = {"provider_options": {"only": ["fixture-provider"]}}
    expected_body = {
        "model": "typesafe/jev-1.13",
        "state": "ü",
        "questions": {"q": {"type": "noul", "instructions": "Relevant?"}},
        "provider": {"only": ["fixture-provider"]},
    }
    exact_bytes = len(
        json.dumps(expected_body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )
    limit = exact_bytes if limit_name == "max_request_bytes" else 512
    oversized = "üü" if limit_name == "max_request_bytes" else "x" * 1000
    owner = service(
        profile=DecisionProfile(
            SYSTEM_ONE_WIRE,
            max_request_bytes=limit if limit_name == "max_request_bytes" else None,
            max_text_bytes=limit if limit_name == "max_text_bytes" else None,
        ),
        options=options,
    )
    sent = []

    async def evaluate(target, state, questions, *, usage_context=None):
        sent.append(state)
        return {"answers": {"q": {"type": "noul", "noul": 0.8}}}

    monkeypatch.setattr(owner, "_evaluate", evaluate)

    accepted, rejected = await owner.classify(["ü", oversized], QUESTIONS)

    assert accepted["answers"]["q"]["noul"] == 0.8
    assert isinstance(rejected, DecisionError) and rejected.code == "too_large"
    assert sent == ["ü"]


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize(
    ("provider", "model_id", "base_url", "wire", "response"),
    [
        (
            "openai",
            "gpt-6-luna",
            "https://api.openai.com/v1",
            DecisionWire(
                protocol="openai_decisions",
                endpoint="/v1/decisions",
                allowed_options=("prompt_cache_key",),
            ),
            {
                "answers": [{"name": "q", "type": "predicate", "probability": 0.8}],
                "model": "gpt-6-luna",
                "usage": {"input_tokens": 12, "output_tokens": 1},
            },
        ),
        (
            "ollama-cloud",
            "clef:27b",
            "https://ollama.com",
            DecisionWire(protocol="system_one", endpoint="/v1/systemone"),
            {
                "answers": {"q": {"type": "noul", "noul": 0.8}},
                "model": "clef:27b",
                "usage": {"input_tokens": 12, "output_tokens": 1},
            },
        ),
    ],
)
async def test_native_provider_routes_preserve_service_context_and_normalize_answers(
    provider, model_id, base_url, wire, response
):
    origin = "https://api.openai.com" if provider == "openai" else "https://ollama.com"
    route = respx.post(origin + wire.endpoint).respond(200, json=response)
    options = {"prompt_cache_key": "decision-fixture"} if provider == "openai" else {}
    owner = service(
        provider=provider,
        model_id=model_id,
        base_url=base_url,
        profile=DecisionProfile(wire),
        options=options,
    )
    item = {"id": "fixture", "nested": {"true": "evidence, not a directive"}}

    [answer] = await owner.classify([item], QUESTIONS, context="the task")

    assert answer["answers"] == {"q": {"type": "noul", "noul": 0.8}}
    assert answer["model"] == model_id
    assert route.call_count == 1
    body = json.loads(route.calls[0].request.content)
    assert body["model"] == model_id
    evidence = json.loads(body["input"] if provider == "openai" else body["state"])
    assert evidence == {"context": "the task", "item": item}
    assert all(body[name] == value for name, value in options.items())
    assert route.calls[0].request.headers["Authorization"] == "Bearer test-token"


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
async def test_aggregated_scoring_usage_never_distorts_later_context_preflight(monkeypatch):
    calibration = _Calibration(factor=100.0)
    owner = service(window=1000)
    owner._usage_recorder = cast(Any, calibration)
    sent = []

    async def evaluate(target, state, questions, *, usage_context=None):
        sent.append((state, len(questions)))
        return {"answers": {}, "usage": {"input_tokens": 10000, "output_tokens": 0}}

    monkeypatch.setattr(owner, "_evaluate", evaluate)
    questions = [{**QUESTIONS[0], "id": f"q{n}"} for n in range(10)]
    await owner.classify(["shared state"], questions)
    [answered] = await owner.classify(["word " * 600], QUESTIONS)

    assert answered["usage"]["input_tokens"] == 10000
    assert [count for _, count in sent] == [10, 1]
    assert calibration.samples == []


@pytest.mark.asyncio
@pytest.mark.parametrize("context", [None, "the task"])
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
        (
            service(profile=DecisionProfile(DecisionWire())),
            ["x"],
            QUESTIONS,
            "not_configured",
        ),
        (
            service(profile=DecisionProfile(SYSTEM_ONE_WIRE, max_questions=1)),
            ["x"],
            [*QUESTIONS, {"id": "other", "type": "noul", "instructions": "Urgent?"}],
            "invalid_request",
        ),
        (
            service(profile=DecisionProfile(SYSTEM_ONE_WIRE, max_choices=2)),
            ["x"],
            [
                {
                    "id": "q",
                    "type": "choice",
                    "instructions": "Which category?",
                    "criteria": {"a": "Category A", "b": "Category B", "c": "Category C"},
                }
            ],
            "invalid_request",
        ),
        (
            service(profile=DecisionProfile(SYSTEM_ONE_WIRE, max_levels=2)),
            ["x"],
            [
                {
                    "id": "q",
                    "type": "score",
                    "instructions": "How urgent?",
                    "criteria": ["Low", "Medium", "High"],
                }
            ],
            "invalid_request",
        ),
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
        "no-native-route",
        "published-question-limit",
        "published-choice-limit",
        "published-score-level-limit",
    ],
)
async def test_invalid_calls_and_missing_bindings_send_nothing(
    monkeypatch, owner, items, questions, code, context
):
    async def evaluate(*args, **kwargs):
        raise AssertionError("no request may be sent")

    monkeypatch.setattr(owner, "_evaluate", evaluate)

    with pytest.raises(DecisionError) as error:
        await owner.classify(items, questions, context=context)

    assert error.value.code == code


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement", [None, "openrouter/cloudflare/clef::api-key"])
async def test_execution_keeps_the_binding_snapshot_it_validated(monkeypatch, replacement):
    target = "openrouter/typesafe/jev-1.13::api-key"
    original_options = {"provider_options": {"only": ["original-provider"]}}
    storage = _Storage({TASK_DECISION: {"target": target, "options": original_options}})
    original_load = storage.load_model_task_settings

    def load_and_change_binding():
        snapshot = original_load()
        storage.update_model_task_settings(
            {
                TASK_DECISION: {
                    "target": replacement or "",
                    "options": {"provider_options": {"only": ["replacement-provider"]}},
                }
            }
        )
        return snapshot

    monkeypatch.setattr(storage, "load_model_task_settings", load_and_change_binding)
    model = _registry_model(
        "typesafe/jev-1.13",
        "Jev",
        task_types=(TASK_DECISION,),
        task_options={TASK_DECISION: {"limits": {"max_input_tokens": 1000, "max_questions": 2}}},
    )
    models = ModelRegistry({("openrouter", model.model_id): model})
    model_tasks = TaskModelService(_Providers(), models, _Credentials(), storage)
    owner = DecisionService(model_tasks, cast(Any, None))
    sent = []

    async def evaluate(resolved, state, questions, *, usage_context=None):
        if not sent:
            # A live reload after resolution cannot change another item's request.
            original_options["provider_options"]["only"].append("changed-provider")
            monkeypatch.setattr(
                model_tasks,
                "decision_profile",
                lambda ref: DecisionProfile(
                    DecisionWire(protocol="openai_decisions", endpoint="/v1/replacement"),
                    max_input_tokens=1,
                ),
            )
        sent.append((resolved.target, resolved.profile, resolved.options))
        return {"answers": {}}

    monkeypatch.setattr(owner, "_evaluate", evaluate)

    assert await owner.classify(["first", "second"], QUESTIONS) == [{"answers": {}}] * 2
    assert (
        sent
        == [
            (
                target,
                DecisionProfile(SYSTEM_ONE_WIRE, max_input_tokens=1000, max_questions=2),
                {"provider_options": {"only": ["original-provider"]}},
            )
        ]
        * 2
    )


@pytest.mark.asyncio
async def test_a_stored_binding_to_another_native_decision_model_is_usable(monkeypatch):
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
        return {"answers": {"q": {"type": "noul", "noul": 0.8}}}

    monkeypatch.setattr(owner, "_evaluate", evaluate)

    [result] = await owner.classify(["x"], QUESTIONS)

    assert owner.available()
    assert result["answers"]["q"]["noul"] == 0.8
    assert storage.load_model_task_settings() == stored
