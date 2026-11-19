"""Profile assignments that move between live documents and restorable payloads."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from core.agents import AgentError, load_validated_agent_json
from core.config_validation import discover_json_documents
from core.json_documents import DURABLE_DOCUMENTS
from core.projects import ProjectError, format_agent_address, load_validated_project_json
from core.sessions import (
    ARCHIVE_KIND_AGENT,
    ARCHIVE_KIND_PROJECT,
    ARCHIVE_STATE_ARCHIVED,
    ARCHIVE_STATE_PURGING,
    ARCHIVE_STATE_RESTORED,
    ARCHIVE_TREE_AGENT,
    ARCHIVE_TREE_PRIVATE_AGENT,
    ARCHIVE_TREE_PROJECT,
    ArchiveEntry,
    ArchiveEntryFilter,
    SessionArchiveLedger,
)

_CONFIG_FILES = {
    ARCHIVE_TREE_AGENT: "agent.json",
    ARCHIVE_TREE_PRIVATE_AGENT: "agent.json",
    ARCHIVE_TREE_PROJECT: "project.json",
}


def _entries(ledger: SessionArchiveLedger, kind: str) -> Iterator[ArchiveEntry]:
    cursor = None
    while True:
        page = ledger.page(ArchiveEntryFilter(kind=kind), cursor=cursor, limit=100)
        yield from page.entries
        if page.next_cursor is None:
            return
        cursor = page.next_cursor


def _assigned(path: Path, name: str) -> list[str]:
    if path.name == "agent.json":
        data = load_validated_agent_json(path)
        return [data["id"]] if data.get("permission_profile") == name else []
    data = load_validated_project_json(path)
    return [
        format_agent_address(agent_id, data["project_id"])
        for agent_id, fields in (data.get("overrides") or {}).items()
        if fields.get("permission_profile") == name
    ]


def permission_profile_references(
    data_dir: Path, ledger: SessionArchiveLedger, name: str
) -> tuple[list[str], list[str]]:
    """Return assignments and any unverifiable locations under the caller's reference lock.

    Ordinary rosters omit unreadable entries. Archive and restore move these
    documents under the reference lock. A concurrent purge can only remove an
    assignment and the purging state is irreversible.
    """
    references: list[str] = []
    unavailable: list[str] = []
    for kind in (ARCHIVE_KIND_AGENT, ARCHIVE_KIND_PROJECT):
        paths, failures = discover_json_documents(data_dir, DURABLE_DOCUMENTS[kind])
        unavailable.extend(str(report.file_path) for report in failures)
        for path in paths:
            try:
                references.extend(_assigned(path, name))
            except AgentError, ProjectError, OSError:
                unavailable.append(str(path))

        for entry in _entries(ledger, kind):
            if entry.state in (ARCHIVE_STATE_RESTORED, ARCHIVE_STATE_PURGING):
                continue
            if entry.state != ARCHIVE_STATE_ARCHIVED:
                unavailable.append(f"{entry.entry_id} ({entry.state})")
                continue
            for tree in entry.trees:
                filename = _CONFIG_FILES.get(tree.role)
                if filename is None:
                    continue
                path = data_dir / tree.path / filename
                try:
                    targets = _assigned(path, name)
                except AgentError, ProjectError, OSError:
                    current = ledger.entry_by_key(entry.entry_key)
                    if current and current.state != ARCHIVE_STATE_PURGING:
                        unavailable.append(f"{entry.entry_id} ({path})")
                else:
                    references.extend(f"{target} ({entry.entry_id})" for target in targets)
    return references, unavailable
