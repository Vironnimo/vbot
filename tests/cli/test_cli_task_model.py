"""Tests for the ``vbot task-model`` commands: bindings, options, RPC requests and output."""

from __future__ import annotations

import io
import json
import sys
from typing import Any

import pytest

from tests.cli.cli_test_support import FakeRpc, RunCli

TTS_TARGET = "openrouter/microsoft/mai-voice-2::api-key"


def _saved(task_type: str, target: str, options: dict[str, Any]) -> dict[str, Any]:
    return {"model_tasks": {task_type: {"target": target, "options": options}}}


def test_task_model_list_prints_one_row_per_binding(rpc: FakeRpc, run_cli: RunCli) -> None:
    rpc.reply(
        "task_model.settings",
        {
            "model_tasks": {
                "text_to_speech": {
                    "target": "openai/gpt-4o-mini-tts::api-key",
                    "options": {"voice": "alloy"},
                },
                "speech_to_text": {"target": "openai/gpt-4o-transcribe::api-key", "options": {}},
            }
        },
    )

    code, out, _err = run_cli("task-model", "list")

    assert code == 0
    assert rpc.calls == [("task_model.settings", {})]
    assert out.splitlines()[1:] == [
        "- speech_to_text: target=openai/gpt-4o-transcribe::api-key options={}",
        '- text_to_speech: target=openai/gpt-4o-mini-tts::api-key options={"voice": "alloy"}',
    ]


def test_task_model_list_reports_the_empty_state(rpc: FakeRpc, run_cli: RunCli) -> None:
    rpc.reply("task_model.settings", {"model_tasks": {}})

    code, out, _err = run_cli("task-model", "list")

    assert code == 0
    assert out.strip()


def test_task_model_status_reports_configured_and_usable(rpc: FakeRpc, run_cli: RunCli) -> None:
    rpc.reply(
        "task_model.status", {"task_type": "text_to_speech", "configured": True, "usable": True}
    )

    code, out, _err = run_cli("task-model", "status", "text_to_speech")

    assert code == 0
    assert rpc.calls == [("task_model.status", {"task_type": "text_to_speech"})]
    assert out.splitlines() == ["task-model text_to_speech: configured=yes usable=yes"]


def test_task_model_targets_prints_one_row_per_target(rpc: FakeRpc, run_cli: RunCli) -> None:
    rpc.reply(
        "task_model.list_targets",
        {
            "targets": [
                {
                    "id": "openai/gpt-4o-transcribe::api-key",
                    "kind": "provider",
                    "label": "OpenAI · GPT-4o Transcribe",
                    "usable": True,
                }
            ]
        },
    )

    code, out, _err = run_cli("task-model", "targets", "speech_to_text")

    assert code == 0
    assert rpc.calls == [("task_model.list_targets", {"task_type": "speech_to_text"})]
    assert out.splitlines()[1:] == [
        "- id=openai/gpt-4o-transcribe::api-key kind=provider "
        "label=OpenAI · GPT-4o Transcribe usable=yes"
    ]


def test_task_model_commands_reject_an_unknown_task_type(rpc: FakeRpc, run_cli: RunCli) -> None:
    with pytest.raises(SystemExit) as exc_info:
        run_cli("task-model", "targets", "audio_effect_generation")

    assert exc_info.value.code == 2
    assert rpc.calls == []


@pytest.mark.parametrize(
    ("target", "params"),
    [
        pytest.param(
            ("openai/tts::api-key",),
            {"task_type": "text_to_speech", "target": "openai/tts::api-key"},
            id="explicit-target",
        ),
        pytest.param((), {"task_type": "text_to_speech"}, id="current-binding"),
    ],
)
def test_task_model_options_prints_the_option_schema(
    rpc: FakeRpc, run_cli: RunCli, target: tuple[str, ...], params: dict[str, str]
) -> None:
    schema = {"configured_options": {"voice": "Harper"}, "fields": []}
    rpc.reply("task_model.options", {"schema": schema})

    code, out, _err = run_cli("task-model", "options", "text_to_speech", *target)

    assert code == 0
    assert rpc.calls == [("task_model.options", params)]
    assert json.loads(out) == schema


@pytest.mark.parametrize(
    ("options", "stdin", "saved_options"),
    [
        pytest.param(("--options", '{"dimensions": 512}'), None, {"dimensions": 512}, id="json"),
        pytest.param(
            ("--option", "voice", "Harper", "--option", "speed", "1.25"),
            None,
            {"voice": "Harper", "speed": 1.25},
            id="typed-pairs",
        ),
        pytest.param(("--options-stdin",), '{"voice":"Harper"}\n', {"voice": "Harper"}, id="stdin"),
    ],
)
def test_task_model_set_replaces_the_binding_with_the_given_options(
    rpc: FakeRpc,
    run_cli: RunCli,
    monkeypatch: pytest.MonkeyPatch,
    options: tuple[str, ...],
    stdin: str | None,
    saved_options: dict[str, Any],
) -> None:
    if stdin is not None:
        monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    rpc.reply("task_model.update", _saved("text_to_speech", TTS_TARGET, saved_options))

    code, out, _err = run_cli("task-model", "set", "text_to_speech", TTS_TARGET, *options)

    assert code == 0
    assert rpc.calls == [
        (
            "task_model.update",
            {"model_tasks": {"text_to_speech": {"target": TTS_TARGET, "options": saved_options}}},
        )
    ]
    assert out.splitlines() == [
        f"text_to_speech: target={TTS_TARGET} options={json.dumps(saved_options, sort_keys=True)}"
    ]


@pytest.mark.parametrize("options_json", ["{not json", '["a"]'])
def test_task_model_set_rejects_options_that_are_not_a_json_object(
    rpc: FakeRpc, run_cli: RunCli, options_json: str
) -> None:
    code, out, _err = run_cli(
        "task-model", "set", "text_embedding", "local/whisper", "--options", options_json
    )

    assert code == 1
    assert "--options" in out
    assert rpc.calls == []


def test_task_model_clear_saves_an_empty_target(rpc: FakeRpc, run_cli: RunCli) -> None:
    rpc.reply("task_model.update", {"model_tasks": {}})

    code, out, _err = run_cli("task-model", "clear", "image_generation")

    assert code == 0
    assert rpc.calls == [
        ("task_model.update", {"model_tasks": {"image_generation": {"target": ""}}})
    ]
    assert "image_generation" in out


@pytest.mark.parametrize(
    ("argv", "stdin", "patch", "saved_options"),
    [
        pytest.param(
            ("set-option", "text_to_speech", "speed", "1.25"),
            None,
            {"set": {"speed": 1.25}},
            {"voice": "Harper", "speed": 1.25},
            id="set-typed-value",
        ),
        pytest.param(
            ("set-option", "text_to_speech", "extra_options", "--stdin"),
            '{"style":"friendly"}\n',
            {"set": {"extra_options": {"style": "friendly"}}},
            {"extra_options": {"style": "friendly"}},
            id="set-json-from-stdin",
        ),
        pytest.param(
            ("unset-option", "text_to_speech", "speed"),
            None,
            {"unset": ["speed"]},
            {"voice": "Harper"},
            id="unset",
        ),
    ],
)
def test_task_model_option_commands_patch_one_option_and_print_the_saved_binding(
    rpc: FakeRpc,
    run_cli: RunCli,
    monkeypatch: pytest.MonkeyPatch,
    argv: tuple[str, ...],
    stdin: str | None,
    patch: dict[str, Any],
    saved_options: dict[str, Any],
) -> None:
    if stdin is not None:
        monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    rpc.reply("task_model.patch_options", _saved("text_to_speech", TTS_TARGET, saved_options))

    code, out, _err = run_cli("task-model", *argv)

    assert code == 0
    assert rpc.calls == [("task_model.patch_options", {"task_type": "text_to_speech", **patch})]
    assert out.rstrip().endswith(f"options={json.dumps(saved_options, sort_keys=True)}")
