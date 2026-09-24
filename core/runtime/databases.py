"""The canonical databases a Runtime composes, for tools that work while it is stopped.

The running Runtime exposes its open handles through
``Runtime.canonical_databases()``. Offline tools (data-store status, restore,
the updater's snapshot) use these declarations to check owner compatibility
and owner facts without opening the databases through a Runtime.
"""

from __future__ import annotations

from pathlib import Path

from core.database import DatabaseSpec, canonical_database_path
from core.providers.usage_history import DATABASE_NAME as PROVIDER_USAGE_DATABASE_NAME
from core.providers.usage_history import provider_usage_database_spec
from core.sessions._store_schema import session_database_spec
from core.sessions.schema import DATABASE_NAME as SESSIONS_DATABASE_NAME


def canonical_database_specs(data_dir: Path) -> tuple[DatabaseSpec, ...]:
    """Declarations of every canonical database this vBot owns in ``data_dir``.

    Extension databases are declared by their Extensions at load time and are
    not listed here; offline tools verify them by kernel identity only.
    """
    return (
        session_database_spec(canonical_database_path(data_dir, SESSIONS_DATABASE_NAME)),
        provider_usage_database_spec(
            canonical_database_path(data_dir, PROVIDER_USAGE_DATABASE_NAME)
        ),
    )
