"""Tests for complete system/runtime Model DB root selection."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from core.models.database import (
    MODEL_DATABASE_SCHEMA_VERSION,
    MODEL_DATABASE_SOURCE_RUNTIME,
    MODEL_DATABASE_SOURCE_SYSTEM,
    begin_runtime_model_database_refresh,
    begin_system_model_database_refresh,
    read_model_database_manifest,
    select_model_database_dir,
    write_model_database_manifest,
)
from core.models.models import ModelRegistry
from core.storage.layout import DataDirectoryLayout


def test_manifest_round_trips_refresh_provenance(tmp_path: Path) -> None:
    models_dir = tmp_path / "models"
    refreshed_at = datetime(2026, 7, 21, 12, 30, tzinfo=UTC)

    written = write_model_database_manifest(
        models_dir,
        source=MODEL_DATABASE_SOURCE_RUNTIME,
        refreshed_at=refreshed_at,
    )

    assert written.schema_version == MODEL_DATABASE_SCHEMA_VERSION
    assert read_model_database_manifest(models_dir) == written


def test_boolean_schema_version_is_not_accepted_as_version_one(tmp_path: Path) -> None:
    models_dir = tmp_path / "models"
    models_dir.mkdir()
    models_dir.joinpath("manifest.json").write_text(
        json.dumps(
            {
                "schema_version": True,
                "refreshed_at": "2026-07-21T12:30:00+00:00",
                "source": "runtime",
            }
        ),
        encoding="utf-8",
    )

    assert read_model_database_manifest(models_dir) is None


def test_newer_runtime_database_wins_as_a_complete_root(tmp_path: Path) -> None:
    resources_dir = tmp_path / "resources"
    system_models_dir = resources_dir / "models"
    runtime_models_dir = tmp_path / "data" / "models"
    write_model_database_manifest(
        system_models_dir,
        source=MODEL_DATABASE_SOURCE_SYSTEM,
        refreshed_at=datetime(2026, 7, 20, tzinfo=UTC),
    )
    write_model_database_manifest(
        runtime_models_dir,
        source=MODEL_DATABASE_SOURCE_RUNTIME,
        refreshed_at=datetime(2026, 7, 21, tzinfo=UTC),
    )

    assert select_model_database_dir(resources_dir, runtime_models_dir) == runtime_models_dir


def test_equal_refresh_time_prefers_system_database(tmp_path: Path) -> None:
    resources_dir = tmp_path / "resources"
    system_models_dir = resources_dir / "models"
    runtime_models_dir = tmp_path / "data" / "models"
    refreshed_at = datetime(2026, 7, 21, tzinfo=UTC)
    write_model_database_manifest(
        system_models_dir,
        source=MODEL_DATABASE_SOURCE_SYSTEM,
        refreshed_at=refreshed_at,
    )
    write_model_database_manifest(
        runtime_models_dir,
        source=MODEL_DATABASE_SOURCE_RUNTIME,
        refreshed_at=refreshed_at,
    )

    assert select_model_database_dir(resources_dir, runtime_models_dir) == system_models_dir


def test_incompatible_runtime_database_is_ignored(tmp_path: Path) -> None:
    resources_dir = tmp_path / "resources"
    system_models_dir = resources_dir / "models"
    runtime_models_dir = tmp_path / "data" / "models"
    system_models_dir.mkdir(parents=True)
    runtime_models_dir.mkdir(parents=True)
    runtime_models_dir.joinpath("manifest.json").write_text(
        json.dumps(
            {
                "schema_version": MODEL_DATABASE_SCHEMA_VERSION + 1,
                "refreshed_at": "2030-01-01T00:00:00+00:00",
                "source": "runtime",
            }
        ),
        encoding="utf-8",
    )

    assert select_model_database_dir(resources_dir, runtime_models_dir) == system_models_dir


def test_runtime_refresh_copies_and_publishes_every_model_file(tmp_path: Path) -> None:
    resources_dir = tmp_path / "resources"
    system_models_dir = resources_dir / "models"
    data_dir = tmp_path / "data"
    system_models_dir.mkdir(parents=True)
    system_models_dir.joinpath("openai.json").write_text("generated", encoding="utf-8")
    system_models_dir.joinpath("openai.overrides.json").write_text(
        "maintainer override", encoding="utf-8"
    )
    system_models_dir.joinpath("openai.raw.json").write_text("raw", encoding="utf-8")
    write_model_database_manifest(
        system_models_dir,
        source=MODEL_DATABASE_SOURCE_SYSTEM,
        refreshed_at=datetime(2026, 7, 21, tzinfo=UTC),
    )

    refresh = begin_runtime_model_database_refresh(resources_dir, data_dir)
    assert refresh.resources_dir.parent == DataDirectoryLayout(data_dir).atomic_temporary
    assert refresh.publish_temporary_dir == DataDirectoryLayout(data_dir).atomic_temporary
    refresh.commit()

    assert sorted(path.name for path in DataDirectoryLayout(data_dir).models.iterdir()) == [
        "manifest.json",
        "openai.json",
        "openai.overrides.json",
        "openai.raw.json",
    ]


def test_discarded_runtime_refresh_leaves_published_database_untouched(tmp_path: Path) -> None:
    resources_dir = tmp_path / "resources"
    data_dir = tmp_path / "data"
    runtime_models_dir = DataDirectoryLayout(data_dir).models
    runtime_models_dir.mkdir(parents=True)
    runtime_models_dir.joinpath("openai.json").write_text("published", encoding="utf-8")
    write_model_database_manifest(
        runtime_models_dir,
        source=MODEL_DATABASE_SOURCE_RUNTIME,
        refreshed_at=datetime(2026, 7, 21, tzinfo=UTC),
    )

    refresh = begin_runtime_model_database_refresh(resources_dir, data_dir)
    refresh.resources_dir.joinpath("models", "openai.json").write_text(
        "unpublished",
        encoding="utf-8",
    )
    refresh.discard()

    assert runtime_models_dir.joinpath("openai.json").read_text(encoding="utf-8") == "published"


def test_system_refresh_is_unpublished_until_complete_commit(tmp_path: Path) -> None:
    resources_dir = tmp_path / "resources"
    system_models_dir = resources_dir / "models"
    system_models_dir.mkdir(parents=True)
    system_models_dir.joinpath("openai.json").write_text("old", encoding="utf-8")

    refresh = begin_system_model_database_refresh(resources_dir)
    refresh.resources_dir.joinpath("models", "openai.json").write_text(
        "new",
        encoding="utf-8",
    )

    assert system_models_dir.joinpath("openai.json").read_text(encoding="utf-8") == "old"

    refresh.commit()

    assert system_models_dir.joinpath("openai.json").read_text(encoding="utf-8") == "new"
    manifest = read_model_database_manifest(system_models_dir)
    assert manifest is not None
    assert manifest.source == MODEL_DATABASE_SOURCE_SYSTEM


def test_registry_loads_only_the_newer_root_including_its_override(tmp_path: Path) -> None:
    resources_dir = tmp_path / "resources"
    system_models_dir = resources_dir / "models"
    runtime_models_dir = tmp_path / "data" / "models"
    _write_provider_database(system_models_dir, generated_name="System", override_name="System")
    _write_provider_database(runtime_models_dir, generated_name="Runtime", override_name="Runtime")
    system_models_dir.joinpath("system-only.json").write_text(
        _provider_payload("system-only", "system-model", "Must not leak"),
        encoding="utf-8",
    )
    write_model_database_manifest(
        system_models_dir,
        source=MODEL_DATABASE_SOURCE_SYSTEM,
        refreshed_at=datetime(2026, 7, 20, tzinfo=UTC),
    )
    write_model_database_manifest(
        runtime_models_dir,
        source=MODEL_DATABASE_SOURCE_RUNTIME,
        refreshed_at=datetime(2026, 7, 21, tzinfo=UTC),
    )

    registry = ModelRegistry.load(resources_dir, runtime_models_dir=runtime_models_dir)

    assert registry.get("openai", "gpt-test").name == "Runtime override"
    assert registry.list_for_provider("system-only") == []


def _write_provider_database(
    models_dir: Path,
    *,
    generated_name: str,
    override_name: str,
) -> None:
    models_dir.mkdir(parents=True, exist_ok=True)
    models_dir.joinpath("openai.json").write_text(
        _provider_payload("openai", "gpt-test", generated_name),
        encoding="utf-8",
    )
    models_dir.joinpath("openai.overrides.json").write_text(
        json.dumps({"models": {"gpt-test": {"name": f"{override_name} override"}}}),
        encoding="utf-8",
    )


def _provider_payload(provider_id: str, model_id: str, name: str) -> str:
    return json.dumps(
        {
            "provider_id": provider_id,
            "models": {
                model_id: {
                    "name": name,
                    "capabilities": {
                        "vision": False,
                        "tools": True,
                        "json_mode": False,
                        "reasoning": {"supported": False},
                    },
                    "context_window": 128000,
                    "max_output_tokens": 8192,
                }
            },
        }
    )
