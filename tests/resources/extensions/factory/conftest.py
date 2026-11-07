"""Factory databases are built once per worker and cloned for each contract test."""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from core.database import open_offline_database
from core.extensions.databases import extension_database_spec
from resources.extensions.factory.store import DATABASE_NAME, SCHEMA_SQL, FactoryStore


@pytest.fixture(scope="session")
def factory_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    directory = tmp_path_factory.mktemp("factory-template")
    spec = extension_database_spec(directory, "factory", DATABASE_NAME, SCHEMA_SQL)
    database = open_offline_database(spec)
    database.close()
    return spec.path


@pytest.fixture
def factory_store(tmp_path: Path, factory_template: Path) -> Iterator[FactoryStore]:
    spec = extension_database_spec(tmp_path, "factory", DATABASE_NAME, SCHEMA_SQL)
    spec.path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(factory_template, spec.path)
    database = open_offline_database(spec)
    yield FactoryStore(database)
    database.close()
