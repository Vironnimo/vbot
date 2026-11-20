"""Server filesystem browsing RPC handlers for path pickers: listing and creating entries."""

from __future__ import annotations

from typing import Any

from core.utils.directory_listing import DirectoryListing, EntryKind, create_entry, list_directory
from server.rpc.dispatcher import RpcMethodHandler
from server.rpc.error_mapping import _map_expected_error
from server.rpc.errors import RPC_ERROR_INVALID_REQUEST, RpcError
from server.rpc.validation import (
    _optional_bool,
    _optional_string,
    _reject_unsupported,
    _required_string,
    rpc_request_fields,
)

JsonObject = dict[str, Any]


async def _list_filesystem(_state: Any, params: JsonObject) -> JsonObject:
    """List one server directory, or the places to start from.

    ``path`` null or absent lists the places (filesystem roots plus ``home``);
    otherwise it names an absolute directory, ``~`` or ``~/...``, or with ``root``
    a directory relative to that root (``""`` is the root). A non-empty ``prefix``
    lists only the entries whose names start with it, ignoring case, before the
    entry limit applies. The listing runs off the Event Loop within its own budget.
    """
    _reject_unsupported(params, rpc_request_fields("filesystem.list"), "filesystem.list")
    path = params.get("path")
    if path is not None and not isinstance(path, str):
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "params.path must be a string or null")
    root = _optional_string(params, "root")
    include_files = _optional_bool(params, "include_files", default=False)
    prefix = params.get("prefix")
    if prefix is not None and not isinstance(prefix, str):
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "params.prefix must be a string or null")
    try:
        listing = await list_directory(path, root=root, include_files=include_files, prefix=prefix)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return _listing_payload(listing)


async def _create_filesystem_directory(_state: Any, params: JsonObject) -> JsonObject:
    return await _create_filesystem_entry(params, "directory")


async def _create_filesystem_file(_state: Any, params: JsonObject) -> JsonObject:
    return await _create_filesystem_entry(params, "file")


async def _create_filesystem_entry(params: JsonObject, kind: EntryKind) -> JsonObject:
    method = f"filesystem.create_{kind}"
    _reject_unsupported(params, rpc_request_fields(method), method)
    path = params.get("path")
    if not isinstance(path, str):
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "params.path must be a string")
    name = _required_string(params, "name")
    root = _optional_string(params, "root")
    try:
        created = await create_entry(path, name, kind=kind, root=root)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return {"path": created}


def _listing_payload(listing: DirectoryListing) -> JsonObject:
    payload: JsonObject = {
        "path": listing.path,
        "parent": listing.parent,
        "entries": [
            {"name": entry.name, "kind": entry.kind, "link": entry.link, "hidden": entry.hidden}
            for entry in listing.entries
        ],
        "truncated": listing.truncated,
        "separator": listing.separator,
    }
    if listing.home is not None:
        payload["home"] = listing.home
    return payload


def method_handlers() -> dict[str, RpcMethodHandler]:
    """Return server filesystem browsing RPC handlers."""

    return {
        "filesystem.create_file": _create_filesystem_file,
        "filesystem.list": _list_filesystem,
        "filesystem.create_directory": _create_filesystem_directory,
    }
