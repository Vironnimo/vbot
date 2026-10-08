"""Tests for raw ``settings.json`` validation and data-dir validation orchestration."""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pytest

from core.config_validation import JsonDiagnostic
from core.settings import (
    SettingsValidationError,
    load_runtime_settings_json,
    validate_data_dir_config,
    validate_settings_data,
    validate_settings_document,
    validate_settings_file,
)


def _diagnostics(items: Iterable[JsonDiagnostic]) -> list[tuple[str, str, str]]:
    return [(item.severity, item.path, item.message) for item in items]


_VALID_PROJECT = {
    "format_version": 1,
    "project_id": "vbot",
    "display_name": "vBot",
    "cwd": "/srv/repos/vbot",
    "allowed_tools": [],
    "created_at": "2026-06-18T10:00:00Z",
    "updated_at": "2026-06-18T10:00:00Z",
}


@pytest.mark.parametrize(
    ("relative_path", "content", "first_diagnostic"),
    [
        pytest.param("projects/vbot/project.json", json.dumps(_VALID_PROJECT), None, id="project"),
        pytest.param(
            "agents/order.json",
            json.dumps({"format_version": 1, "revision": 1, "agent_ids": ["main", "main"]}),
            ("$.agent_ids[1]", ""),
            id="agent-order",
        ),
        # The staging directory name never leaves the Agent directory.
        pytest.param(
            "agents/rename-pending.json",
            json.dumps(
                {
                    "format_version": 1,
                    "source_id": "main",
                    "target_id": "Main",
                    "staging_name": "../escape",
                }
            ),
            ("$.staging_name", ""),
            id="agent-rename",
        ),
        pytest.param(
            "bootstrap/jobs.json",
            '{"format_version": 1, "jobs": [{"mode": "sometimes"}]}',
            ("$.jobs[0].id", "is required"),
            id="bootstrap-jobs",
        ),
        # Undecodable bytes are a diagnostic, never an exception.
        pytest.param(
            "agents/main/agent.json",
            b'{"id":"main","name":"\xff"}',
            ("$", "not valid UTF-8"),
            id="non-utf8-agent",
        ),
    ],
)
def test_validate_data_dir_config_delegates_each_document_to_its_owner(
    tmp_path: Path,
    relative_path: str,
    content: str | bytes,
    first_diagnostic: tuple[str, str] | None,
) -> None:
    path = tmp_path / relative_path
    path.parent.mkdir(parents=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")

    reports = validate_data_dir_config(tmp_path)

    [report] = [report for report in reports if report.file_path == path]
    assert report.ok is (first_diagnostic is None)
    if first_diagnostic is not None:
        diagnostic = report.diagnostics[0]
        assert diagnostic.path == first_diagnostic[0]
        assert first_diagnostic[1] in diagnostic.message


_ATTACHMENT_METADATA: dict[str, object] = {
    "id": "att_000000000001",
    "filename": "notes.txt",
    "media_type": "text/plain",
    "size_bytes": 5,
    "stored_at": "2026-06-18T10:00:00+00:00",
}
_SPEECH_ARTIFACT_METADATA: dict[str, object] = {
    "id": "aud_000000000001",
    "filename": "aud_000000000001.mp3",
    "media_type": "audio/mpeg",
    "size_bytes": 3,
}

# Every durable JSON document the doctor covers, in its pre-Generation-1 form and
# in a minimal current form.
_DATA_DIR_DOCUMENTS: dict[str, tuple[str, dict[str, object]]] = {
    "settings.json": ("{}", {}),
    "agents/order.json": ("{}", {"revision": 1, "agent_ids": []}),
    "agents/rename-pending.json": ("{}", {"source_id": "main", "target_id": "renamed"}),
    "agents/main/prompts/layout.json": ("[]", {"entries": []}),
    "agents/main/librarian.json": ("{}", {}),
    "prompts/layout.json": ("[]", {"entries": []}),
    "cron/jobs.json": ("[]", {"jobs": []}),
    "bootstrap/jobs.json": ("[]", {"jobs": []}),
    "calendar/events.json": ("[]", {"events": []}),
    "skills/policy.json": ('{"version": 2}', {}),
    "terminals/launch-history.json": ('{"version": 1, "entries": []}', {"entries": []}),
    "terminals/groups.json": ('{"version": 1, "groups": []}', {"groups": []}),
    "oauth/github-copilot-oauth.json": ('{"access_token": "token"}', {"access_token": "token"}),
    "extension-data/mcp/connections.json": ("[]", {"connections": []}),
    "artifacts/attachments/att_000000000001.json": (
        json.dumps(_ATTACHMENT_METADATA),
        _ATTACHMENT_METADATA,
    ),
    "artifacts/speech/aud_000000000001.json": (
        json.dumps(_SPEECH_ARTIFACT_METADATA),
        _SPEECH_ARTIFACT_METADATA,
    ),
}


def _write_data_dir_documents(root: Path, *, current: bool) -> None:
    for relative_path, (legacy, fields) in _DATA_DIR_DOCUMENTS.items():
        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps({"format_version": 1, **fields}) if current else legacy
        path.write_text(text, encoding="utf-8")


def test_validate_data_dir_config_covers_every_json_document(tmp_path: Path) -> None:
    _write_data_dir_documents(tmp_path, current=True)

    reports = validate_data_dir_config(tmp_path)

    reported = {report.file_path.relative_to(tmp_path).as_posix() for report in reports}
    assert reported == set(_DATA_DIR_DOCUMENTS)
    assert [
        (report.file_path, report.diagnostics) for report in reports if report.diagnostics
    ] == []


# ``[]`` is unhashable; ``None`` is the missing value.
@pytest.mark.parametrize("media_type", [[], None])
def test_validate_data_dir_config_reports_invalid_attachment_media_type(
    tmp_path: Path, media_type: object
) -> None:
    _write_data_dir_documents(tmp_path, current=True)
    path = tmp_path / "artifacts" / "attachments" / "att_000000000001.json"
    payload = {"format_version": 1, **_ATTACHMENT_METADATA, "media_type": media_type}
    original = json.dumps(payload)
    path.write_text(original, encoding="utf-8")

    reports = validate_data_dir_config(tmp_path)

    assert len(reports) == len(_DATA_DIR_DOCUMENTS)
    invalid = [report for report in reports if not report.ok]
    assert len(invalid) == 1
    assert invalid[0].file_path == path
    assert [(item.severity, item.path) for item in invalid[0].diagnostics] == [
        ("error", "$.media_type")
    ]
    assert path.read_text(encoding="utf-8") == original


def test_validate_data_dir_config_refuses_documents_before_generation_1(tmp_path: Path) -> None:
    _write_data_dir_documents(tmp_path, current=False)

    reports = validate_data_dir_config(tmp_path)

    assert len(reports) == len(_DATA_DIR_DOCUMENTS)
    assert [report.file_path for report in reports if report.ok] == []


@pytest.mark.parametrize(
    ("content", "ok", "diagnostics"),
    [
        pytest.param(None, True, [], id="missing-file"),
        pytest.param(
            "{",
            False,
            [
                (
                    "error",
                    "$",
                    "Invalid JSON: Expecting property name enclosed in double quotes at line 1 "
                    "column 2",
                )
            ],
            id="invalid-json",
        ),
        pytest.param("[]", False, [("error", "$", "Expected a JSON object, got list")], id="array"),
        # Unknown fields warn for forward compatibility; the file stays valid.
        pytest.param(
            json.dumps({"format_version": 1, "debug": {"enabled": True, "extra": 1}}),
            True,
            [("warning", "$.debug.extra", "unknown debug field: extra")],
            id="unknown-field-warns",
        ),
    ],
)
def test_validate_settings_file_reports_the_document(
    tmp_path: Path, content: str | None, ok: bool, diagnostics: list[tuple[str, str, str]]
) -> None:
    path = tmp_path / "settings.json"
    if content is not None:
        path.write_text(content, encoding="utf-8")

    report = validate_settings_file(path)

    assert (report.ok, report.exists) == (ok, content is not None)
    assert _diagnostics(report.diagnostics) == diagnostics


def test_validate_settings_file_accepts_every_known_section(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    finite_values = [None, True, -2, 1.25, {"number": 1e300}]
    settings_path.write_text(
        json.dumps(
            {
                "format_version": 1,
                "server_port": 8500,
                "keep_awake": True,
                "timezone": "Europe/Berlin",
                "appearance": {
                    "language": "en",
                    "chat_width": "wide",
                    "chat_working_mode": "compact",
                },
                "skill_directories": ["~/skills"],
                "extension_directories": ["C:/vbot/extensions"],
                "attachment_max_size_bytes": 1024,
                "speech_upload_max_size_bytes": 2048,
                "speech": {
                    "transcription_audio": {
                        "profile": "custom",
                        "format": "flac",
                        "sample_rate_hz": 24_000,
                    }
                },
                "max_subagent_depth": 4,
                "max_active_subagents": 8,
                "compaction": {
                    "enabled": True,
                    "trigger": {"type": "context_ratio", "threshold": 0.8},
                    "strategy": {
                        "type": "summary_tail",
                        "tail_tokens": 15_000,
                        "summary_model": None,
                    },
                },
                "recall": {"backend": "sqlite_fts"},
                "reflection": {
                    "enabled": True,
                    "memory_turn_interval": 10,
                    "skill_model_step_interval": 25,
                },
                "librarian": {
                    "enabled": True,
                    "interval_days": 7,
                    "archive_after_days": 90,
                    "consolidate": False,
                },
                "extensions": {
                    "disabled": ["legacy-ext"],
                    "config": {
                        "weather": {"api_key": "x", "units": "metric", "nested": finite_values}
                    },
                },
                "web_search": {
                    "provider": "searxng",
                    "default_count": 12,
                    "searxng": {"base_url": "http://localhost:8888"},
                },
                "defaults": {
                    "agent": {
                        "model": "openai/gpt-5.2",
                        "fallback_models": [],
                        "temperature": 0.7,
                        "thinking_effort": "medium",
                    }
                },
                "model_tasks": {
                    "speech_to_text": {
                        "target": "openrouter/openai/gpt-4o-transcribe::api-key",
                        "options": {"language": "auto", "nested": finite_values},
                    }
                },
                "session_titles": {"enabled": False, "model": ""},
                "notifications": {"run_completed": False, "server_stopped": True},
                "local_models": {"context_windows": {"ollama/m": 16384}},
                "debug": {"enabled": True, "trace_limit": 500},
                "archive": {"retention_days": None},
                # Custom Provider records hold secret-free Model facts.
                "providers": {
                    "custom": {
                        "local-ai": {
                            "name": "Local AI",
                            "adapter": "openai_compatible",
                            "base_url": "http://127.0.0.1:8080/v1",
                            "auth": "none",
                            "models_endpoint": "/models",
                            "defaults": {"nested": finite_values},
                            "models": {
                                "chat-model": {
                                    "capabilities": {
                                        "tools": True,
                                        "input_modalities": ["text"],
                                        "output_modalities": ["text"],
                                        "task_options": {"nested": finite_values},
                                    }
                                }
                            },
                            "wire": {
                                "defaults": {"reasoning": {"dialect": "thinking_toggle"}},
                                "models": {
                                    "chat-model": {"set": {"replay": {"strip_when_off": True}}}
                                },
                            },
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    report = validate_settings_file(settings_path)

    assert (report.ok, report.exists, report.diagnostics) == (True, True, ())


@pytest.mark.parametrize(
    ("data", "diagnostics"),
    [
        pytest.param(
            {
                "server_port": 70000,
                "skill_directories": ["relative/path"],
                "attachment_max_size_bytes": 0,
                "speech_upload_max_size_bytes": 0,
                "compaction": {
                    "enabled": True,
                    "trigger": {"type": "context_ratio", "threshold": 2},
                    "strategy": {"type": "summary_tail", "tail_tokens": False},
                },
                "defaults": {"agent": {"temperature": "warm", "unknown": True}},
                "web_search": {
                    "provider": "unknown",
                    "default_count": 25,
                    "searxng": {"base_url": ""},
                },
                "model_tasks": {"speech_to_text": {"target": "", "options": []}},
                "typo": True,
            },
            [
                ("warning", "$.typo", "unknown settings key: typo"),
                ("error", "$.server_port", "must be between 1 and 65535"),
                ("error", "$.skill_directories[0]", "must be an absolute or home-relative path"),
                ("error", "$.attachment_max_size_bytes", "must be a positive integer"),
                ("error", "$.speech_upload_max_size_bytes", "must be a positive integer"),
                ("error", "$.compaction.trigger.threshold", "must be in (0, 1]"),
                ("error", "$.compaction.strategy.tail_tokens", "must be a positive integer"),
                ("warning", "$.defaults.agent.unknown", "unknown defaults.agent setting: unknown"),
                ("error", "$.defaults.agent.temperature", "must be a number"),
                (
                    "error",
                    "$.web_search.provider",
                    "must be one of: brave, duckduckgo, exa, firecrawl, "
                    "perplexity, searxng, serper, tavily",
                ),
                ("error", "$.web_search.default_count", "must be an integer between 1 and 20"),
                ("error", "$.web_search.searxng.base_url", "must be a non-empty string"),
                ("error", "$.model_tasks.speech_to_text.target", "must be a non-empty string"),
                ("error", "$.model_tasks.speech_to_text.options", "must be an object"),
            ],
            id="every-invalid-top-level-field-in-order",
        ),
        pytest.param(
            {"appearance": {"language": "en", "chat_width": "huge"}},
            [
                (
                    "error",
                    "$.appearance.chat_width",
                    "unsupported chat width; supported: comfortable, full, wide",
                )
            ],
            id="chat-width",
        ),
        pytest.param(
            {"appearance": {"language": "en", "chat_working_mode": "dense"}},
            [
                (
                    "error",
                    "$.appearance.chat_working_mode",
                    "unsupported chat working mode; supported: compact, normal",
                )
            ],
            id="chat-working-mode",
        ),
        pytest.param(
            {"recall": {"backend": "SQLite FTS"}},
            [("error", "$.recall.backend", "must use lowercase snake_case")],
            id="recall-backend",
        ),
        pytest.param(
            {"extensions": []},
            [("error", "$.extensions", "must be an object")],
            id="extensions-not-an-object",
        ),
        pytest.param(
            {
                "extensions": {
                    "disabled": ["ok", "", 5],
                    "config": {"good": {}, "bad": ["x"]},
                    "weird": True,
                }
            },
            [
                ("warning", "$.extensions.weird", "unknown extensions field: weird"),
                ("error", "$.extensions.disabled[1]", "must be a non-empty string"),
                ("error", "$.extensions.disabled[2]", "must be a non-empty string"),
                ("error", "$.extensions.config.bad", "must be an object"),
            ],
            id="extensions-fields",
        ),
        pytest.param(
            {"extensions": {"disabled": "solo"}},
            [("error", "$.extensions.disabled", "must be a list")],
            id="extensions-disabled-not-a-list",
        ),
        pytest.param(
            {"debug": []}, [("error", "$.debug", "must be an object")], id="debug-not-an-object"
        ),
        pytest.param(
            {"debug": {"enabled": "yes", "trace_limit": True}},
            [
                ("error", "$.debug.enabled", "must be a boolean"),
                ("error", "$.debug.trace_limit", "must be a positive integer (1-500)"),
            ],
            id="debug-types",
        ),
        pytest.param(
            {"debug": {"trace_limit": 0}},
            [("error", "$.debug.trace_limit", "must be at least 1")],
            id="trace-limit-below-range",
        ),
        pytest.param(
            {"debug": {"trace_limit": 501}},
            [("error", "$.debug.trace_limit", "must be at most 500")],
            id="trace-limit-above-range",
        ),
        pytest.param(
            {"archive": {"retention_days": 3651, "extra": 1}},
            [
                ("warning", "$.archive.extra", "unknown archive field: extra"),
                (
                    "error",
                    "$.archive.retention_days",
                    "must be an integer from 1 to 3650, "
                    "or null to keep archived items until they are deleted",
                ),
            ],
            id="archive-retention-out-of-range",
        ),
        pytest.param(
            {"reflection": []},
            [("error", "$.reflection", "must be an object")],
            id="reflection-not-an-object",
        ),
        # Omitted intervals keep their defaults.
        pytest.param({"reflection": {"enabled": False}}, [], id="partial-reflection"),
        pytest.param(
            {
                "reflection": {
                    "enabled": "yes",
                    "memory_turn_interval": "five",
                    "skill_model_step_interval": 0,
                    "extra": 1,
                }
            },
            [
                ("warning", "$.reflection.extra", "unknown reflection field: extra"),
                ("error", "$.reflection.enabled", "must be a boolean"),
                ("error", "$.reflection.memory_turn_interval", "must be a positive integer"),
                ("error", "$.reflection.skill_model_step_interval", "must be at least 1"),
            ],
            id="reflection-fields",
        ),
        pytest.param(
            {
                "librarian": {
                    "enabled": 1,
                    "consolidate": "no",
                    "interval_days": "7",
                    "archive_after_days": 0,
                    "model": 5,
                    "extra": 1,
                }
            },
            [
                ("warning", "$.librarian.extra", "unknown librarian field: extra"),
                # An earlier vBot's Librarian Model is explained, never an error.
                (
                    "warning",
                    "$.librarian.model",
                    "no longer used: the Librarian is an Agent with its own Model now; set it "
                    "with: vbot agent update librarian --model <provider/model>",
                ),
                ("error", "$.librarian.enabled", "must be a boolean"),
                ("error", "$.librarian.consolidate", "must be a boolean"),
                ("error", "$.librarian.interval_days", "must be a positive integer"),
                ("error", "$.librarian.archive_after_days", "must be at least 1"),
            ],
            id="librarian-fields",
        ),
        pytest.param(
            {"librarian": {"interval_days": 3651}},
            [("error", "$.librarian.interval_days", "must be at most 3650")],
            id="librarian-days-above-maximum",
        ),
        pytest.param(
            {"local_models": []},
            [("error", "$.local_models", "must be an object")],
            id="local-models-not-an-object",
        ),
        pytest.param(
            {"local_models": {"context_windows": 42}},
            [("error", "$.local_models.context_windows", "must be an object")],
            id="context-windows-not-an-object",
        ),
        pytest.param(
            {
                "local_models": {
                    "context_windows": {"no-slash": 4096, "ollama/m": 0},
                    "extra": 1,
                }
            },
            [
                ("warning", "$.local_models.extra", "unknown local_models field: extra"),
                (
                    "error",
                    "$.local_models.context_windows['no-slash']",
                    "key must be a '<provider>/<model_id>' string",
                ),
                (
                    "error",
                    "$.local_models.context_windows['ollama/m']",
                    "must be a positive integer",
                ),
            ],
            id="context-window-entries",
        ),
        pytest.param(
            {"session_titles": {"enabled": "yes", "model": 7, "extra": True}},
            [
                ("warning", "$.session_titles.extra", "unknown session_titles field: extra"),
                ("error", "$.session_titles.enabled", "must be a boolean"),
                ("error", "$.session_titles.model", "must be a string"),
            ],
            id="session-titles",
        ),
        pytest.param(
            {"keep_awake": "yes"}, [("error", "$.keep_awake", "must be a boolean")], id="keep-awake"
        ),
        pytest.param(
            {"notifications": {"run_failed": "yes", "sound": True}},
            [
                ("warning", "$.notifications.sound", "unknown notifications field: sound"),
                ("error", "$.notifications.run_failed", "must be a boolean"),
            ],
            id="notifications",
        ),
        pytest.param(
            {"timezone": "Berlin"},
            [("error", "$.timezone", "is not a known IANA timezone")],
            id="timezone",
        ),
        # Live voice is a Task Model binding; its old opt-in is only an unknown key.
        pytest.param(
            {"live_voice": {"enabled": True}},
            [("warning", "$.live_voice", "unknown settings key: live_voice")],
            id="removed-live-voice",
        ),
        # The per-turn Sub-Agent limit and the nested Sub-Agent timeout were removed.
        pytest.param(
            {"max_subagents_per_turn": 8, "subagent_timeout_minutes": 60},
            [
                (
                    "warning",
                    "$.max_subagents_per_turn",
                    "unknown settings key: max_subagents_per_turn",
                ),
                (
                    "warning",
                    "$.subagent_timeout_minutes",
                    "unknown settings key: subagent_timeout_minutes",
                ),
            ],
            id="removed-subagent-limits",
        ),
        # A Custom Provider record never carries a secret, even inside its URL.
        pytest.param(
            {
                "providers": {
                    "custom": {
                        "local-ai": {
                            "name": "Local AI",
                            "adapter": "openai_compatible",
                            "base_url": "https://user:secret@example.test/v1",
                            "auth": "api_key",
                            "api_key": "must-not-live-here",
                        }
                    }
                }
            },
            [
                (
                    "warning",
                    "$.providers.custom['local-ai'].api_key",
                    "unknown custom provider field: api_key",
                ),
                (
                    "error",
                    "$.providers.custom['local-ai']",
                    "local-ai.base_url must be an absolute HTTP(S) URL without credentials, "
                    "query, or fragment",
                ),
            ],
            id="custom-provider-secrets",
        ),
        # The Provider ignores invalid wire block entries; the record stays valid.
        pytest.param(
            {
                "providers": {
                    "custom": {
                        "local-ai": {
                            "name": "Local AI",
                            "adapter": "openai_compatible",
                            "base_url": "http://127.0.0.1:8080/v1",
                            "wire": "thinking_toggle",
                        },
                        "other-ai": {
                            "name": "Other AI",
                            "adapter": "openai_compatible",
                            "base_url": "http://127.0.0.1:8081/v1",
                            "wire": {"connections": {"api-key": {}}},
                        },
                    }
                }
            },
            [
                (
                    "warning",
                    "$.providers.custom['local-ai'].wire",
                    "wire: expected a JSON object, ignoring the block",
                ),
                (
                    "warning",
                    "$.providers.custom['other-ai'].wire",
                    "wire.connections.api-key: unknown Connection (this Provider has default), "
                    "ignoring it",
                ),
            ],
            id="custom-provider-wire-issues-warn",
        ),
        pytest.param(
            {
                "web_fetch": {"provider": "direct", "future_mode": "x"},
                "model_tasks": {"future_task": {"target": "a/b::c"}},
                "defaults": {"future_section": {}, "agent": {"future_default": 1}},
                "providers": {
                    "openrouter": {"routing": {"default": {"mode": "automatic", "future": 1}}}
                },
            },
            [
                (
                    "warning",
                    "$.defaults.future_section",
                    "unknown defaults section: future_section",
                ),
                (
                    "warning",
                    "$.defaults.agent.future_default",
                    "unknown defaults.agent setting: future_default",
                ),
                ("warning", "$.web_fetch.future_mode", "unknown web_fetch field: future_mode"),
                ("warning", "$.model_tasks.future_task", "unknown model task type: future_task"),
                (
                    "warning",
                    "$.providers.openrouter.routing.default.future",
                    "unknown routing field: future",
                ),
            ],
            id="unknown-fields-below-strict-sections-warn",
        ),
    ],
)
def test_settings_data_reports_each_invalid_field(
    data: dict[str, Any], diagnostics: list[tuple[str, str, str]]
) -> None:
    assert _diagnostics(validate_settings_data(data)) == diagnostics


@pytest.mark.parametrize(
    ("document", "message"),
    [
        ({"keep_awake": True}, "is required"),
        ({"format_version": 2, "keep_awake": True}, "written by a newer vBot"),
    ],
)
def test_settings_document_requires_the_current_format_version(
    document: dict[str, object], message: str
) -> None:
    diagnostics = validate_settings_document(document)

    assert [(item.severity, item.path) for item in diagnostics] == [("error", "$.format_version")]
    assert message in diagnostics[0].message


def test_runtime_settings_refuse_a_newer_format_version(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"format_version": 2, "keep_awake": True}), encoding="utf-8")

    with pytest.raises(SettingsValidationError, match="written by a newer vBot"):
        load_runtime_settings_json(path)


def test_runtime_settings_leave_out_unknown_fields_and_the_version(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps(
            {
                "format_version": 1,
                "future": True,
                "web_fetch": {"provider": "direct", "future_mode": "x"},
                "model_tasks": {"future_task": {"target": "a/b::c"}},
            }
        ),
        encoding="utf-8",
    )

    settings, ignored = load_runtime_settings_json(path)

    assert settings == {"web_fetch": {"provider": "direct"}, "model_tasks": {}}
    assert ignored == ()
