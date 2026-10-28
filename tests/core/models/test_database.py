"""Models: the system/runtime Model DB roots (manifest, per-catalog selection, refresh)."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from core.models.database import (
    MODEL_DATABASE_SCHEMA_VERSION,
    MODEL_DATABASE_SOURCE_RUNTIME,
    MODEL_DATABASE_SOURCE_SYSTEM,
    begin_runtime_model_database_refresh,
    begin_system_model_database_refresh,
    read_model_database_manifest,
    select_model_database_files,
    write_model_database_manifest,
)
from core.models.models import ModelRegistry
from core.storage.layout import DataDirectoryLayout

_JULY_19 = datetime(2026, 7, 19, tzinfo=UTC)
_JULY_20 = datetime(2026, 7, 20, tzinfo=UTC)
_JULY_21 = datetime(2026, 7, 21, tzinfo=UTC)


def test_manifest_round_trips_refresh_provenance(tmp_path: Path) -> None:
    models_dir = tmp_path / "models"

    written = write_model_database_manifest(
        models_dir,
        source=MODEL_DATABASE_SOURCE_RUNTIME,
        catalogs={"models.json": _JULY_20, "ollama.json": _JULY_21},
        refreshed_at=_JULY_21,
    )

    assert written.schema_version == MODEL_DATABASE_SCHEMA_VERSION
    assert read_model_database_manifest(models_dir) == written
    assert json.loads((models_dir / "manifest.json").read_text(encoding="utf-8")) == {
        "catalogs": {
            "models.json": "2026-07-20T00:00:00+00:00",
            "ollama.json": "2026-07-21T00:00:00+00:00",
        },
        "refreshed_at": "2026-07-21T00:00:00+00:00",
        "schema_version": MODEL_DATABASE_SCHEMA_VERSION,
        "source": "runtime",
    }


@pytest.mark.parametrize(
    "change",
    [
        pytest.param({"schema_version": True}, id="boolean-schema-version"),
        pytest.param({"schema_version": MODEL_DATABASE_SCHEMA_VERSION - 1}, id="older-schema"),
        pytest.param({"catalogs": None}, id="no-catalog-records"),
        pytest.param(
            {"catalogs": {"openai.overrides.json": "2026-07-21T00:00:00+00:00"}},
            id="hand-layer-record",
        ),
        pytest.param(
            {"catalogs": {"../openai.json": "2026-07-21T00:00:00+00:00"}}, id="path-record"
        ),
        pytest.param({"catalogs": {"openai.json": "2026-07-21T00:00:00"}}, id="naive-record"),
        pytest.param({"refreshed_at": "0001-01-01T00:00:00+01:00"}, id="root-time-underflows-utc"),
        pytest.param(
            {"catalogs": {"openai.json": "9999-12-31T23:59:59-01:00"}},
            id="catalog-time-overflows-utc",
        ),
    ],
)
def test_invalid_manifest_makes_the_root_incompatible(
    tmp_path: Path, change: dict[str, object]
) -> None:
    models_dir = tmp_path / "models"
    models_dir.mkdir()
    manifest = {
        "schema_version": MODEL_DATABASE_SCHEMA_VERSION,
        "refreshed_at": "2026-07-21T12:30:00+00:00",
        "source": "runtime",
        "catalogs": {"openai.json": "2026-07-21T12:30:00+00:00"},
    }
    models_dir.joinpath("manifest.json").write_text(json.dumps(manifest | change), encoding="utf-8")

    assert read_model_database_manifest(models_dir) is None


@pytest.mark.parametrize("runtime_compatible", [True, False])
def test_each_catalog_is_selected_from_the_root_that_fetched_it_last(
    tmp_path: Path, runtime_compatible: bool
) -> None:
    resources_dir = tmp_path / "resources"
    system_models_dir = resources_dir / "models"
    runtime_models_dir = tmp_path / "data" / "models"
    _write_catalogs(
        system_models_dir,
        ["models.json", "older.json", "newer.json", "seed.json", "equal.json"],
    )
    write_model_database_manifest(
        system_models_dir,
        source=MODEL_DATABASE_SOURCE_SYSTEM,
        catalogs={
            "models.json": _JULY_20,
            "older.json": _JULY_20,
            "newer.json": _JULY_21,
            "equal.json": _JULY_20,
        },
    )
    _write_catalogs(
        runtime_models_dir,
        ["models.json", "older.json", "newer.json", "seed.json", "equal.json", "custom.json"],
    )
    # A runtime file without a record is a stale copy, never a fetched catalog.
    _write_catalogs(runtime_models_dir, ["stale.json"])
    write_model_database_manifest(
        runtime_models_dir,
        source=MODEL_DATABASE_SOURCE_RUNTIME,
        catalogs={
            "models.json": _JULY_21,
            "older.json": _JULY_21,
            "newer.json": _JULY_20,
            "seed.json": _JULY_19,
            "equal.json": _JULY_20,
            "custom.json": _JULY_19,
        },
    )
    if not runtime_compatible:
        manifest_path = runtime_models_dir / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["schema_version"] = MODEL_DATABASE_SCHEMA_VERSION + 1
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    files = select_model_database_files(resources_dir, runtime_models_dir)

    runtime_catalogs = (
        {"models.json", "older.json", "seed.json", "custom.json"} if runtime_compatible else set()
    )
    expected = {
        name: (runtime_models_dir if name in runtime_catalogs else system_models_dir) / name
        for name in ["older.json", "newer.json", "seed.json", "equal.json"]
    }
    if runtime_compatible:
        expected["custom.json"] = runtime_models_dir / "custom.json"
    assert (
        files.canonical
        == (runtime_models_dir if runtime_compatible else system_models_dir) / "models.json"
    )
    assert files.providers == tuple(expected[name] for name in sorted(expected))
    assert files.overrides_dir == system_models_dir


@pytest.mark.parametrize("held_directory", [False, True])
def test_runtime_refresh_publishes_only_the_catalogs_this_installation_fetched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, held_directory: bool
) -> None:
    resources_dir = tmp_path / "resources"
    system_models_dir = resources_dir / "models"
    data_dir = tmp_path / "data"
    runtime_models_dir = DataDirectoryLayout(data_dir).models
    _write_catalogs(system_models_dir, ["models.json", "openai.json", "superseded.json"])
    system_models_dir.joinpath("openai.overrides.json").write_text("{}", encoding="utf-8")
    system_models_dir.joinpath("openai.raw.json").write_text("raw", encoding="utf-8")
    write_model_database_manifest(
        system_models_dir,
        source=MODEL_DATABASE_SOURCE_SYSTEM,
        catalogs={"models.json": _JULY_20, "openai.json": _JULY_20, "superseded.json": _JULY_21},
    )
    _write_catalogs(runtime_models_dir, ["ollama.json", "superseded.json", "stale.json"])
    write_model_database_manifest(
        runtime_models_dir,
        source=MODEL_DATABASE_SOURCE_RUNTIME,
        catalogs={"ollama.json": _JULY_21, "superseded.json": _JULY_19},
    )

    refresh = begin_runtime_model_database_refresh(resources_dir, data_dir)
    staged_models_dir = refresh.resources_dir / "models"
    assert refresh.resources_dir.parent == DataDirectoryLayout(data_dir).atomic_temporary
    assert refresh.publish_temporary_dir == DataDirectoryLayout(data_dir).atomic_temporary
    # The working copy is what Load sees: every selected catalog plus the bundled overrides.
    assert sorted(path.name for path in staged_models_dir.iterdir()) == [
        "models.json",
        "ollama.json",
        "openai.json",
        "openai.overrides.json",
        "superseded.json",
    ]
    assert staged_models_dir.joinpath("superseded.json").read_text(encoding="utf-8") == str(
        system_models_dir / "superseded.json"
    )
    _write_catalogs(staged_models_dir, ["lmstudio.json"])
    held: set[Path] = set()
    if held_directory:
        real_replace = os.replace

        def replace(source, target):
            if Path(source).is_dir() and source not in held:
                held.add(source)
                error = PermissionError("temporarily held directory")
                error.winerror = 32
                raise error
            real_replace(source, target)

        monkeypatch.setattr("core.utils.atomic.os.replace", replace)
        monkeypatch.setattr("core.utils.atomic._sleep", lambda _: None)
    manifest = refresh.commit(providers=["lmstudio"], canonical=True)
    assert len(held) == (2 if held_directory else 0)

    assert sorted(path.name for path in runtime_models_dir.iterdir()) == [
        "lmstudio.json",
        "manifest.json",
        "models.json",
        "ollama.json",
    ]
    assert read_model_database_manifest(runtime_models_dir) == manifest
    assert manifest.catalogs == {
        "lmstudio.json": manifest.refreshed_at,
        "models.json": manifest.refreshed_at,
        "ollama.json": _JULY_21,
    }


def test_runtime_refresh_stages_the_current_bundled_overrides(tmp_path: Path) -> None:
    resources_dir = tmp_path / "resources"
    system_models_dir = resources_dir / "models"
    data_dir = tmp_path / "data"
    runtime_models_dir = DataDirectoryLayout(data_dir).models
    system_models_dir.mkdir(parents=True)
    runtime_models_dir.mkdir(parents=True)
    system_models_dir.joinpath("openai.overrides.json").write_text(
        "current bundled override",
        encoding="utf-8",
    )
    runtime_models_dir.joinpath("openai.overrides.json").write_text(
        "stale runtime override",
        encoding="utf-8",
    )
    runtime_models_dir.joinpath("removed.overrides.json").write_text(
        "removed bundled override",
        encoding="utf-8",
    )
    write_model_database_manifest(runtime_models_dir, source=MODEL_DATABASE_SOURCE_RUNTIME)

    refresh = begin_runtime_model_database_refresh(resources_dir, data_dir)
    staged_models_dir = refresh.resources_dir / "models"

    assert (
        staged_models_dir.joinpath("openai.overrides.json").read_text(encoding="utf-8")
        == "current bundled override"
    )
    assert not staged_models_dir.joinpath("removed.overrides.json").exists()


def test_discarded_runtime_refresh_leaves_published_database_untouched(tmp_path: Path) -> None:
    resources_dir = tmp_path / "resources"
    data_dir = tmp_path / "data"
    runtime_models_dir = DataDirectoryLayout(data_dir).models
    runtime_models_dir.mkdir(parents=True)
    runtime_models_dir.joinpath("openai.json").write_text("published", encoding="utf-8")
    write_model_database_manifest(
        runtime_models_dir,
        source=MODEL_DATABASE_SOURCE_RUNTIME,
        catalogs={"openai.json": _JULY_21},
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
    instance_catalogs = ["ollama.json", "lmstudio.json", "custom.json"]
    _write_catalogs(
        system_models_dir, ["openai.json", "anthropic.json", "seed.json", *instance_catalogs]
    )
    system_models_dir.joinpath("openai.overrides.json").write_text("{}", encoding="utf-8")
    write_model_database_manifest(
        system_models_dir,
        source=MODEL_DATABASE_SOURCE_SYSTEM,
        catalogs={
            "openai.json": _JULY_20,
            "anthropic.json": _JULY_20,
            **dict.fromkeys(instance_catalogs, _JULY_20),
        },
    )

    refresh = begin_system_model_database_refresh(
        resources_dir, provider_ids={"openai", "anthropic", "seed"}
    )
    refresh.resources_dir.joinpath("models", "openai.json").write_text(
        "new",
        encoding="utf-8",
    )

    assert system_models_dir.joinpath("openai.json").read_text(encoding="utf-8") != "new"
    assert all((system_models_dir / name).exists() for name in instance_catalogs)

    manifest = refresh.commit(providers=["openai"])

    assert system_models_dir.joinpath("openai.json").read_text(encoding="utf-8") == "new"
    # The shipped root stays complete: hand layers and unrecorded seeds remain.
    assert sorted(path.name for path in system_models_dir.iterdir()) == [
        "anthropic.json",
        "manifest.json",
        "openai.json",
        "openai.overrides.json",
        "seed.json",
    ]
    assert read_model_database_manifest(system_models_dir) == manifest
    assert manifest.source == MODEL_DATABASE_SOURCE_SYSTEM
    assert manifest.catalogs == {"openai.json": manifest.refreshed_at, "anthropic.json": _JULY_20}


@pytest.mark.parametrize("unavailable", [None, "runtime-catalog", "system-catalog", "system-root"])
def test_load_combines_runtime_and_system_catalogs_under_bundled_overrides(
    tmp_path: Path, deny_access: Callable[[Path], None], unavailable: str | None
) -> None:
    resources_dir = tmp_path / "resources"
    system_models_dir = resources_dir / "models"
    data_dir = tmp_path / "data"
    runtime_models_dir = DataDirectoryLayout(data_dir).models
    _write_provider_database(system_models_dir, generated_name="System", override_name="System")
    _write_provider_database(runtime_models_dir, generated_name="Runtime", override_name="Runtime")
    for models_dir, provider_id, name in [
        (system_models_dir, "anthropic", "Shipped by an update"),
        (system_models_dir, "ollama", "Bundled seed"),
        (runtime_models_dir, "ollama", "Served locally"),
    ]:
        models_dir.joinpath(f"{provider_id}.json").write_text(
            _provider_payload(provider_id, "model", name), encoding="utf-8"
        )
    # A bundled override-only Provider needs no generated catalog in either root.
    system_models_dir.joinpath("hand-only.overrides.json").write_text(
        json.dumps(
            {
                "models": {
                    "manual-model": json.loads(
                        _provider_payload("hand-only", "manual-model", "Manual")
                    )["models"]["manual-model"]
                }
            }
        ),
        encoding="utf-8",
    )
    write_model_database_manifest(
        system_models_dir,
        source=MODEL_DATABASE_SOURCE_SYSTEM,
        catalogs={"openai.json": _JULY_19, "anthropic.json": _JULY_19},
    )
    write_model_database_manifest(
        runtime_models_dir,
        source=MODEL_DATABASE_SOURCE_RUNTIME,
        catalogs={"openai.json": _JULY_21, "ollama.json": _JULY_21},
    )

    if unavailable is not None:
        denied = {
            "runtime-catalog": runtime_models_dir / "ollama.json",
            "system-catalog": system_models_dir / "anthropic.json",
            "system-root": system_models_dir,
        }[unavailable]
        deny_access(denied)

    registry = ModelRegistry.load(resources_dir, runtime_models_dir=runtime_models_dir)

    assert registry.get("openai", "gpt-test").name == (
        "Runtime" if unavailable == "system-root" else "System override"
    )
    if unavailable in {"system-catalog", "system-root"}:
        assert registry.list_for_provider("anthropic") == []
    else:
        assert registry.get("anthropic", "model").name == "Shipped by an update"
    if unavailable == "runtime-catalog":
        assert registry.list_for_provider("ollama") == []
    else:
        assert registry.get("ollama", "model").name == "Served locally"
    if unavailable == "system-root":
        assert registry.list_for_provider("hand-only") == []
    else:
        assert registry.get("hand-only", "manual-model").name == "Manual"

    if unavailable is not None:
        # Refresh cannot publish a partial copy of the same unreadable data.
        with pytest.raises(PermissionError):
            begin_runtime_model_database_refresh(resources_dir, data_dir)


def _write_catalogs(models_dir: Path, names: list[str]) -> None:
    """Write placeholder catalogs whose content names the file they were written as."""

    models_dir.mkdir(parents=True, exist_ok=True)
    for name in names:
        models_dir.joinpath(name).write_text(str(models_dir / name), encoding="utf-8")


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
