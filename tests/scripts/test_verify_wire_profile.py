"""Smoke test of the live wire profile verification against a mocked Chat wire."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from core.providers._wire_profile_files import parse_wire_profile_file
from core.providers.wire_observations import WireObservations
from core.providers.wire_profiles import WireProfiles, custom_provider_wire_file
from core.storage.storage import StorageManager
from scripts._wire_verify.checks import run_checks
from scripts._wire_verify.proposal import (
    propose_entry,
    write_custom_provider_entry,
    write_entry,
)
from tests.core.providers.openai_compatible_test_support import (
    MINIMAL_URL,
    NO_DEFAULTS_CONFIG,
    catalog_model,
    make_adapter,
    sse,
    sse_response,
)

_REPLY = {
    "choices": [
        {
            "message": {"role": "assistant", "content": "ready", "thinking": "Trace"},
            "finish_reason": "stop",
        }
    ],
    "usage": {
        "prompt_tokens": 5,
        "completion_tokens": 3,
        "completion_tokens_details": {"reasoning_tokens": 2},
    },
}


@pytest.mark.asyncio
async def test_a_clean_run_proposes_a_verified_entry_that_parses_as_a_wire_file(
    tmp_path: Path,
) -> None:
    model = catalog_model("m", levels=("low", "high"))
    adapter = make_adapter(NO_DEFAULTS_CONFIG, model=model)
    observations = WireObservations(None, save_delay=None)
    profiles = WireProfiles(
        files={},
        protocol_support=lambda _provider_id: ("chat_completions",),
        model_resolver=lambda _provider_id, _model_id: model,
        report=lambda issue: None,
        observations=observations,
    )
    adapter.bind_wire_profiles(profiles.bind("minimal", "api-key"))

    def reply(request: httpx.Request) -> httpx.Response:
        if json.loads(request.content).get("stream"):
            return sse_response(
                sse(
                    {"choices": [{"delta": {"thinking": "Trace"}}]},
                    {"choices": [{"delta": {"content": "ready"}, "finish_reason": "stop"}]},
                )
            )
        return httpx.Response(200, json=_REPLY)

    with respx.mock:
        respx.post(MINIMAL_URL).mock(side_effect=reply)
        results = await run_checks(adapter, "m", checks=("send", "efforts"))

    assert [(result.name, result.status) for result in results] == [
        ("send", "ok"),
        ("effort:low", "ok"),
        ("effort:high", "ok"),
        ("effort:none", "ok"),
    ]
    entry = propose_entry(
        adapter.wire_profile("m"),
        observations.facts_for("minimal", "api-key", "m"),
        results,
        connection_id="api-key",
        today="2026-10-02",
    )
    assert entry["set"]["response"]["reasoning_fields"][0] == "thinking"
    assert entry["verified"]["connections"] == ["api-key"]

    path = write_entry(tmp_path, "minimal", "m", entry)
    issues: list[str] = []
    parsed = parse_wire_profile_file(
        "minimal",
        json.loads(path.read_text(encoding="utf-8")),
        source=str(path),
        report=issues.append,
    )
    assert parsed is not None
    assert issues == []
    assert parsed.models["m"].verification is not None

    # A Custom Provider's entry goes into its Settings wire block, next to what is there.
    storage = StorageManager(tmp_path / "data")
    storage.save_custom_provider_settings(
        "local-ai",
        {
            "name": "Local AI",
            "adapter": "openai_compatible",
            "base_url": "http://127.0.0.1:8080/v1",
            "wire": {"defaults": {"reasoning": {"dialect": "thinking_toggle"}}},
        },
    )
    custom_entry = {**entry, "verified": {**entry["verified"], "connections": ["default"]}}
    write_custom_provider_entry(storage, "local-ai", "m", custom_entry)
    record = storage.load_custom_providers_settings()["local-ai"]
    block = custom_provider_wire_file("local-ai", record, report=issues.append)

    assert block is not None
    assert issues == []
    assert block.defaults["reasoning"]["dialect"] == "thinking_toggle"
    assert block.models["m"].verification is not None
    assert not (tmp_path / "local-ai.json").exists()
