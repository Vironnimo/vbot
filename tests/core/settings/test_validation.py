"""Tests for Settings-owned data-dir validation orchestration."""

from __future__ import annotations

import json
from pathlib import Path

from core.settings import validate_data_dir_config


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
