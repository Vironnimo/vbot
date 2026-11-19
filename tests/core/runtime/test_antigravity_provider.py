"""Antigravity subscription enrollment, quota evidence and transport lifetime."""

from __future__ import annotations

import asyncio
import json
import subprocess
from collections.abc import AsyncGenerator
from contextlib import aclosing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from core.providers import _antigravity_native as agy
from core.providers import antigravity
from core.providers.accounts import ConnectionRef
from core.providers.antigravity import AntigravityAdapter
from core.providers.errors import ProviderAuthError
from core.runtime import Runtime
from core.utils.config import Config
from core.utils.errors import ProviderError

_CONNECTION = "antigravity:subscription"


@pytest.mark.asyncio
async def test_opt_in_publishes_cli_models_and_personal_quota_only(
    config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "agy.exe"
    executable.touch()
    monkeypatch.setenv("VBOT_ANTIGRAVITY_PATH", str(executable))
    monkeypatch.setenv("GEMINI_API_KEY", "synthetic-api-key")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "synthetic-credentials")
    monkeypatch.setenv("AGY_ADC_AUTH", "true")
    calls = []
    weekly = {"window": "weekly", "remaining_fraction": 0.5}
    five_hour = {"window": "5h", "remaining_fraction": 0, "reset_time": "2026-11-20T01:00:00Z"}
    buckets = [weekly]

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        assert command[0] == str(executable.resolve())
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert kwargs["env"]["HOME"] == kwargs["cwd"]
        assert kwargs["env"]["AGY_ADC_AUTH"] == "false"
        assert kwargs["env"]["AGY_CLI_DISABLE_AUTO_UPDATE"] == "true"
        assert "GEMINI_API_KEY" not in kwargs["env"]
        assert "GOOGLE_APPLICATION_CREDENTIALS" not in kwargs["env"]
        settings = json.loads(command[command.index("--settings") + 1])
        assert settings["modelProvider"] == "" and settings["useG1Credits"] is False
        assert command[command.index("--output-format") + 1] == "json"
        name = command[-1].lstrip("/")
        calls.append(name)
        data = (
            {
                "models": [
                    {"id": "gemini-4-hydrogen-high", "label": "Gemini 4 Hydrogen (High)"},
                    {"id": "gemini-pro-orchestrator-neon", "label": "Orchestrator (Neon)"},
                ]
            }
            if name == "models"
            else {"groups": [{"name": "Gemini Models", "buckets": buckets}]}
        )
        payload = {"status": "SUCCESS", "num_turns": 0, "command": {"name": name, "data": data}}
        return subprocess.CompletedProcess(
            command, 0, stdout=json.dumps(payload).encode(), stderr=b""
        )

    runtime = Runtime(config, safe_startup_mode="test")
    runtime.start()
    try:
        monkeypatch.setattr(agy.subprocess, "run", run)
        assert (await runtime.refresh_external_connection("antigravity", _CONNECTION))[
            "status"
        ] == "disabled"
        assert calls == []
        runtime.storage.set_provider_connection_enabled(_CONNECTION, True)
        assert (await runtime.refresh_external_connection("antigravity", _CONNECTION))[
            "status"
        ] == "subscription_unconfirmed"
        assert runtime.models.list_for_provider("antigravity") == [] and calls == ["usage"]
        buckets.append(five_hour)
        assert (await runtime.refresh_external_connection("antigravity", _CONNECTION))["connected"]
        assert calls == ["usage", "usage", "models"]
        assert [row.model_id for row in runtime.models.list_for_provider("antigravity")] == [
            "gemini-4-hydrogen-high"
        ]
        model = runtime.models.get("antigravity", "gemini-4-hydrogen-high")
        assert model.name == "Gemini 4 Hydrogen (High)" and model.connections == ("subscription",)
        assert model.context_window_for("subscription") == 1_048_576
        adapter = runtime.get_adapter(ConnectionRef("antigravity", _CONNECTION))
        assert isinstance(adapter, AntigravityAdapter)
        report = await runtime.provider_usage.report([_CONNECTION])
        assert calls == ["usage", "usage", "models"]  # Enrollment already measured the quotas.
        windows = report.providers[0].windows
        assert [(window.window_seconds, window.used_percent) for window in windows] == [
            (604800, 50),
            (18000, 100),
        ]
        assert windows[1].reset_at == five_hour["reset_time"]

        observed: list[dict[str, Any]] = []
        runtime.provider_usage.subscribe_observations(observed.append)
        measured_at = datetime(2026, 11, 19, 14, tzinfo=UTC)
        monkeypatch.setattr(runtime.provider_usage, "_clock", lambda: measured_at)

        async def stream_turn(*_args: Any, **_kwargs: Any) -> AsyncGenerator[dict]:
            assert observed[-1]["observed_at"] == measured_at.strftime("%Y-%m-%dT%H:%M:%S.000000Z")
            yield {"type": "finish", "reason": "stop"}

        monkeypatch.setattr(antigravity, "stream_turn", stream_turn)
        weekly["remaining_fraction"] = 0.4
        for _ in range(2):
            measured_at += timedelta(minutes=1)
            async with aclosing(
                adapter.stream([{"role": "user", "content": "hello"}], model_id=model.model_id)
            ) as stream:
                assert [event["type"] async for event in stream] == ["finish"]
        assert len(observed) == 2  # An unchanged measurement also refreshes its age.
        [latest] = runtime.provider_usage.observations()
        assert latest == observed[-1]
        assert (latest["connection"], latest["account"]) == (_CONNECTION, "default")
        assert latest["windows"][0]["used_percent"] == 60
        assert (await runtime.provider_usage.report([_CONNECTION])).providers[0].to_dict() == {
            key: value for key, value in latest.items() if key != "observed_at"
        }
        assert calls == ["usage", "usage", "models", "usage", "usage"]
        buckets.clear()
        with pytest.raises(ProviderAuthError, match="subscription quotas"):
            async with aclosing(
                adapter.stream([{"role": "user", "content": "hello"}], model_id=model.model_id)
            ) as stream:
                await anext(stream)
        assert (
            runtime.external_connection_status("antigravity", _CONNECTION)["status"]
            == "subscription_unconfirmed"
        )
        assert runtime.connection_reachability(_CONNECTION) is False
        assert runtime.models.get("antigravity", model.model_id) == model
        assert runtime.provider_usage.observations() == [latest]  # Failed checks keep the age.
        await runtime.close_connection_transports("antigravity", "subscription")
        assert runtime.provider_usage.observations() == [] and observed[-1]["windows"] == []
        assert (await runtime.provider_usage.report([_CONNECTION])).providers[0].error
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_disconnect_retires_a_stream_between_independently_timed_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(agy, "_executable", lambda: Path("agy"))
    monkeypatch.setattr(agy, "_windows", lambda _path: ())
    native = agy.AntigravityNative(temporary_files=None)
    closed = asyncio.Event()

    async def stream(_executable: Path) -> AsyncGenerator[int]:
        try:
            yield 1
            await asyncio.Future()
        finally:
            closed.set()

    async with aclosing(native.stream(stream)) as iterator:
        assert await asyncio.wait_for(anext(iterator), timeout=1) == 1
        await native.aclose()
        assert closed.is_set()
        with pytest.raises(ProviderError, match="closed"):
            await anext(iterator)
    with pytest.raises(ProviderAuthError, match="closed"):
        async with native.turn():
            pytest.fail("retired Connection started inference")
