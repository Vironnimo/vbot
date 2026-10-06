"""Named additive backfill for Generation 1 Project source lists.

The retired field stays an unknown field on disk. Only this initializer reads it;
runtime Projects and resolution consume the source list exclusively. No load
rewrites a document: the next guarded Project mutation persists the backfill.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from core.projects.sources.catalog import SOURCE_DEFINITIONS
from core.utils.file_status import is_dir_strict, is_file_strict


def project_sources_v1(data: dict[str, Any]) -> dict[str, Any]:
    if "sources" in data:
        return data
    previous = data.get("source_format") or "opencode"
    if previous not in {"opencode", "claude"}:
        raise ValueError("Cannot backfill Project sources from an invalid source_format.")
    selected = [
        source
        for source in SOURCE_DEFINITIONS
        if source.ecosystem == previous and source.kind in {"agents", "skills"}
    ]
    root = Path(data["cwd"])
    others = [
        source
        for source in SOURCE_DEFINITIONS
        if source not in selected
        and any(is_dir_strict(root / path) or is_file_strict(root / path) for path in source.paths)
    ]
    return {
        **data,
        "sources": [
            {
                "id": source.id,
                "enabled": source in selected,
                **(
                    {"agent_paths": [f".{previous}/agents/*.md"]}
                    if source in selected and source.kind == "agents"
                    else {}
                ),
            }
            for source in [*selected, *others]
        ],
    }
