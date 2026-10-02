"""Storage contract for the bundled system and the runtime Model DB.

The system Model DB (``resources/models/``) is complete: the generated canonical
layer ``models.json``, the generated ``<provider>.json`` catalog of each Provider
it ships, and every hand-maintained ``*.overrides.json``. The runtime Model DB
(``<data_dir>/artifacts/models/``) holds only the generated catalogs this
installation refreshed itself.

Each root's ``manifest.json`` records, per generated catalog file, when a refresh
of that root last fetched it. Load selects every catalog file on its own: the
runtime copy when the runtime root fetched it more recently than the system
root, or when the system root does not ship that catalog; otherwise the system
copy. A catalog without a record, such as a hand-written seed, counts as older
than any record. An update's newer bundled catalogs therefore reach an
installation whose runtime root refreshed other catalogs later, while catalogs
only this installation produces (Custom Providers, local servers) keep coming
from the runtime root. The current bundled overrides are always the hand layer.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from core.models.assembly import (
    CANONICAL_FILE_NAME,
    CANONICAL_OVERRIDES_FILE_NAME,
    ModelDataIssueReport,
    log_model_data_issue,
)
from core.storage.layout import DataDirectoryLayout
from core.utils.atomic import atomic_write_text
from core.utils.file_status import exists_strict

MODEL_DATABASE_DIRECTORY_NAME = "models"
MODEL_DATABASE_MANIFEST_FILE_NAME = "manifest.json"
MODEL_DATABASE_SCHEMA_VERSION = 2
MODEL_DATABASE_SOURCE_RUNTIME = "runtime"
MODEL_DATABASE_SOURCE_SYSTEM = "system"
_MODEL_DATABASE_SOURCES = frozenset({MODEL_DATABASE_SOURCE_RUNTIME, MODEL_DATABASE_SOURCE_SYSTEM})

# Provider-layer files under ``models/`` are ``<provider>.json``. ``*.raw.json`` is
# a legacy inspection dump older refreshes wrote; ``*.overrides.json`` is a hand
# layer applied during assembly; the canonical files and the manifest have their
# own readers. The offline validator shares this classification.
RAW_FILE_SUFFIX = ".raw.json"
OVERRIDES_FILE_SUFFIX = ".overrides.json"
_JSON_SUFFIX = ".json"
_NON_PROVIDER_FILE_NAMES = frozenset(
    {
        CANONICAL_FILE_NAME,
        CANONICAL_OVERRIDES_FILE_NAME,
        MODEL_DATABASE_MANIFEST_FILE_NAME,
    }
)


def is_provider_file(file_name: str) -> bool:
    """Return whether the ``*.json`` file ``file_name`` is a ``<provider>.json`` catalog.

    Excludes a legacy ``*.raw.json`` inspection dump, the ``*.overrides.json`` hand
    layer, the database ``manifest.json``, and the canonical ``models.json`` /
    ``models.overrides.json``.
    """

    if file_name.endswith(RAW_FILE_SUFFIX) or file_name.endswith(OVERRIDES_FILE_SUFFIX):
        return False
    return file_name not in _NON_PROVIDER_FILE_NAMES


@dataclass(frozen=True)
class ModelDatabaseManifest:
    """Refresh provenance of one Model DB root.

    ``catalogs`` maps each generated catalog file a refresh of this root fetched
    to when it last did; ``refreshed_at`` is the root's latest refresh.
    """

    schema_version: int
    refreshed_at: datetime
    source: str
    catalogs: Mapping[str, datetime]


@dataclass(frozen=True)
class ModelDatabaseFiles:
    """The files one Load assembles, each catalog taken from its selected root."""

    canonical: Path | None
    providers: tuple[Path, ...]
    overrides_dir: Path


@dataclass(frozen=True)
class ModelDatabaseRefresh:
    """Isolated working copy for one Model DB refresh.

    ``catalogs`` holds the records of the catalogs the working copy took from the
    root it publishes to; :meth:`commit` carries them forward.
    """

    resources_dir: Path
    target_models_dir: Path
    source: str
    catalogs: Mapping[str, datetime]
    publish_temporary_dir: Path | None = None

    def commit(
        self,
        *,
        providers: Collection[str] = (),
        canonical: bool = False,
    ) -> ModelDatabaseManifest:
        """Record what this refresh fetched and atomically publish the root.

        ``providers`` names each Provider whose catalog this refresh fetched and
        ``canonical`` whether it fetched the canonical layer; they are recorded
        with the commit time. Every other catalog keeps the record it was copied
        with. A runtime root keeps only recorded catalogs: the rest of its working
        copy came from the system root, which Load reads directly.
        """

        working_models_dir = self.resources_dir / MODEL_DATABASE_DIRECTORY_NAME
        refreshed_at = datetime.now(UTC)
        fetched = {f"{provider_id}{_JSON_SUFFIX}" for provider_id in providers}
        if canonical:
            fetched.add(CANONICAL_FILE_NAME)
        catalogs: dict[str, datetime] = {}
        for name in _catalog_paths(working_models_dir):
            if name in fetched:
                catalogs[name] = refreshed_at
            elif name in self.catalogs:
                catalogs[name] = self.catalogs[name]
        if self.source == MODEL_DATABASE_SOURCE_RUNTIME:
            for path in working_models_dir.iterdir():
                if path.name in catalogs:
                    continue
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink()
        manifest = write_model_database_manifest(
            working_models_dir,
            source=self.source,
            catalogs=catalogs,
            refreshed_at=refreshed_at,
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
    except OSError, UnicodeError, json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    schema_version = data.get("schema_version")
    source = data.get("source")
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != MODEL_DATABASE_SCHEMA_VERSION
    ):
        return None
    if not isinstance(source, str) or source not in _MODEL_DATABASE_SOURCES:
        return None
    refreshed_at = _parse_timestamp(data.get("refreshed_at"))
    catalogs_data = data.get("catalogs")
    if refreshed_at is None or not isinstance(catalogs_data, dict):
        return None
    catalogs: dict[str, datetime] = {}
    for name, value in catalogs_data.items():
        fetched_at = _parse_timestamp(value)
        if not _is_catalog_file_name(name) or fetched_at is None:
            return None
        catalogs[name] = fetched_at
    return ModelDatabaseManifest(
        schema_version=schema_version,
        refreshed_at=refreshed_at,
        source=source,
        catalogs=catalogs,
    )


def write_model_database_manifest(
    models_dir: Path,
    *,
    source: str,
    catalogs: Mapping[str, datetime] | None = None,
    refreshed_at: datetime | None = None,
) -> ModelDatabaseManifest:
    """Atomically stamp one Model DB root after a successful refresh."""

    if source not in _MODEL_DATABASE_SOURCES:
        raise ValueError(f"Unsupported Model DB source: {source}")
    timestamp = refreshed_at or datetime.now(UTC)
    records = dict(catalogs or {})
    for name, fetched_at in [("refreshed_at", timestamp), *records.items()]:
        if fetched_at.tzinfo is None:
            raise ValueError(f"Model DB refresh time of '{name}' must include a timezone")
    for name in records:
        if not _is_catalog_file_name(name):
            raise ValueError(f"Not a generated Model DB catalog: {name}")
    manifest = ModelDatabaseManifest(
        schema_version=MODEL_DATABASE_SCHEMA_VERSION,
        refreshed_at=timestamp.astimezone(UTC),
        source=source,
        catalogs={name: fetched_at.astimezone(UTC) for name, fetched_at in records.items()},
    )
    payload = {
        "schema_version": manifest.schema_version,
        "refreshed_at": manifest.refreshed_at.isoformat(),
        "source": manifest.source,
        "catalogs": {
            name: fetched_at.isoformat() for name, fetched_at in manifest.catalogs.items()
        },
    }
    atomic_write_text(
        models_dir / MODEL_DATABASE_MANIFEST_FILE_NAME,
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
    )
    return manifest


def select_model_database_files(
    system_resources_dir: Path,
    runtime_models_dir: Path | None = None,
    *,
    report: ModelDataIssueReport = log_model_data_issue,
) -> ModelDatabaseFiles:
    """Select each generated catalog from the root that fetched it last.

    Without a compatible runtime manifest every catalog comes from the system
    root. ``report`` receives a root whose files cannot be listed.
    """

    system_models_dir = system_resources_dir / MODEL_DATABASE_DIRECTORY_NAME
    try:
        selected, _runtime_catalogs = _select_catalogs(system_models_dir, runtime_models_dir)
    except OSError as exc:
        report(f"Could not list the Model DB catalogs: {exc}")
        selected = {}
    canonical = selected.pop(CANONICAL_FILE_NAME, None)
    return ModelDatabaseFiles(
        canonical=canonical,
        providers=tuple(selected[name] for name in sorted(selected)),
        overrides_dir=system_models_dir,
    )


def begin_runtime_model_database_refresh(
    system_resources_dir: Path,
    data_dir: Path,
    *,
    provider_ids: Collection[str] | None = None,
) -> ModelDatabaseRefresh:
    """Create an isolated working copy of the effective Model DB for a runtime refresh.

    The copy holds every catalog Load currently selects, each from its root, plus
    the current bundled overrides, so refresh functions merge into and
    validation checks what Load sees. Existing refresh functions receive the
    returned resource-like staging root and write its ``models/`` child. The
    caller validates the result and calls :meth:`ModelDatabaseRefresh.commit`;
    failure calls ``discard`` and leaves the published runtime root untouched.

    ``provider_ids`` names every Provider that currently exists, Custom
    Providers included. When given, the copy drops each generated
    ``<provider>.json`` whose Provider is not among them, such as the catalog of
    a deleted Custom Provider; a catalog the system Model DB ships is kept.
    """

    layout = DataDirectoryLayout(data_dir)
    system_models_dir = system_resources_dir / MODEL_DATABASE_DIRECTORY_NAME
    selected, runtime_catalogs = _select_catalogs(system_models_dir, layout.models)
    staging_resources_dir = layout.atomic_temporary / f"model-db-refresh-{uuid4().hex}"
    staging_models_dir = staging_resources_dir / MODEL_DATABASE_DIRECTORY_NAME
    try:
        staging_models_dir.mkdir(parents=True)
        for name, path in selected.items():
            shutil.copy2(path, staging_models_dir / name)
        if system_models_dir.is_dir():
            for override_path in system_models_dir.glob(f"*{OVERRIDES_FILE_SUFFIX}"):
                shutil.copy2(override_path, staging_models_dir / override_path.name)
        if provider_ids is not None:
            _drop_orphan_provider_catalogs(staging_models_dir, system_models_dir, provider_ids)
    except BaseException:
        shutil.rmtree(staging_resources_dir, ignore_errors=True)
        raise
    return ModelDatabaseRefresh(
        resources_dir=staging_resources_dir,
        target_models_dir=layout.models,
        source=MODEL_DATABASE_SOURCE_RUNTIME,
        catalogs=runtime_catalogs,
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
        shutil.copytree(system_models_dir, staging_models_dir, ignore=_skip_legacy_raw_dumps)
    else:
        staging_models_dir.mkdir(parents=True)
    system_manifest = read_model_database_manifest(system_models_dir)
    return ModelDatabaseRefresh(
        resources_dir=staging_resources_dir,
        target_models_dir=system_models_dir,
        source=MODEL_DATABASE_SOURCE_SYSTEM,
        catalogs=system_manifest.catalogs if system_manifest is not None else {},
    )


def _select_catalogs(
    system_models_dir: Path,
    runtime_models_dir: Path | None,
) -> tuple[dict[str, Path], dict[str, datetime]]:
    """Return every selected catalog path and the records of those from the runtime root."""

    selected = _catalog_paths(system_models_dir)
    runtime_manifest = (
        read_model_database_manifest(runtime_models_dir) if runtime_models_dir is not None else None
    )
    if runtime_models_dir is None or runtime_manifest is None:
        return selected, {}
    system_manifest = read_model_database_manifest(system_models_dir)
    system_catalogs = system_manifest.catalogs if system_manifest is not None else {}
    runtime_catalogs: dict[str, datetime] = {}
    for name, fetched_at in runtime_manifest.catalogs.items():
        runtime_path = runtime_models_dir / name
        if not runtime_path.is_file():
            continue
        system_fetched_at = system_catalogs.get(name)
        if name in selected and system_fetched_at is not None and system_fetched_at >= fetched_at:
            continue
        selected[name] = runtime_path
        runtime_catalogs[name] = fetched_at
    return selected, runtime_catalogs


def _catalog_paths(models_dir: Path) -> dict[str, Path]:
    """Return the generated catalog files of one root by file name."""

    if not models_dir.is_dir():
        return {}
    return {
        path.name: path
        for path in models_dir.glob(f"*{_JSON_SUFFIX}")
        if _is_catalog_file_name(path.name) and path.is_file()
    }


def _is_catalog_file_name(name: Any) -> bool:
    """Return whether ``name`` is the bare file name of a generated catalog."""

    return (
        isinstance(name, str)
        and name.endswith(_JSON_SUFFIX)
        and not name.startswith(".")
        and "/" not in name
        and "\\" not in name
        and (name == CANONICAL_FILE_NAME or is_provider_file(name))
    )


def _parse_timestamp(value: Any) -> datetime | None:
    """Parse a timezone-aware ISO timestamp as UTC; anything else is ``None``."""

    if not isinstance(value, str):
        return None
    try:
        timestamp = datetime.fromisoformat(value)
    except ValueError:
        return None
    if timestamp.tzinfo is None:
        return None
    return timestamp.astimezone(UTC)


def _skip_legacy_raw_dumps(_directory: str, names: list[str]) -> set[str]:
    """Leave out the ``*.raw.json`` inspection dumps older refreshes wrote.

    Nothing reads them, so a refresh stops copying them forward and the root it
    publishes no longer contains them.
    """

    return {name for name in names if name.endswith(RAW_FILE_SUFFIX)}


def _drop_orphan_provider_catalogs(
    staging_models_dir: Path,
    system_models_dir: Path,
    provider_ids: Collection[str],
) -> None:
    """Remove staged generated catalogs of Providers that no longer exist."""

    known_provider_ids = frozenset(provider_ids)
    for catalog_path in staging_models_dir.glob(f"*{_JSON_SUFFIX}"):
        if not is_provider_file(catalog_path.name) or catalog_path.stem in known_provider_ids:
            continue
        if exists_strict(system_models_dir / catalog_path.name):
            continue
        catalog_path.unlink()


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
