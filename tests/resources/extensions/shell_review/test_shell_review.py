"""Bundled shell-review judgments through their real registration and management path."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from core.extensions.operations import ExtensionHost
from core.model_tasks.decision_types import DecisionError
from tests.resources.extensions.bundled_test_support import load_bundled


def _request(**changes: Any) -> dict[str, Any]:
    return {
        "platform": "windows",
        "shell": "powershell",
        "cwd": "C:/project",
        "command": "Get-ChildItem",
        "user_requests": ["List the files in this project."],
        **changes,
    }


def _answer(probability: float = 0.98) -> dict[str, Any]:
    return {
        "answers": {"allow": {"type": "noul", "noul": probability}},
        "model": "fixture/decision",
    }


async def _extension(tmp_path: Path, judge: Any, config: dict[str, Any] | None = None):
    registry = load_bundled("shell_review", config_provider=lambda _name: config or {})
    record = next(item for item in registry.records() if item.name == "shell_review")
    assert record.status == "loaded"
    assert not record.capability_errors
    host = ExtensionHost(
        data_dir=tmp_path,
        sample=AsyncMock(),
        resolve_agent=lambda *_: None,
        resolve_tool_agent=lambda *_: None,
        store_attachment=lambda *_: None,
        resolve_credential=lambda *_: "",
        set_credential=lambda *_: None,
        decide=judge,
    )
    registry.bind_host(host)
    await registry.fire_startup()
    return registry, registry.management("shell_review")


@pytest.mark.asyncio
async def test_preview_uses_the_native_model_and_live_criteria_without_shell_execution(
    tmp_path: Path,
) -> None:
    config: dict[str, Any] = {}
    judge = AsyncMock(return_value=_answer())
    registry, operations = await _extension(tmp_path, judge, config)
    request = _request()
    first = await operations.invoke("review", request)
    assert first["decision"] == "allow"
    assert first["model"] == "fixture/decision"
    assert judge.await_args is not None
    assert judge.await_args.args[1]["state"] == request
    assert request == _request()
    assert registry.records()[0].declarations.tools == []

    config.update(policy="Operator criteria sentinel", allow_threshold=1.0)
    second = await operations.invoke("review", request)
    assert second["decision"] == "ask"
    assert judge.await_args is not None
    assert judge.await_args.args[1]["questions"][0]["instructions"] == config["policy"]
    assert second["allow_threshold"] == 1.0
    await registry.fire_shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("answer", "reason"),
    [
        (_answer(0.5), "confirmation_needed"),
        ({"answers": {"allow": {"type": "refusal"}}}, "unanswered"),
        ({"answers": {}}, "unanswered"),
        (_answer(float("nan")), "unanswered"),
        (_answer(True), "unanswered"),
        (DecisionError("Unavailable", code="not_configured"), "not_configured"),
        (DecisionError("Too large", code="too_large"), "too_large"),
        (TimeoutError(), "timeout"),
    ],
)
async def test_uncertainty_refusals_and_model_failures_request_confirmation(
    tmp_path: Path, answer: Any, reason: str
) -> None:
    judge = AsyncMock(
        **({"side_effect": answer} if isinstance(answer, Exception) else {"return_value": answer})
    )
    registry, operations = await _extension(tmp_path, judge)
    result = await operations.invoke("review", _request())
    assert result["decision"] == "ask"
    assert result["reason_code"] == reason
    await registry.fire_shutdown()


@pytest.mark.asyncio
async def test_no_authority_or_invalid_configuration_never_calls_the_model(tmp_path: Path) -> None:
    judge = AsyncMock(return_value=_answer())
    config: dict[str, Any] = {}
    registry, operations = await _extension(tmp_path, judge, config)
    assert (await operations.invoke("review", _request(user_requests=[])))[
        "reason_code"
    ] == "missing_authority"
    for invalid in (0, float("nan"), True, 1.1):
        config["allow_threshold"] = invalid
        assert (await operations.invoke("review", _request()))["reason_code"] == "invalid_settings"
    judge.assert_not_awaited()
    await registry.fire_shutdown()


@pytest.mark.asyncio
async def test_cancelling_a_review_propagates_without_a_judgment(tmp_path: Path) -> None:
    entered = asyncio.Event()

    async def judge(*_args: Any) -> dict[str, Any]:
        entered.set()
        await asyncio.Future()
        raise AssertionError("unreachable")

    registry, operations = await _extension(tmp_path, judge)
    task = asyncio.create_task(operations.invoke("review", _request()))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await registry.fire_shutdown()


@pytest.mark.asyncio
async def test_unexpected_errors_request_confirmation_without_logging_private_content(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    judge = AsyncMock(side_effect=RuntimeError("private-command-sentinel"))
    registry, operations = await _extension(tmp_path, judge)
    with caplog.at_level(logging.ERROR):
        result = await operations.invoke("review", _request())
    assert result == {"decision": "ask", "reason_code": "review_failed"}
    assert "private-command-sentinel" not in caplog.text
    assert any(record.levelno == logging.ERROR for record in caplog.records)
    await registry.fire_shutdown()
