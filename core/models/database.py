"""Whole-root storage contract for the bundled and runtime Model DBs.

Each Model DB is a complete ``models/`` directory: generated canonical and
Provider projections, raw inspection dumps, and a snapshot of every bundled
``*.overrides.json`` input. Load selects the newer schema-compatible generated
catalog root as a whole, while the current bundled overrides remain the
authoritative hand layer even when the runtime catalog is newer.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from core.storage.layout import DataDirectoryLayout
from core.utils.atomic import atomic_write_text

MODEL_DATABASE_DIRECTORY_NAME = "models"
MODEL_DATABASE_MANIFEST_FILE_NAME = "manifest.json"
MODEL_DATABASE_SCHEMA_VERSION = 1
MODEL_DATABASE_SOURCE_RUNTIME = "runtime"
MODEL_DATABASE_SOURCE_SYSTEM = "system"
_MODEL_DATABASE_SOURCES = frozenset({MODEL_DATABASE_SOURCE_RUNTIME, MODEL_DATABASE_SOURCE_SYSTEM})


@dataclass(frozen=True)
class ModelDatabaseManifest:
    """Refresh provenance used to select one complete Model DB root."""

    schema_version: int
    refreshed_at: datetime
    source: str


@dataclass(frozen=True)
class ModelDatabaseRefresh:
    """Isolated working copy for one complete Model DB refresh."""

    resources_dir: Path
    target_models_dir: Path
    source: str
    publish_temporary_dir: Path | None = None

    def commit(self) -> ModelDatabaseManifest:
        """Stamp and atomically publish the complete refreshed root."""

        working_models_dir = self.resources_dir / MODEL_DATABASE_DIRECTORY_NAME
        manifest = write_model_database_manifest(
            working_models_dir,
            source=self.source,
        )
        _replace_directory(
            working_models_dir,
            self.target_models_dir,
            temporary_dir=self.publish_temporary_dir,
        )
        self.discard()
        return manifest

    def discard(self) -> None:
        """Best-effort removal of the unpublished working copy."""

        shutil.rmtree(self.resources_dir, ignore_errors=True)


def read_model_database_manifest(models_dir: Path) -> ModelDatabaseManifest | None:
    """Return a valid manifest, or ``None`` for a missing/incompatible root."""

    manifest_path = models_dir / MODEL_DATABASE_MANIFEST_FILE_NAME
    if not manifest_path.is_file():
        return None
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    schema_version = data.get("schema_version")
    refreshed_at_raw = data.get("refreshed_at")
    source = data.get("source")
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != MODEL_DATABASE_SCHEMA_VERSION
    ):
        return None
    if not isinstance(refreshed_at_raw, str) or not isinstance(source, str):
        return None
    if source not in _MODEL_DATABASE_SOURCES:
        return None
    try:
        refreshed_at = datetime.fromisoformat(refreshed_at_raw)
    except ValueError:
        return None
    if refreshed_at.tzinfo is None:
        return None
    return ModelDatabaseManifest(
        schema_version=schema_version,
        refreshed_at=refreshed_at.astimezone(UTC),
        source=source,
    )


def write_model_database_manifest(
    models_dir: Path,
    *,
    source: str,
    refreshed_at: datetime | None = None,
) -> ModelDatabaseManifest:
    """Atomically stamp one complete Model DB after a successful refresh."""

    if source not in _MODEL_DATABASE_SOURCES:
        raise ValueError(f"Unsupported Model DB source: {source}")
    timestamp = refreshed_at or datetime.now(UTC)
    if timestamp.tzinfo is None:
        raise ValueError("Model DB refresh timestamp must include a timezone")
    manifest = ModelDatabaseManifest(
        schema_version=MODEL_DATABASE_SCHEMA_VERSION,
        refreshed_at=timestamp.astimezone(UTC),
        source=source,
    )
    payload = {
        "schema_version": manifest.schema_version,
        "refreshed_at": manifest.refreshed_at.isoformat(),
        "source": manifest.source,
    }
    atomic_write_text(
        models_dir / MODEL_DATABASE_MANIFEST_FILE_NAME,
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
    )
    return manifest


def select_model_database_dir(
    system_resources_dir: Path,
    runtime_models_dir: Path | None = None,
) -> Path:
    """Select the newer compatible Model DB directory as one indivisible root."""

    system_models_dir = system_resources_dir / MODEL_DATABASE_DIRECTORY_NAME
    if runtime_models_dir is None or not runtime_models_dir.is_dir():
        return system_models_dir

    runtime_manifest = read_model_database_manifest(runtime_models_dir)
    if runtime_manifest is None:
        return system_models_dir
    system_manifest = read_model_database_manifest(system_models_dir)
    if system_manifest is None:
        return runtime_models_dir
    if runtime_manifest.refreshed_at > system_manifest.refreshed_at:
        return runtime_models_dir
    return system_models_dir


def begin_runtime_model_database_refresh(
    system_resources_dir: Path,
    data_dir: Path,
) -> ModelDatabaseRefresh:
    """Create an isolated complete copy of the active Model DB for refresh.

    Existing refresh functions receive the returned resource-like staging root
    and write its ``models/`` child. The caller validates the result and calls
    :meth:`ModelDatabaseRefresh.commit`; failure calls ``discard`` and
    leaves the previously active runtime root untouched.
    """

    layout = DataDirectoryLayout(data_dir)
    runtime_models_dir = layout.models
    selected_models_dir = select_model_database_dir(system_resources_dir, runtime_models_dir)
    active_models_dir = selected_models_dir if selected_models_dir.is_dir() else None
    staging_resources_dir = layout.atomic_temporary / f"model-db-refresh-{uuid4().hex}"
    staging_models_dir = staging_resources_dir / MODEL_DATABASE_DIRECTORY_NAME
    if active_models_dir is None:
        staging_models_dir.mkdir(parents=True)
    else:
        shutil.copytree(active_models_dir, staging_models_dir)
    _synchronize_bundled_overrides(system_resources_dir, staging_models_dir)
    return ModelDatabaseRefresh(
        resources_dir=staging_resources_dir,
        target_models_dir=runtime_models_dir,
        source=MODEL_DATABASE_SOURCE_RUNTIME,
        publish_temporary_dir=layout.atomic_temporary,
    )


def begin_system_model_database_refresh(system_resources_dir: Path) -> ModelDatabaseRefresh:
    """Create an isolated copy of the system Model DB for a release refresh."""

    system_models_dir = system_resources_dir / MODEL_DATABASE_DIRECTORY_NAME
    staging_resources_dir = (
        system_resources_dir.parent / f".{system_resources_dir.name}-model-db-refresh-{uuid4().hex}"
    )
    staging_models_dir = staging_resources_dir / MODEL_DATABASE_DIRECTORY_NAME
    if system_models_dir.is_dir():
        shutil.copytree(system_models_dir, staging_models_dir)
    else:
        staging_models_dir.mkdir(parents=True)
    return ModelDatabaseRefresh(
        resources_dir=staging_resources_dir,
        target_models_dir=system_models_dir,
        source=MODEL_DATABASE_SOURCE_SYSTEM,
    )


def _synchronize_bundled_overrides(
    system_resources_dir: Path,
    staging_models_dir: Path,
) -> None:
    """Replace a runtime staging root's override snapshot with the bundled set."""

    for override_path in staging_models_dir.glob("*.overrides.json"):
        override_path.unlink()

    system_models_dir = system_resources_dir / MODEL_DATABASE_DIRECTORY_NAME
    if not system_models_dir.is_dir():
        return
    for override_path in system_models_dir.glob("*.overrides.json"):
        shutil.copy2(override_path, staging_models_dir / override_path.name)


def _replace_directory(
    source: Path,
    target: Path,
    *,
    temporary_dir: Path | None = None,
) -> None:
    """Replace ``target`` with a complete same-filesystem copy of ``source``."""

    if not source.is_dir():
        raise FileNotFoundError(f"Model DB directory not found: {source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    publish_root = temporary_dir or target.parent
    publish_root.mkdir(parents=True, exist_ok=True)
    token = uuid4().hex
    staging = publish_root / f".{target.name}.{token}.tmp"
    backup = publish_root / f".{target.name}.{token}.bak"
    shutil.copytree(source, staging)
    moved_existing = False
    try:
        if target.exists():
            target.replace(backup)
            moved_existing = True
        staging.replace(target)
    except OSError:
        if moved_existing and backup.exists() and not target.exists():
            backup.replace(target)
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        if target.exists():
            shutil.rmtree(backup, ignore_errors=True)
