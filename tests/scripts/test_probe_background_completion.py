"""Tests for the live background-completion Provider probe."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = PROJECT_ROOT / "scripts" / "probe_background_completion.py"


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("background_completion_probe", MODULE_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError(f"Unable to load module from {MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


PROBE = _load_module()


class _FakeAdapter:
    def __init__(self) -> None:
        self.closed = False

    async def send(self, _messages: list[dict[str, Any]], **_kwargs: Any) -> dict[str, Any]:
        return {"content": "live"}

    def normalize_response(
        self,
        response: dict[str, Any],
        *,
        model_id: str,
    ) -> dict[str, Any]:
        del model_id
        return response

    async def stream(self, _messages: list[dict[str, Any]], **_kwargs: Any) -> Any:
        yield {"type": "content_delta", "text": "live"}
        yield {"type": "finish", "reason": "stop"}

    def reasoning_replay_policy(self, _model_id: str) -> str:
        return "current_run"

    def wire_media_support(self, _model_id: str) -> frozenset[str]:
        return frozenset()

    def request_context_kwargs(self, **_kwargs: Any) -> dict[str, Any]:
        return {}

    async def aclose(self) -> None:
        self.closed = True


def test_nonstream_wrapper_synthesizes_first_step_and_delegates_second() -> None:
    async def exercise() -> None:
        delegate = _FakeAdapter()
        wrapper = PROBE._SyntheticFirstStepAdapter(delegate)
        synthetic = await wrapper.send([], model_id="model")
        normalized = wrapper.normalize_response(synthetic, model_id="model")
        live = await wrapper.send([{"role": "user", "content": "next"}], model_id="model")

        assert normalized["tool_calls"] == [
            {
                "id": PROBE.PROBE_TOOL_CALL_ID,
                "name": PROBE.PROBE_TOOL_NAME,
                "arguments": {},
            }
        ]
        assert live == {"content": "live"}
        assert wrapper.synthetic_request_count == 1
        assert wrapper.live_request_count == 1
        await wrapper.aclose()
        assert delegate.closed is True

    asyncio.run(exercise())


def test_stream_wrapper_synthesizes_tool_call_then_delegates() -> None:
    async def collect(stream: Any) -> list[dict[str, Any]]:
        return [delta async for delta in stream]

    async def exercise() -> None:
        wrapper = PROBE._SyntheticFirstStepAdapter(_FakeAdapter())

        synthetic = await collect(wrapper.stream([], model_id="model"))
        live = await collect(
            wrapper.stream([{"role": "user", "content": "next"}], model_id="model")
        )

        assert [delta["type"] for delta in synthetic] == ["tool_call_delta", "finish"]
        assert synthetic[0]["name_delta"] == PROBE.PROBE_TOOL_NAME
        assert [delta["type"] for delta in live] == ["content_delta", "finish"]
        assert wrapper.synthetic_request_count == 1
        assert wrapper.live_request_count == 1

    asyncio.run(exercise())


def test_request_measurements_require_contiguous_tool_cycle_and_later_reminder() -> None:
    token = "VBOT_COMPLETION_TEST"
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "request"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "call", "name": "probe", "arguments": {}}],
        },
        {"role": "tool", "tool_call_id": "call", "content": "result"},
        {
            "role": "user",
            "content": f"<system-reminder>\n{token}\n</system-reminder>",
        },
    ]

    result = PROBE._completion_request_measurements(messages, token)

    assert result == {
        "request_roles": ["system", "user", "assistant", "tool", "user"],
        "one_completion_reminder": True,
        "tool_cycle_contiguous": True,
        "reminder_after_tool_result": True,
    }


def test_result_pass_requires_every_behavioral_invariant() -> None:
    result = {
        "status": "completed",
        "synthetic_requests": 1,
        "live_provider_requests": 1,
        "model_steps": 2,
        "completion_acknowledged": True,
        "one_completion_note_persisted": True,
        "one_completion_reminder": True,
        "tool_cycle_contiguous": True,
        "reminder_after_tool_result": True,
        "verification_token_observed": True,
        "no_follow_up_run": True,
        "session_order_valid": True,
    }

    assert PROBE._result_passed(result) is True
    result["no_follow_up_run"] = False
    assert PROBE._result_passed(result) is False
