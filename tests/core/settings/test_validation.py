"""Tests for Settings-owned data-dir validation orchestration."""

from __future__ import annotations

import json
from pathlib import Path

from core.settings import validate_data_dir_config, validate_settings_data


def test_validate_data_dir_config_delegates_project_files(tmp_path: Path) -> None:
    project_dir = tmp_path / "projects" / "vbot"
    project_dir.mkdir(parents=True)
    (project_dir / "project.json").write_text(
        json.dumps(
            {
                "project_id": "vbot",
                "display_name": "vBot",
                "cwd": "/srv/repos/vbot",
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
        json.dumps({"revision": 1, "agent_ids": ["main", "main"]}),
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
    jobs_path.write_text('[{"mode": "sometimes"}]', encoding="utf-8")

    reports = validate_data_dir_config(tmp_path)

    bootstrap_reports = [report for report in reports if report.file_path == jobs_path]
    assert len(bootstrap_reports) == 1
    assert bootstrap_reports[0].ok is False


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
