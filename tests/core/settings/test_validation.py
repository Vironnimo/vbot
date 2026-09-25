"""Tests for Settings-owned data-dir validation orchestration."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.settings import (
    SettingsValidationError,
    load_runtime_settings_json,
    validate_data_dir_config,
    validate_settings_data,
    validate_settings_document,
)


def test_validate_data_dir_config_delegates_project_files(tmp_path: Path) -> None:
    project_dir = tmp_path / "projects" / "vbot"
    project_dir.mkdir(parents=True)
    (project_dir / "project.json").write_text(
        json.dumps(
            {
                "format_version": 1,
                "project_id": "vbot",
                "display_name": "vBot",
                "cwd": "/srv/repos/vbot",
                "allowed_tools": [],
                "created_at": "2026-06-18T10:00:00Z",
                "updated_at": "2026-06-18T10:00:00Z",
            }
        ),
        encoding="utf-8",
    )

    reports = validate_data_dir_config(tmp_path)

    project_reports = [report for report in reports if report.file_path.name == "project.json"]
    assert len(project_reports) == 1
    assert project_reports[0].ok


def test_validate_data_dir_config_delegates_agent_order_file(tmp_path: Path) -> None:
    order_path = tmp_path / "agents" / "order.json"
    order_path.parent.mkdir(parents=True)
    order_path.write_text(
        json.dumps({"format_version": 1, "revision": 1, "agent_ids": ["main", "main"]}),
        encoding="utf-8",
    )

    reports = validate_data_dir_config(tmp_path)

    order_reports = [report for report in reports if report.file_path == order_path]
    assert len(order_reports) == 1
    assert order_reports[0].ok is False
    assert order_reports[0].diagnostics[0].path == "$.agent_ids[1]"


def test_validate_data_dir_config_delegates_bootstrap_jobs(tmp_path: Path) -> None:
    jobs_path = tmp_path / "bootstrap" / "jobs.json"
    jobs_path.parent.mkdir(parents=True)
    jobs_path.write_text('{"format_version": 1, "jobs": [{"mode": "sometimes"}]}', encoding="utf-8")

    reports = validate_data_dir_config(tmp_path)

    bootstrap_reports = [report for report in reports if report.file_path == jobs_path]
    assert len(bootstrap_reports) == 1
    assert bootstrap_reports[0].ok is False


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
    "agents/main/prompts/layout.json": ("[]", {"entries": []}),
    "prompts/layout.json": ("[]", {"entries": []}),
    "cron/jobs.json": ("[]", {"jobs": []}),
    "bootstrap/jobs.json": ("[]", {"jobs": []}),
    "calendar/events.json": ("[]", {"events": []}),
    "calendar/actions.json": (
        '{"actions": [], "executions": {}}',
        {"actions": [], "executions": {}},
    ),
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


@pytest.mark.parametrize("media_type", [[], {}, None, 1, True])
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


def test_validate_data_dir_config_reports_non_utf8_json_without_raising(tmp_path: Path) -> None:
    agent_path = tmp_path / "agents" / "main" / "agent.json"
    agent_path.parent.mkdir(parents=True)
    agent_path.write_bytes(b'{"id":"main","name":"\xff"}')

    reports = validate_data_dir_config(tmp_path)

    agent_report = next(report for report in reports if report.file_path == agent_path)
    assert agent_report.ok is False
    assert agent_report.diagnostics[0].path == "$"
    assert "not valid UTF-8" in agent_report.diagnostics[0].message


def test_validate_custom_provider_accepts_secret_free_model_facts() -> None:
    diagnostics = validate_settings_data(
        {
            "providers": {
                "custom": {
                    "local-ai": {
                        "name": "Local AI",
                        "adapter": "openai_compatible",
                        "base_url": "http://127.0.0.1:8080/v1",
                        "auth": "none",
                        "models_endpoint": "/models",
                        "models": {
                            "chat-model": {
                                "capabilities": {
                                    "tools": True,
                                    "input_modalities": ["text"],
                                    "output_modalities": ["text"],
                                }
                            }
                        },
                    }
                }
            }
        }
    )

    assert [item for item in diagnostics if item.severity == "error"] == []


def test_validate_custom_provider_rejects_secret_and_invalid_endpoint() -> None:
    diagnostics = validate_settings_data(
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
        }
    )

    errors = [item for item in diagnostics if item.severity == "error"]
    assert len(errors) == 1
    assert errors[0].path == "$.providers.custom['local-ai']"


def test_validate_settings_data_accepts_boolean_keep_awake() -> None:
    assert validate_settings_data({"keep_awake": True}) == []
    assert validate_settings_data({"keep_awake": False}) == []


def test_validate_settings_data_accepts_missing_keep_awake() -> None:
    assert validate_settings_data({}) == []


def test_validate_settings_data_rejects_non_boolean_keep_awake() -> None:
    diagnostics = validate_settings_data({"keep_awake": "yes"})
    errors = [diagnostic for diagnostic in diagnostics if diagnostic.severity == "error"]

    assert len(errors) == 1
    assert errors[0].path == "$.keep_awake"
    assert errors[0].message == "must be a boolean"


def test_validate_settings_data_accepts_iana_timezone() -> None:
    assert validate_settings_data({"timezone": "Europe/Berlin"}) == []


def test_validate_settings_data_rejects_unknown_timezone() -> None:
    diagnostics = validate_settings_data({"timezone": "Berlin"})
    errors = [diagnostic for diagnostic in diagnostics if diagnostic.severity == "error"]

    assert len(errors) == 1
    assert errors[0].path == "$.timezone"
    assert errors[0].message == "is not a known IANA timezone"


def test_removed_live_voice_section_is_an_unknown_key() -> None:
    """Live voice is configured as a Task Model; its old opt-in is not a Setting."""

    diagnostics = validate_settings_data({"live_voice": {"enabled": True}})

    assert [(item.path, item.severity) for item in diagnostics] == [("$.live_voice", "warning")]


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


def test_unknown_fields_below_strict_sections_are_warnings() -> None:
    diagnostics = validate_settings_data(
        {
            "web_fetch": {"provider": "direct", "future_mode": "x"},
            "model_tasks": {"future_task": {"target": "a/b::c"}},
            "defaults": {"future_section": {}, "agent": {"future_default": 1}},
            "providers": {
                "openrouter": {"routing": {"default": {"mode": "automatic", "future": 1}}}
            },
        }
    )

    assert sorted((item.severity, item.path) for item in diagnostics) == [
        ("warning", "$.defaults.agent.future_default"),
        ("warning", "$.defaults.future_section"),
        ("warning", "$.model_tasks.future_task"),
        ("warning", "$.providers.openrouter.routing.default.future"),
        ("warning", "$.web_fetch.future_mode"),
    ]
