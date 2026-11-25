"""Local load retries keep the original request's input and output capacity."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any
from unittest.mock import Mock

import httpx
import pytest
import respx

from core.chat._request_targets import RequestTargets
from core.compaction._model_request import _reported_input_capacity
from core.providers._wire_profile_files import parse_wire_profile_file
from core.providers.adapter import request_input_budget
from core.providers.errors import ProviderError
from core.providers.lmstudio import LMStudioAdapter
from core.providers.ollama import OllamaAdapter
from core.providers.providers import RequestInput
from core.providers.wire_profiles import standalone_wire_binding
from tests.core.providers.ollama_test_support import (
    LOCAL_CHAT_URL,
    LOCAL_CONFIG,
    SAMPLE_MESSAGES,
    TEXT_RESPONSE,
    local_adapter,
    model_lookup,
)
from tests.core.providers.test_lmstudio import (
    CHAT_RESPONSE,
    LMSTUDIO_CONFIG,
    MODEL_ID,
    NATIVE_MODEL,
)


def _load_error() -> httpx.Response:
    return httpx.Response(400, json={"error": "loading model: CUDA out of memory"})


@respx.mock
@pytest.mark.asyncio
async def test_ollama_learned_window_leaves_larger_requests_admissible() -> None:
    model = "plain-model"
    adapter = local_adapter(local_context_resolver=lambda _: 8192)
    ok = httpx.Response(200, json=TEXT_RESPONSE)
    route = respx.post(LOCAL_CHAT_URL).mock(side_effect=[_load_error(), ok, ok, ok, ok])
    try:
        await adapter.send(SAMPLE_MESSAGES, model_id=model, max_tokens=512)
        assert adapter.reported_context_window(model) == 4096
        dependencies = Mock()
        catalog = dependencies.models.get.return_value
        catalog.context_window_for.return_value = 65536
        catalog.metadata = {"ollama": {"local": True}}
        dependencies.providers.get.return_value = LOCAL_CONFIG
        dependencies.get_local_context_windows.return_value = {"ollama/plain-model": 8192}
        targets = RequestTargets(dependencies, max_image_bytes=None)
        target = Mock(adapter=adapter, model_id=model, provider_id="ollama", connection_id="local")
        assert targets.resolve_context_window(None, target) == 8192
        assert _reported_input_capacity(adapter, model, {}) is None
        await adapter.send(SAMPLE_MESSAGES, model_id=model, max_tokens=512)
        with request_input_budget(model, RequestInput.measured(5200)):
            await adapter.send(
                [{"role": "user", "content": "long " * 5000}], model_id=model, max_tokens=512
            )
        await adapter.send(SAMPLE_MESSAGES, model_id=model, max_output_tokens=4096)
    finally:
        await adapter.aclose()

    options = [json.loads(call.request.content)["options"] for call in route.calls]
    assert [option["num_ctx"] for option in options] == [8192, 4096, 4096, 8192, 8192]
    assert options[-1]["num_predict"] == 4096


@pytest.mark.parametrize(
    "kind",
    [
        "provider-default",
        "wire-default",
        "wire-input",
        "wire-format",
        "catalog",
        "tools",
        "unbounded",
        "media",
        "unknown",
    ],
)
@respx.mock
@pytest.mark.asyncio
async def test_ollama_reduces_only_when_the_complete_request_fits(kind: str) -> None:
    config = replace(
        LOCAL_CONFIG, defaults={"max_tokens": 4096} if kind == "provider-default" else {}
    )
    model = model_lookup("plain-model")
    assert model is not None
    catalog = replace(model, max_output_tokens=4096) if kind == "catalog" else model
    adapter = OllamaAdapter(
        config, "", local_context_resolver=lambda _: 8192, model_lookup=lambda _: catalog
    )
    if kind in ("wire-default", "wire-input", "wire-format"):
        rules: dict[str, Any] = {"output_limit_default": 4096}
        if kind == "wire-input":
            rules["extra_body"] = {"messages": [{"role": "user", "content": "large " * 8000}]}
        if kind == "wire-format":
            rules["output_limit_default"] = 512
            rules["extra_body"] = {"format": {"type": "string", "description": "schema " * 1400}}
        wire_file = parse_wire_profile_file(
            "ollama",
            {"format_version": 1, "defaults": {"request": rules}},
            source="test",
            report=pytest.fail,
        )
        assert wire_file is not None
        adapter.bind_wire_profiles(
            standalone_wire_binding(
                provider_id="ollama",
                connection_id="local",
                protocols=("ollama_chat",),
                model_lookup=lambda _: catalog,
                files={"ollama": wire_file},
            )
        )
    messages: list[dict[str, Any]] = list(SAMPLE_MESSAGES)
    kwargs: dict[str, Any] = {}
    if kind == "tools":
        kwargs = {
            "max_tokens": 4096,
            "tools": [
                {"name": "lookup", "description": "tool " * 500, "parameters": {"type": "object"}}
            ],
        }
    if kind == "unbounded":
        kwargs = {"max_tokens": -1}
    if kind == "media":
        messages = [
            {
                "role": "user",
                "content": [{"type": "media", "media_type": "image/png", "base64": "AA=="}],
            }
        ]
        kwargs = {"max_tokens": 512}
    route = respx.post(LOCAL_CHAT_URL).mock(
        side_effect=[_load_error(), httpx.Response(200, json=TEXT_RESPONSE)]
    )
    try:
        if kind in ("provider-default", "wire-default", "catalog", "tools"):
            await adapter.send(messages, model_id="test", **kwargs)
            opts = json.loads(route.calls.last.request.content)["options"]
            if kind != "catalog":
                assert opts["num_predict"] == 4096
            assert (4596 if kind == "tools" else 4096) < opts["num_ctx"] < 8192
        else:
            with (
                request_input_budget(
                    "test",
                    RequestInput.measured(6900)
                    if kind == "wire-format"
                    else RequestInput.estimate(60),
                ),
                pytest.raises(ProviderError, match="out of memory"),
            ):
                await adapter.send(messages, model_id="test", **kwargs)
            assert route.call_count == 1
    finally:
        await adapter.aclose()


@pytest.mark.parametrize("remembered", [False, True])
@pytest.mark.parametrize(("measured", "served"), [(1200, 2048), (1200, 6144), (3500, 12288)])
@respx.mock
@pytest.mark.asyncio
async def test_lmstudio_checks_reduced_window_before_inference(
    remembered: bool, measured: int, served: int
) -> None:
    adapter = LMStudioAdapter(LMSTUDIO_CONFIG, "", local_context_resolver=lambda _: 12288)
    if remembered:
        adapter.wire.remember_route(MODEL_ID, "load_window", 6144, setting=12288)
        assert adapter.reported_context_window(MODEL_ID) == 6144
        assert adapter.admission_context_window(MODEL_ID) is None
    instances = [{"id": "i", "config": {"context_length": served}}]
    respx.get("http://localhost:1234/api/v1/models").mock(
        side_effect=[
            httpx.Response(200, json={"models": [{**NATIVE_MODEL, "loaded_instances": listing}]})
            for listing in ([], instances)
        ]
    )
    accepted = httpx.Response(200, json={"instance_id": "i"})
    load = respx.post("http://localhost:1234/api/v1/models/load").mock(
        side_effect=[accepted] if remembered else [_load_error(), accepted]
    )
    chat = respx.post("http://localhost:1234/v1/chat/completions").mock(
        return_value=httpx.Response(200, json=CHAT_RESPONSE)
    )
    try:
        with request_input_budget(MODEL_ID, RequestInput.measured(measured)):
            if served >= measured + 4096:
                assert (
                    await adapter.send(SAMPLE_MESSAGES, model_id=MODEL_ID, max_output_tokens=4096)
                    == CHAT_RESPONSE
                )
                assert chat.call_count == 1
                assert json.loads(chat.calls.last.request.content)["max_output_tokens"] == 4096
            else:
                with pytest.raises(ProviderError, match="2048.*5296"):
                    await adapter.send(SAMPLE_MESSAGES, model_id=MODEL_ID, max_output_tokens=4096)
                assert chat.call_count == 0
        assert adapter.admission_context_window(MODEL_ID) == served
        loads = [json.loads(call.request.content)["context_length"] for call in load.calls]
        if remembered and measured + 4096 > 6144:
            assert loads == [12288]
        else:
            candidate = max(6144, measured + 4096)
            assert loads == ([candidate] if remembered else [12288, candidate])
    finally:
        await adapter.aclose()
