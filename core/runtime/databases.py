"""The canonical databases a Runtime composes, for tools that work while it is stopped.

The running Runtime exposes its open handles through
``Runtime.canonical_databases()``. Offline tools (data-store status, restore,
the updater's snapshot) use these declarations to check owner compatibility
and owner facts without opening the databases through a Runtime.
"""

from __future__ import annotations

from pathlib import Path

from core.channels import channel_database_spec
from core.channels._state_schema import DATABASE_NAME as CHANNELS_DATABASE_NAME
from core.database import DatabaseSpec, canonical_database_path
from core.model_tasks.decision_store import DATABASE_NAME as DECISIONS_DATABASE_NAME
from core.model_tasks.decision_store import decision_database_spec
from core.providers.usage_history import DATABASE_NAME as PROVIDER_USAGE_DATABASE_NAME
from core.providers.usage_history import provider_usage_database_spec
from core.sessions._store_schema import session_database_spec
from core.sessions.schema import DATABASE_NAME as SESSIONS_DATABASE_NAME
from core.usage import usage_database_spec


def canonical_database_specs(data_dir: Path) -> tuple[DatabaseSpec, ...]:
    """Declarations of every canonical database this vBot owns in ``data_dir``.

    Extension databases (``ext.<owner>.<name>``) are declared only when their
    Extension opens them through ``host.open_database``: declaring them here
    would mean running Extension code in an offline tool. Offline tools verify
    them by kernel identity and integrity only, without owner facts or schema
    compatibility checks.
    """
    return (
        session_database_spec(canonical_database_path(data_dir, SESSIONS_DATABASE_NAME)),
        decision_database_spec(canonical_database_path(data_dir, DECISIONS_DATABASE_NAME)),
        provider_usage_database_spec(
            canonical_database_path(data_dir, PROVIDER_USAGE_DATABASE_NAME)
        ),
        channel_database_spec(canonical_database_path(data_dir, CHANNELS_DATABASE_NAME)),
        usage_database_spec(canonical_database_path(data_dir, "model_usage")),
    )
