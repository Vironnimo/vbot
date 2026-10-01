"""Archive methods: list, show, restore and permanently delete archive entries.

The archive service (``runtime.archive``) owns every operation; these handlers
validate params, hold the locks the service expects from its callers and shape
results. A restore creates an Agent, Project or Session id, so it runs under the
Agent lifecycle guard like Agent create, rename and delete; a purge needs no
reference lock. Entry changes reach clients through the ``archive`` resource
event the server bridges from the archive service.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from core.archive import (
    ArchiveEntryBusyError,
    ArchiveEntryDetail,
    ArchiveEntryNotFoundError,
    ArchiveListing,
    PurgeOutcome,
    RestoreCheck,
    RestoreOutcome,
)
from core.sessions import (
    ARCHIVE_KIND_AGENT,
    ARCHIVE_KIND_PROJECT,
    ARCHIVE_KINDS,
    ArchiveEntryCursor,
    ArchiveEntryFilter,
)
from core.utils.logging import get_logger
from core.utils.timestamps import is_canonical_timestamp
from server.events import (
    RESOURCE_KIND_AGENTS,
    RESOURCE_KIND_PROJECTS,
    RESOURCE_KIND_SESSIONS,
)
from server.rpc.agent_refs import _guard_agent_lifecycle
from server.rpc.dispatcher import RpcMethodHandler
from server.rpc.error_mapping import _map_expected_error, restore_problem_payload
from server.rpc.errors import RPC_ERROR_INVALID_REQUEST, RpcError
from server.rpc.event_bridge import publish_resource_changed
from server.rpc.validation import (
    _optional_bool,
    _optional_positive_integer,
    _optional_string,
    _reject_unsupported,
    _required_agent_address,
    _required_string,
    _required_string_list,
)

JsonObject = dict[str, Any]

_LOGGER = get_logger("server.rpc.archive")

_LIST_LIMIT_MAX = 200
_SHOW_SESSION_LIMIT_MAX = 500
_PURGE_IDS_MAX = 100


async def _list_entries(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(
        params, {"kind", "agent_id", "project_id", "cursor", "limit"}, "archive.list"
    )
    filters = _entry_filter(params)
    cursor = _entry_cursor(params.get("cursor"))
    limit = _optional_positive_integer(params, "limit", max_value=_LIST_LIMIT_MAX) or 50
    try:
        page = await state.runtime.archive.list(filters, cursor=cursor, limit=limit)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return {
        "entries": [_entry_payload(listing) for listing in page.entries],
        "next_cursor": None
        if page.next_cursor is None
        else {"archived_at": page.next_cursor.archived_at, "entry_id": page.next_cursor.entry_id},
        "retention_days": page.retention_days,
        "retention_unknown": page.retention_unknown,
    }


async def _show_entry(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"entry_id", "session_limit"}, "archive.show")
    entry_id = _required_string(params, "entry_id")
    session_limit = (
        _optional_positive_integer(params, "session_limit", max_value=_SHOW_SESSION_LIMIT_MAX)
        or 100
    )
    try:
        detail = await state.runtime.archive.show(entry_id, session_limit=session_limit)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return _detail_payload(detail)


@_guard_agent_lifecycle
async def _restore_entry(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"entry_id", "target_id"}, "archive.restore")
    entry_id = _required_string(params, "entry_id")
    target_id = _optional_string(params, "target_id")
    try:
        outcome = await state.runtime.archive.restore(entry_id, target_id=target_id)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    _publish_restored(state, outcome)
    return _restore_payload(outcome)


async def _purge_entries(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(
        params, {"entry_ids", "all", "kind", "agent_id", "project_id"}, "archive.purge"
    )
    purge_all = _optional_bool(params, "all", default=False)
    if purge_all == ("entry_ids" in params):
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            "archive.purge needs either params.entry_ids or params.all = true, not both",
        )
    entry_ids: list[str] = []
    filters: ArchiveEntryFilter | None = None
    if purge_all:
        filters = _entry_filter(params)
    else:
        filter_fields = sorted({"kind", "agent_id", "project_id"} & set(params))
        if filter_fields:
            raise RpcError(
                RPC_ERROR_INVALID_REQUEST,
                f"archive.purge filters need params.all = true: {', '.join(filter_fields)}",
            )
        entry_ids = _required_string_list(params, "entry_ids")
        if not 1 <= len(entry_ids) <= _PURGE_IDS_MAX:
            raise RpcError(
                RPC_ERROR_INVALID_REQUEST,
                f"params.entry_ids must name 1 to {_PURGE_IDS_MAX} archive entries",
            )
    try:
        outcome = await state.runtime.archive.purge(entry_ids, all_matching=filters)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return _purge_payload(outcome)


async def purge_permanently(state: Any, entry_id: str) -> JsonObject:
    """Purge the entry a permanent delete just created; the delete result's purge fields.

    The archive has succeeded, so nothing here fails the delete. ``archive_entry_id``
    always names the entry. ``purged`` says whether this purge deleted it and
    ``purge_pending`` whether its deletion began and continues automatically;
    otherwise ``purge_reason`` says why it was not deleted: ``busy`` (another
    operation, such as a restore, took the entry first), ``gone`` (another
    operation deleted or restored it first), or a failure that left it unchanged.
    For a pending entry, ``purge_reason`` is the pending reason.
    """
    try:
        outcome = await state.runtime.archive.purge([entry_id], reason="permanent")
    except ArchiveEntryNotFoundError:
        return _permanent_purge(entry_id, reason="gone")
    except ArchiveEntryBusyError:
        return _permanent_purge(entry_id, reason="busy")
    except Exception as exc:
        _LOGGER.warning(
            "Permanent delete left its archive entry in the archive (entry=%s): %s",
            entry_id,
            exc,
        )
        return _permanent_purge(entry_id, reason=type(exc).__name__)
    if any(entry.entry_id == entry_id for entry in outcome.purged):
        return _permanent_purge(entry_id, purged=True)
    for pending in outcome.pending:
        if pending.entry_id == entry_id:
            return _permanent_purge(entry_id, pending=True, reason=pending.reason)
    for skipped in outcome.skipped:
        if skipped.entry_id == entry_id:
            return _permanent_purge(entry_id, reason=skipped.reason)
    return _permanent_purge(entry_id, reason="gone")


def not_purged() -> JsonObject:
    """The purge fields of a delete that only archived."""
    return {"purged": False, "purge_pending": False, "purge_reason": None}


def _permanent_purge(
    entry_id: str, *, purged: bool = False, pending: bool = False, reason: str | None = None
) -> JsonObject:
    return {
        "archive_entry_id": entry_id,
        "purged": purged,
        "purge_pending": pending,
        "purge_reason": reason,
    }


def _entry_filter(params: JsonObject) -> ArchiveEntryFilter:
    kind = _optional_string(params, "kind")
    if kind is not None and kind not in ARCHIVE_KINDS:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST, f"params.kind must be one of: {', '.join(ARCHIVE_KINDS)}"
        )
    project_id = _optional_string(params, "project_id")
    agent_id: str | None = None
    if params.get("agent_id") is not None:
        agent_id, address_project_id = _required_agent_address(params, "agent_id")
        if address_project_id is not None:
            if project_id is not None and project_id != address_project_id:
                raise RpcError(
                    RPC_ERROR_INVALID_REQUEST,
                    f"params.agent_id names Project {address_project_id}, "
                    f"params.project_id {project_id}",
                )
            project_id = address_project_id
    return ArchiveEntryFilter(kind=kind, agent_id=agent_id, project_id=project_id)


def _entry_cursor(value: Any) -> ArchiveEntryCursor | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"archived_at", "entry_id"}:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST, "params.cursor must be an archive.list next_cursor object"
        )
    archived_at = value["archived_at"]
    entry_id = value["entry_id"]
    if not is_canonical_timestamp(archived_at):
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "params.cursor.archived_at is invalid")
    if not isinstance(entry_id, str) or not entry_id:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "params.cursor.entry_id is invalid")
    return ArchiveEntryCursor(archived_at=archived_at, entry_id=entry_id)


def _entry_payload(listing: ArchiveListing) -> JsonObject:
    entry = listing.entry
    return {
        "entry_id": entry.entry_id,
        "kind": entry.kind,
        "state": entry.state,
        "subject_id": entry.subject_id,
        "project_id": entry.project_id or None,
        "agent_id": entry.agent_id or None,
        "owner_name": entry.owner_name,
        "label": listing.label,
        "archived_at": entry.archived_at,
        "origin": entry.origin,
        "purge_at": listing.purge_at,
        "session_count": entry.session_count,
        "restorable": listing.restorable,
        "not_restorable_reason": listing.not_restorable_reason,
        "may_hold_user_folders": listing.may_hold_user_folders,
    }


def _detail_payload(detail: ArchiveEntryDetail) -> JsonObject:
    entry = detail.listing.entry
    user_folders = {tree.path for tree in detail.user_folders}
    return {
        "entry": _entry_payload(detail.listing),
        "sessions": [
            {
                "session_id": member.address.session_id,
                "project_id": member.address.project_id,
                "agent_id": member.address.agent_id,
                "title": member.title,
                "created_at": member.created_at,
                "last_activity_at": member.last_activity_at,
            }
            for member in detail.sessions
        ],
        "session_count": entry.session_count,
        "files": {
            "state": detail.files,
            "trees": [
                {
                    "role": tree.role,
                    "path": tree.path,
                    "source_path": tree.source_path,
                    "user_folder": tree.path in user_folders,
                }
                for tree in entry.trees
            ],
        },
        "details": _entry_details(entry.facts),
        "restore": _restore_check_payload(detail.restore),
    }


def _entry_details(facts: Mapping[str, Any]) -> JsonObject:
    """The recorded facts a reader needs, without restore or cleanup internals."""
    details: JsonObject = {}
    for key in ("name", "root_project_id", "roster_index", "cwd", "reason"):
        if key in facts:
            details[key] = facts[key]
    workspace = facts.get("workspace")
    if isinstance(workspace, Mapping):
        details["workspace"] = {
            key: workspace.get(key) for key in ("path", "external") if key in workspace
        }
    if "grants" in facts:
        details["grants"] = _agent_ids(facts["grants"])
    if "unrooted_agents" in facts:
        details["unrooted_agents"] = _agent_ids(facts["unrooted_agents"])
    return details


def _agent_ids(records: Any) -> list[str]:
    return [
        record["agent_id"]
        for record in records or ()
        if isinstance(record, Mapping) and isinstance(record.get("agent_id"), str)
    ]


def _restore_check_payload(check: RestoreCheck) -> JsonObject:
    return {
        "possible": check.possible,
        "target_id": check.target_id,
        "blockers": [restore_problem_payload(blocker) for blocker in check.blockers],
        "warnings": [restore_problem_payload(warning) for warning in check.warnings],
    }


def _restore_payload(outcome: RestoreOutcome) -> JsonObject:
    restored: JsonObject
    if outcome.kind == ARCHIVE_KIND_AGENT:
        restored = {"agent_id": outcome.target_id}
    elif outcome.kind == ARCHIVE_KIND_PROJECT:
        restored = {"project_id": outcome.target_id}
    else:
        address = outcome.addresses[0]
        restored = {
            "session_id": address.session_id,
            "agent_id": address.agent_id,
            "project_id": address.project_id,
        }
    return {
        "entry_id": outcome.entry_id,
        "kind": outcome.kind,
        "subject_id": outcome.subject_id,
        "restored": restored,
        "session_count": len(outcome.addresses),
        "grant_agent_ids": list(outcome.grant_agent_ids),
        "warnings": [restore_problem_payload(warning) for warning in outcome.warnings],
    }


def _purge_payload(outcome: PurgeOutcome) -> JsonObject:
    return {
        "purged": [
            {
                "entry_id": entry.entry_id,
                "kind": entry.kind,
                "subject_id": entry.subject_id,
                "session_count": entry.session_count,
            }
            for entry in outcome.purged
        ],
        "pending": [
            {"entry_id": entry.entry_id, "reason": entry.reason} for entry in outcome.pending
        ],
        "skipped": [
            {"entry_id": entry.entry_id, "reason": entry.reason, "state": entry.state}
            for entry in outcome.skipped
        ],
        "gone": list(outcome.gone),
        "kept": [{"entry_id": entry.entry_id, "reason": entry.reason} for entry in outcome.kept],
    }


def _publish_restored(state: Any, outcome: RestoreOutcome) -> None:
    if outcome.kind == ARCHIVE_KIND_AGENT:
        publish_resource_changed(state, RESOURCE_KIND_AGENTS)
    elif outcome.kind == ARCHIVE_KIND_PROJECT:
        # Re-rooted Identity Agents change with their Project.
        publish_resource_changed(state, RESOURCE_KIND_PROJECTS)
        publish_resource_changed(state, RESOURCE_KIND_AGENTS)
    else:
        address = outcome.addresses[0]
        publish_resource_changed(
            state,
            RESOURCE_KIND_SESSIONS,
            scope={"agent_id": address.agent_id, "project_id": address.project_id},
        )


def method_handlers() -> dict[str, RpcMethodHandler]:
    """Return the registered archive RPC handlers."""
    return {
        "archive.list": _list_entries,
        "archive.show": _show_entry,
        "archive.restore": _restore_entry,
        "archive.purge": _purge_entries,
    }
