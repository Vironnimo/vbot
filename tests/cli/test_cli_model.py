"""Tests for the ``vbot model`` commands: catalog filters, refresh requests and output."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from cli import model_management
from cli.server_management import ServerInstance
from tests.cli.cli_test_support import FakeRpc, RunCli


def test_model_list_prints_one_row_per_model(rpc: FakeRpc, run_cli: RunCli) -> None:
    rpc.reply(
        "model.list",
        {
            "models": [
                {"id": "openai/gpt-4o", "name": "GPT-4o", "context_window": 128000},
                {"id": "anthropic/claude-sonnet-4", "name": "Claude Sonnet 4"},
            ]
        },
    )

    code, out, _err = run_cli("model", "list")

    assert code == 0
    assert rpc.calls == [("model.list", {})]
    assert out.splitlines()[1:] == [
        "- id: openai/gpt-4o  name: GPT-4o  context_window: 128000",
        # An unknown context window is shown as such, never guessed.
        "- id: anthropic/claude-sonnet-4  name: Claude Sonnet 4  context_window: ?",
    ]


def test_model_list_sends_the_filters_and_prints_effective_window_and_capabilities(
    rpc: FakeRpc, run_cli: RunCli
) -> None:
    rpc.reply(
        "model.list",
        {
            "models": [
                {
                    "id": "ollama/qwen3",
                    "name": "Qwen 3",
                    "context_window": 262144,
                    "effective_context_window": 32768,
                    "reachable": False,
                    "capabilities": {
                        "vision": False,
                        "tools": True,
                        "json_mode": False,
                        "reasoning": {"supported": True},
                        "task_types": ["chat", "text_output"],
                    },
                }
            ]
        },
    )

    code, out, _err = run_cli(
        "model", "list",
        "--provider", "openai",
        "--capability", "tools",
        "--capability", "reasoning",
        "--task", "chat",
        "--input-modality", "text",
        "--output-modality", "text",
        "--min-context-window", "128000",
    )  # fmt: skip

    assert code == 0
    assert rpc.calls == [
        (
            "model.list",
            {
                "provider_id": "openai",
                "capabilities": ["tools", "reasoning"],
                "tasks": ["chat"],
                "input_modalities": ["text"],
                "output_modalities": ["text"],
                "min_context_window": 128000,
            },
        )
    ]
    assert out.splitlines()[1:] == [
        "- id: ollama/qwen3  name: Qwen 3  context_window: 32768  reachable: no  "
        "capabilities: tools,reasoning  tasks: chat,text_output"
    ]


def test_model_list_reports_an_empty_catalog(rpc: FakeRpc, run_cli: RunCli) -> None:
    rpc.reply("model.list", {"models": []})

    code, out, _err = run_cli("model", "list")

    assert code == 0
    assert out.strip()


def test_model_list_reports_a_server_error(rpc: FakeRpc, run_cli: RunCli) -> None:
    rpc.fail("model.list", "internal_error", "refresh failed", status=500)

    code, out, _err = run_cli("model", "list")

    assert code == 1
    assert "internal_error: refresh failed" in out


def test_model_show_prints_the_complete_model_data(rpc: FakeRpc, run_cli: RunCli) -> None:
    model = {
        "id": "openrouter/microsoft/mai-voice-2",
        "capabilities": {
            "supported_voices": ["Harper", "Klaus", "Soleil", "Valeria"],
            "task_types": ["text_to_speech"],
        },
        "metadata": {"source": "openrouter"},
    }
    rpc.reply("model.get", {"model": model})

    code, out, _err = run_cli("model", "show", "openrouter/microsoft/mai-voice-2")

    assert code == 0
    assert rpc.calls == [("model.get", {"model": "openrouter/microsoft/mai-voice-2"})]
    assert json.loads(out) == model


@pytest.mark.parametrize(
    ("provider", "params", "result", "shown"),
    [
        pytest.param(
            (), {}, {"refreshed_count": 2, "model_count": 50}, ["2", "50"], id="all-providers"
        ),
        pytest.param(
            ("openai",),
            {"provider_id": "openai"},
            {"provider_id": "openai"},
            ["openai"],
            id="one-provider",
        ),
    ],
)
def test_model_refresh_refreshes_the_model_database(
    rpc: FakeRpc,
    run_cli: RunCli,
    provider: tuple[str, ...],
    params: dict[str, str],
    result: dict[str, Any],
    shown: list[str],
) -> None:
    rpc.reply("model.refresh_db", result)

    code, out, _err = run_cli("model", "refresh", *provider)

    assert code == 0
    assert rpc.calls == [("model.refresh_db", params)]
    for text in shown:
        assert text in out


@pytest.mark.parametrize(
    ("arguments", "params", "forgotten", "shown"),
    [
        pytest.param(
            ("openai/gpt-5.2", "--connection", "oauth"),
            {"model": "openai/gpt-5.2", "connection": "oauth"},
            1,
            "forgot learned wire facts of 1 Model target(s) for openai/gpt-5.2 on oauth",
            id="one-model-on-one-connection",
        ),
        pytest.param(
            ("openai",),
            {"model": "openai"},
            0,
            "no learned wire facts for openai",
            id="whole-provider",
        ),
    ],
)
def test_model_forget_wire_facts(
    rpc: FakeRpc,
    run_cli: RunCli,
    arguments: tuple[str, ...],
    params: dict[str, str],
    forgotten: int,
    shown: str,
) -> None:
    rpc.reply("model.forget_wire_facts", {"forgotten": forgotten})

    code, out, _err = run_cli("model", "forget-wire-facts", *arguments)

    assert code == 0
    assert rpc.calls == [("model.forget_wire_facts", params)]
    assert out.splitlines() == [shown]


def test_model_refresh_fails_and_names_the_providers_it_skipped(
    rpc: FakeRpc, run_cli: RunCli
) -> None:
    rpc.reply(
        "model.refresh_db",
        {
            "refreshed_count": 1,
            "model_count": 25,
            "errors": [
                {
                    "provider_id": "openrouter",
                    "connection_id": "openrouter:api-key",
                    "error": "503 upstream down",
                }
            ],
        },
    )

    code, out, _err = run_cli("model", "refresh")

    assert code == 1
    for text in ("1", "25", "openrouter:api-key"):
        assert text in out


def test_model_refresh_can_target_the_expected_system_database(
    rpc: FakeRpc, instance: ServerInstance, tmp_path: Path
) -> None:
    # The system target has no CLI option; the model DB refresh developer script uses it.
    resources_dir = tmp_path / "checkout" / "resources"
    rpc.reply("model.refresh_db", {"provider_id": "openai"})

    result = model_management.model_refresh(
        instance, provider_id="openai", target="system", expected_resources_dir=resources_dir
    )

    assert result.ok is True
    assert rpc.calls == [
        (
            "model.refresh_db",
            {
                "provider_id": "openai",
                "target": "system",
                "expected_resources_dir": str(resources_dir.resolve()),
            },
        )
    ]
