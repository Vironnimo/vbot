"""Tests for task-model CLI parsing, RPC commands, and output."""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import httpx
import pytest

from cli import main as cli_main
from cli import task_model_management
from cli.server_management import CommandResult, ServerInstance
from core.utils.logging import resolve_daily_log_path


def make_instance(tmp_path: Path, *, port: int = 8420) -> ServerInstance:
    data_dir = tmp_path / "data"
    return ServerInstance(
        host="127.0.0.1",
        port=port,
        data_dir=data_dir,
        url=f"http://127.0.0.1:{port}",
        log_path=resolve_daily_log_path(data_dir),
    )


def test_parse_args_supports_task_model_set_options() -> None:
    args = cli_main.parse_args(
        [
            "task-model",
            "set",
            "text_embedding",
            "openai/text-embedding-3-small::api-key",
            "--options",
            '{"dimensions": 512}',
        ]
    )

    assert args.area == "task-model"
    assert args.command == "set"
    assert args.task_type == "text_embedding"
    assert args.target == "openai/text-embedding-3-small::api-key"
    assert args.options_json == '{"dimensions": 512}'


def test_parse_args_supports_task_model_set_option_pairs() -> None:
    args = cli_main.parse_args(
        [
            "task-model",
            "set",
            "text_to_speech",
            "openrouter/microsoft/mai-voice-2::api-key",
            "--option",
            "voice",
            "Harper",
            "--option",
            "speed",
            "1.25",
        ]
    )

    assert args.options_json is None
    assert args.option_pairs == [["voice", "Harper"], ["speed", "1.25"]]


def test_parse_args_allows_options_for_current_binding() -> None:
    args = cli_main.parse_args(["task-model", "options", "text_to_speech"])

    assert args.target is None


def test_parse_args_supports_json_option_value_from_stdin() -> None:
    args = cli_main.parse_args(
        ["task-model", "set-option", "text_to_speech", "extra_options", "--stdin"]
    )

    assert args.value is None
    assert args.stdin is True


def test_dispatch_task_model_set_option_reads_json_from_stdin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)
    args = cli_main.parse_args(
        ["task-model", "set-option", "text_to_speech", "extra_options", "--stdin"]
    )
    calls: list[tuple[str, str, str]] = []
    monkeypatch.setattr(cli_main.sys, "stdin", io.StringIO('{"style":"friendly"}\n'))

    def fake_set_option(
        _instance: ServerInstance,
        task_type: str,
        name: str,
        value: str,
    ) -> CommandResult:
        calls.append((task_type, name, value))
        return CommandResult(ok=True, message="saved", instance=instance)

    result = cli_main.dispatch_task_model_command(
        args,
        instance,
        set_option_fn=fake_set_option,
    )

    assert result.ok is True
    assert calls == [("text_to_speech", "extra_options", '{"style":"friendly"}')]


def test_dispatch_task_model_set_reads_complete_options_from_stdin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)
    args = cli_main.parse_args(
        [
            "task-model",
            "set",
            "text_to_speech",
            "openrouter/microsoft/mai-voice-2::api-key",
            "--options-stdin",
        ]
    )
    calls: list[str | None] = []
    monkeypatch.setattr(cli_main.sys, "stdin", io.StringIO('{"voice":"Harper"}\n'))

    def fake_set_binding(
        _instance: ServerInstance,
        _task_type: str,
        _target: str,
        options_json: str | None,
        _option_pairs: Any,
    ) -> CommandResult:
        calls.append(options_json)
        return CommandResult(ok=True, message="saved", instance=instance)

    result = cli_main.dispatch_task_model_command(
        args,
        instance,
        set_binding_fn=fake_set_binding,
    )

    assert result.ok is True
    assert calls == ['{"voice":"Harper"}']


def test_parse_args_rejects_unknown_task_type(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli_main.parse_args(["task-model", "targets", "music_generation"])

    assert exc_info.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_task_model_list_formats_bindings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        assert json == {"method": "task_model.settings", "params": {}}
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "model_tasks": {
                        "text_to_speech": {
                            "target": "openai/gpt-4o-mini-tts::api-key",
                            "options": {"voice": "alloy"},
                        },
                        "speech_to_text": {
                            "target": "openai/gpt-4o-transcribe::api-key",
                            "options": {},
                        },
                    }
                },
            },
        )

    monkeypatch.setattr(task_model_management.httpx, "post", fake_post)

    result = task_model_management.task_model_list(instance)

    assert result.ok is True
    assert result.message.splitlines() == [
        "task-model bindings:",
        "- speech_to_text: target=openai/gpt-4o-transcribe::api-key options={}",
        '- text_to_speech: target=openai/gpt-4o-mini-tts::api-key options={"voice": "alloy"}',
    ]


def test_task_model_list_reports_empty_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "result": {"model_tasks": {}}})

    monkeypatch.setattr(task_model_management.httpx, "post", fake_post)

    result = task_model_management.task_model_list(instance)

    assert result == CommandResult(
        ok=True, message="no task-model bindings configured", instance=instance
    )


def test_task_model_targets_formats_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        assert json == {
            "method": "task_model.list_targets",
            "params": {"task_type": "speech_to_text"},
        }
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "targets": [
                        {
                            "id": "openai/gpt-4o-transcribe::api-key",
                            "kind": "provider",
                            "label": "OpenAI · GPT-4o Transcribe",
                            "usable": True,
                        }
                    ]
                },
            },
        )

    monkeypatch.setattr(task_model_management.httpx, "post", fake_post)

    result = task_model_management.task_model_targets(instance, "speech_to_text")

    assert result.ok is True
    assert result.message.splitlines() == [
        "targets for speech_to_text:",
        (
            "- id=openai/gpt-4o-transcribe::api-key kind=provider "
            "label=OpenAI · GPT-4o Transcribe usable=yes"
        ),
    ]


def test_task_model_set_posts_sparse_update(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)
    calls: list[dict[str, Any]] = []

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        calls.append(json)
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "model_tasks": {
                        "text_embedding": {
                            "target": "openai/text-embedding-3-small::api-key",
                            "options": {"dimensions": 512},
                        }
                    }
                },
            },
        )

    monkeypatch.setattr(task_model_management.httpx, "post", fake_post)

    result = task_model_management.task_model_set(
        instance,
        "text_embedding",
        "openai/text-embedding-3-small::api-key",
        '{"dimensions": 512}',
    )

    assert result == CommandResult(
        ok=True,
        message=(
            "text_embedding: target=openai/text-embedding-3-small::api-key "
            'options={"dimensions": 512}'
        ),
        instance=instance,
    )
    assert calls == [
        {
            "method": "task_model.update",
            "params": {
                "model_tasks": {
                    "text_embedding": {
                        "target": "openai/text-embedding-3-small::api-key",
                        "options": {"dimensions": 512},
                    }
                }
            },
        }
    ]


def test_task_model_set_rejects_invalid_options_json(tmp_path: Path) -> None:
    instance = make_instance(tmp_path)

    result = task_model_management.task_model_set(
        instance, "text_embedding", "local/whisper", "{not json"
    )

    assert result.ok is False
    assert result.message.startswith("--options is not valid JSON:")


def test_task_model_set_rejects_non_object_options(tmp_path: Path) -> None:
    instance = make_instance(tmp_path)

    result = task_model_management.task_model_set(
        instance, "text_embedding", "local/whisper", '["a"]'
    )

    assert result == CommandResult(
        ok=False, message="--options must be a JSON object", instance=instance
    )


def test_task_model_clear_posts_empty_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)
    calls: list[dict[str, Any]] = []

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        calls.append(json)
        return httpx.Response(200, json={"ok": True, "result": {"model_tasks": {}}})

    monkeypatch.setattr(task_model_management.httpx, "post", fake_post)

    result = task_model_management.task_model_clear(instance, "image_generation")

    assert result == CommandResult(
        ok=True, message="cleared image_generation binding", instance=instance
    )
    assert calls == [
        {
            "method": "task_model.update",
            "params": {"model_tasks": {"image_generation": {"target": ""}}},
        }
    ]


def test_task_model_options_dumps_schema_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        assert json == {
            "method": "task_model.options",
            "params": {"task_type": "text_to_speech", "target": "openai/tts::api-key"},
        }
        return httpx.Response(
            200,
            json={"ok": True, "result": {"schema": {"fields": []}}},
        )

    monkeypatch.setattr(task_model_management.httpx, "post", fake_post)

    result = task_model_management.task_model_options(
        instance, "text_to_speech", "openai/tts::api-key"
    )

    assert result.ok is True
    assert result.message.splitlines() == ["{", '  "fields": []', "}"]


def test_task_model_options_uses_current_binding_when_target_is_omitted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        assert json == {
            "method": "task_model.options",
            "params": {"task_type": "text_to_speech"},
        }
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "schema": {
                        "configured_options": {"voice": "Harper"},
                        "effective_options": {
                            "response_format": "mp3",
                            "voice": "Harper",
                        },
                        "fields": [],
                    }
                },
            },
        )

    monkeypatch.setattr(task_model_management.httpx, "post", fake_post)

    result = task_model_management.task_model_options(instance, "text_to_speech", None)

    assert result.ok is True
    assert '"voice": "Harper"' in result.message


def test_task_model_set_option_posts_typed_patch_and_prints_saved_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        assert json == {
            "method": "task_model.patch_options",
            "params": {
                "task_type": "text_to_speech",
                "set": {"speed": 1.25},
            },
        }
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "model_tasks": {
                        "text_to_speech": {
                            "target": "openrouter/microsoft/mai-voice-2::api-key",
                            "options": {"voice": "Harper", "speed": 1.25},
                        }
                    }
                },
            },
        )

    monkeypatch.setattr(task_model_management.httpx, "post", fake_post)

    result = task_model_management.task_model_set_option(
        instance,
        "text_to_speech",
        "speed",
        "1.25",
    )

    assert result.ok is True
    assert result.message.endswith('options={"speed": 1.25, "voice": "Harper"}')


def test_task_model_unset_option_posts_patch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        assert json == {
            "method": "task_model.patch_options",
            "params": {"task_type": "text_to_speech", "unset": ["speed"]},
        }
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "model_tasks": {
                        "text_to_speech": {
                            "target": "openrouter/microsoft/mai-voice-2::api-key",
                            "options": {"voice": "Harper"},
                        }
                    }
                },
            },
        )

    monkeypatch.setattr(task_model_management.httpx, "post", fake_post)

    result = task_model_management.task_model_unset_option(
        instance,
        "text_to_speech",
        "speed",
    )

    assert result.ok is True
    assert result.message.endswith('options={"voice": "Harper"}')


def test_run_dispatches_task_model_list(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path, port=8765)

    def fake_resolve(*, host: str, port: int | None, data_dir: str | None) -> ServerInstance:
        return instance

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "result": {"model_tasks": {}}})

    monkeypatch.setattr(task_model_management.httpx, "post", fake_post)

    exit_code = cli_main.run(["task-model", "list", "--port", "8765"], resolve=fake_resolve)

    assert exit_code == 0
    assert capsys.readouterr().out.splitlines() == ["no task-model bindings configured"]
