"""Smoke test of the live wire profile verification against a mocked Chat wire."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

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
    save_custom_provider_entry,
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

_USAGE = {
    "prompt_tokens": 5,
    "completion_tokens": 3,
    "completion_tokens_details": {"reasoning_tokens": 2},
}
_REPLY = {
    "choices": [
        {
            "message": {"role": "assistant", "content": "ready", "thinking": "Trace"},
            "finish_reason": "stop",
        }
    ],
    "usage": _USAGE,
}


def _tool_call_reply(prompt_tokens: int) -> dict[str, object]:
    call = {
        "id": "call_1",
        "type": "function",
        "function": {"name": "get_weather", "arguments": '{"city": "Paris"}'},
    }
    message = {"role": "assistant", "content": "", "thinking": "Check Paris", "tool_calls": [call]}
    return {
        "choices": [{"message": message, "finish_reason": "tool_calls"}],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": 9},
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
        body = json.loads(request.content)
        # The opening question of a Tool turn gets the Tool Call.
        if len(body["messages"]) == 1 and body.get("tools"):
            return httpx.Response(200, json=_tool_call_reply(len(request.content)))
        if body.get("stream"):
            return sse_response(
                sse(
                    {"choices": [{"delta": {"thinking": "Trace"}}]},
                    {"choices": [{"delta": {"content": "ready"}, "finish_reason": "stop"}]},
                )
            )
        usage = {**_USAGE, "prompt_tokens": len(request.content)}
        return httpx.Response(200, json={**_REPLY, "usage": usage})

    with respx.mock:
        respx.post(MINIMAL_URL).mock(side_effect=reply)
        results = await run_checks(adapter, "m", checks=("send", "efforts", "replay"))

    assert [(result.name, result.status) for result in results] == [
        ("send", "ok"),
        ("effort:low", "ok"),
        ("effort:high", "ok"),
        ("effort:none", "ok"),
        ("replay", "ok"),
    ]
    # Input tokens follow the request size here, so the replayed carrier is measured as consumed.
    replay = results[-1].facts
    assert (
        replay["in_run"]["result"]
        == replay["cross_run"]["result"]
        == replay["answer_turn"]["result"]
        == "consumed"
    )
    assert replay["measured_scope"] == replay["profile_scope"] == "full_history"
    assert replay["in_run"]["a"] < min(replay["in_run"]["b"], replay["in_run"]["c"])
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

    # An existing hand-formatted file changes only in the written Model entry.
    hand = (
        '{\n  "format_version": 1,\n'
        '  "defaults": {"request": {"allowed_parameters": ["top_p"]}},\n'
        '  "models": {\n    "other": {"set": {"reasoning": {"levels": []}}}\n  }\n}\n'
    )
    hand_path = tmp_path / "hand.json"
    hand_path.write_text(hand, encoding="utf-8", newline="\n")
    write_entry(tmp_path, "hand", "m", entry)
    written = hand_path.read_text(encoding="utf-8")
    assert written.startswith(hand[: hand.index("\n  }\n}")])
    assert json.loads(written)["models"]["m"] == json.loads(path.read_text())["models"]["m"]
    write_entry(tmp_path, "hand", "m", entry)
    assert hand_path.read_text(encoding="utf-8") == written
    # Verifying another Connection adds it to the evidence.
    other = {"verified": {**entry["verified"], "connections": ["subscription"]}}
    write_entry(tmp_path, "hand", "m", other)
    verified = json.loads(hand_path.read_text(encoding="utf-8"))["models"]["m"]["verified"]
    assert verified["connections"] == ["api-key", "subscription"]

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

    # With a running server the entry goes through provider.custom_save, based on
    # the listed revision and without the listing's runtime fields.
    calls: list[tuple[str, dict[str, Any]]] = []

    def call(method: str, params: dict[str, Any]) -> dict[str, Any]:
        calls.append((method, params))
        if method == "provider.custom_list":
            listed = {"id": "local-ai", **record, "revision": "rev-1", "usable": True}
            return {"providers": [listed]}
        return {}

    save_custom_provider_entry(call, "local-ai", "m2", custom_entry)
    method, params = calls[-1]
    assert method == "provider.custom_save"
    assert params["expected_revision"] == "rev-1"
    assert "usable" not in params["provider"]
    assert set(params["provider"]["wire"]["models"]) == {"m", "m2"}
