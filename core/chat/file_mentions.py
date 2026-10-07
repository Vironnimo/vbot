"""``@``-mention file support: the picker's listings and send-time snapshots.

The composer's ``@`` picker (``files.list`` RPC) lists the mention root, the
directory relative tool paths resolve against: an index of the files and
directories the search Tools would search there, and on request the direct
entries of one directory, ignored ones included and marked, so the picker
reaches them by navigating. On send, every mentioned file is snapshotted once
into a ``file_mention`` content block — content inline for reasonably sized text
files — and stamped as read in the session's read-before-write guard, so the
agent can edit a mentioned file without a separate read tool call. The snapshot
is durable in canonical Session history: the model always sees the file as it was
when the user sent the message, and the stale-guard catches a later edit when the
file changed afterwards.
"""

from __future__ import annotations

import stat
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from core.agents import default_workspace_dir
from core.attachments import sniff_media_type
from core.chat.content_blocks import ContentBlock, FileMentionBlock, TextBlock
from core.chat.errors import ChatError
from core.prompts import INLINE_FILE_MAX_BYTES
from core.sessions import AGENT_DEFAULT_PROJECT
from core.tools.search import SearchBudget, list_selected_files, unselected_names
from core.utils.directory_listing import DirectoryListingError, EntryKind, list_directory
from core.utils.file_status import stat_or_none
from core.utils.logging import get_logger
from core.utils.search_binary import require_binary
from core.utils.workers import BoundedWorkerPool

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from core.runtime.interfaces import RuntimeServices
    from core.sessions import WorkingProjectChoice
    from core.tools.file_state import FileReadState
    from core.utils.directory_listing import DirectoryListing

_LOGGER = get_logger("chat.file_mentions")

# Inline cap for a mentioned text file, shared with Project auto-load files: larger
# files degrade to a reference note the agent can read selectively, so one
# @-mention cannot flood the context window.
MENTION_INLINE_MAX_BYTES = INLINE_FILE_MAX_BYTES

# Hard ceiling for the picker's file index; the files.list response marks truncation.
# It holds most repositories whole: 20,000 files index in about 0.3 s and make a
# response of about 1.5 MB. The picker reaches files beyond it by navigating.
MENTION_FILE_LIST_LIMIT = 20_000

# Wall-clock budget for one index, and for marking the ignored entries of one
# directory. A tree that cannot be indexed within this is too big for an
# interactive picker; the truncated prefix is returned.
MENTION_FILE_LIST_TIMEOUT_SECONDS = 5.0

# Listings read the filesystem and run the search engine, never on the Event Loop.
_LISTING_WORKERS = BoundedWorkerPool(name="mention-listing", max_workers=2)


@dataclass(frozen=True)
class MentionIndex:
    """The files and directories the ``@`` picker offers under a mention root.

    Paths are relative to the root with ``/`` separators, sorted by name. They are
    what the search Tools search there: ignore rules apply, hidden files count and
    ``.git`` is left out. ``directories`` are the directories of the listed files.
    ``truncated`` says the index is incomplete: the file cap or the time budget
    cut it.
    """

    files: tuple[str, ...]
    directories: tuple[str, ...]
    truncated: bool


@dataclass(frozen=True)
class MentionEntry:
    """One direct entry of a directory under a mention root."""

    name: str
    kind: EntryKind
    # The index leaves the entry out: an ignore rule or the .git exclusion applies
    # to it or to a directory above it.
    ignored: bool


@dataclass(frozen=True)
class MentionDirectory:
    """The direct entries of one directory under a mention root, ignored ones included."""

    entries: tuple[MentionEntry, ...]
    truncated: bool


def resolve_mention_root(
    runtime: RuntimeServices,
    agent_id: str,
    project_id: str | None,
    *,
    session_id: str | None = None,
    working_project_id: WorkingProjectChoice = AGENT_DEFAULT_PROJECT,
) -> Path:
    """Resolve the directory ``@``-mentions work against for one chat address.

    Mirrors tool path resolution (``ToolContext.effective_cwd``): a Session
    working in a Project uses the Project's repo cwd, any other Session the
    Agent's Workspace — so the picker lists exactly the tree that relative tool
    paths resolve against. An existing Session (*session_id*) uses its own
    working Project; without one (a draft, or a Session not created yet) the
    root is where a new Session with *working_project_id* would work.
    """
    resolver = runtime.agent_resolver
    agent = resolver.resolve_agent(project_id, agent_id)
    working_project_id = resolver.resolve_working_project(
        project_id, agent, session_id=session_id, requested=working_project_id
    )
    if working_project_id is not None:
        root = Path(runtime.projects.get(working_project_id).cwd)
        if not root.is_dir():
            raise ChatError(f"Project repository is unavailable: {root}")
        return root
    workspace = getattr(agent, "workspace", None)
    if workspace:
        return Path(workspace)
    return default_workspace_dir(runtime.storage.data_dir, agent_id)


async def list_mention_files(root: Path) -> MentionIndex:
    """Index the files and directories under ``root`` for the ``@`` picker.

    The index stops at ``MENTION_FILE_LIST_LIMIT`` files or after
    ``MENTION_FILE_LIST_TIMEOUT_SECONDS`` and is then marked truncated. A missing
    root lists as empty: a fresh Workspace is a valid, empty picker source.
    """
    return await _LISTING_WORKERS.run(_index, root)


async def list_mention_directory(root: Path, directory: str) -> MentionDirectory:
    """List the direct entries of ``directory`` under ``root`` for the ``@`` picker.

    ``directory`` is relative to ``root`` (``""`` is the root) and must stay inside
    it. Entries the index leaves out are listed too and marked ``ignored``. A
    missing root lists as empty, like the index.

    Raises :class:`~core.utils.directory_listing.ListingPathError` for a directory
    outside ``root`` and :class:`~core.utils.directory_listing.DirectoryListingError`
    when the directory cannot be listed or its entries cannot be marked in time.
    """
    base = root.expanduser().absolute()
    try:
        listing = await list_directory(directory, root=str(base), include_files=True)
    except DirectoryListingError as error:
        if error.reason == "not_found" and await _LISTING_WORKERS.run(_listable, base) is None:
            return MentionDirectory(entries=(), truncated=False)
        raise
    ignored = await _LISTING_WORKERS.run(_ignored_names, base, listing)
    return MentionDirectory(
        entries=tuple(
            MentionEntry(name=entry.name, kind=entry.kind, ignored=entry.name in ignored)
            for entry in listing.entries
        ),
        truncated=listing.truncated,
    )


def _index(root: Path) -> MentionIndex:
    resolved_root = _listable(root)
    if resolved_root is None:
        return MentionIndex(files=(), directories=(), truncated=False)
    budget = SearchBudget(None, timeout_seconds=MENTION_FILE_LIST_TIMEOUT_SECONDS)
    files, truncated = list_selected_files(
        _search_engine(), resolved_root, budget, limit=MENTION_FILE_LIST_LIMIT
    )
    directories: set[str] = set()
    for file in files:
        end = file.rfind("/")
        # Each file adds its directories up to the first one already known.
        while end > 0 and file[:end] not in directories:
            directories.add(file[:end])
            end = file.rfind("/", 0, end)
    return MentionIndex(
        files=tuple(sorted(files, key=_name_order)),
        directories=tuple(sorted(directories, key=_name_order)),
        truncated=truncated,
    )


def _ignored_names(base: Path, listing: DirectoryListing) -> set[str]:
    budget = SearchBudget(None, timeout_seconds=MENTION_FILE_LIST_TIMEOUT_SECONDS)
    names = [entry.name for entry in listing.entries]
    ignored = unselected_names(_search_engine(), base.resolve(), listing.path, names, budget)
    if budget.timed_out:
        shown = listing.path or "The mention root"
        raise DirectoryListingError("timeout", f"{shown} did not answer in time; try again later.")
    return ignored


def _listable(root: Path) -> Path | None:
    """Return ``root`` resolved, or ``None`` when no directory is there.

    A root that cannot be checked counts as listable; its listing reports why it
    cannot be read.
    """
    resolved = root.expanduser().resolve()
    try:
        status = stat_or_none(resolved)
    except OSError:
        return resolved
    if status is None or not stat.S_ISDIR(status.st_mode):
        return None
    return resolved


def _search_engine() -> Path:
    try:
        return require_binary()
    except ValueError as error:
        raise ChatError(f"The file picker cannot list files: {error}") from error


def _name_order(path: str) -> tuple[str, str]:
    return path.casefold(), path


def expand_file_mentions(
    content: str | list[ContentBlock],
    mentions: Sequence[str],
    *,
    root: Path,
    session_id: str,
    file_state: FileReadState,
) -> str | list[ContentBlock]:
    """Append one send-time snapshot block per mentioned file.

    String content is promoted to a block list with the original text first.
    Duplicate mentions collapse to one block. An inlined snapshot stamps the
    file as read for the session (the whole point: no separate read call before
    an edit); degraded snapshots (too large / not text / missing) do not — the
    agent has not seen their content.
    """
    unique_mentions = list(dict.fromkeys(mention for mention in mentions if mention.strip()))
    if not unique_mentions:
        return content

    blocks: list[ContentBlock] = (
        [TextBlock(type="text", text=content)] if isinstance(content, str) else list(content)
    )
    for mention in unique_mentions:
        blocks.append(
            _snapshot_mention(mention, root=root, session_id=session_id, file_state=file_state)
        )
    return blocks


def file_mention_request_text(block: Mapping[str, Any]) -> str:
    """Render a persisted ``file_mention`` block dict as provider-request text.

    The framing tells the model where the content came from (the ``@``-mention
    in the message above), that it is a send-time snapshot, and — for degraded
    snapshots — that the read tool is the way to the actual content.
    """
    path = block.get("path", "")
    status = block.get("status")

    if status == "inlined":
        return (
            f"[File mention: {path} — attached automatically because the user referenced "
            f"@{path} in the message above; content snapshot from send time]\n"
            f"{block.get('text') or ''}"
        )
    if status == "too_large":
        size_bytes = block.get("size_bytes")
        size_note = f" ({size_bytes:,} bytes)" if isinstance(size_bytes, int) else ""
        return (
            f"[File mention: {path}{size_note} — too large to attach inline; "
            f"read it with the read tool if needed]"
        )
    if status == "not_text":
        return f"[File mention: {path} — not a text file; read it with the read tool if needed]"
    return f"[File mention: {path} — the file did not exist when the user sent the message]"


def _snapshot_mention(
    mention: str, *, root: Path, session_id: str, file_state: FileReadState
) -> FileMentionBlock:
    resolved = _resolve_mention_path(root, mention)
    try:
        if not resolved.is_file():
            return _degraded(mention, "missing")
        size_bytes = resolved.stat().st_size
        if size_bytes > MENTION_INLINE_MAX_BYTES:
            return _degraded(mention, "too_large", size_bytes)
        raw = resolved.read_bytes()
    except OSError as error:
        _LOGGER.warning("Could not snapshot @-mentioned file %s: %s", resolved, error)
        return _degraded(mention, "missing")

    if not sniff_media_type(raw, resolved.name).startswith("text/"):
        return _degraded(mention, "not_text", len(raw))

    file_state.record_read(session_id, resolved)
    return FileMentionBlock(
        type="file_mention",
        path=mention,
        status="inlined",
        text=raw.decode("utf-8", errors="replace"),
        size_bytes=len(raw),
    )


def _resolve_mention_path(root: Path, mention: str) -> Path:
    # Same rule as tool path resolution: absolute paths stand alone, relative
    # paths resolve against the cwd — so the stamped path is byte-identical to
    # what a file Tool call on the same mention string resolves to.
    candidate = Path(mention).expanduser()
    if candidate.is_absolute():
        return candidate.resolve()
    return (root / candidate).resolve()


def _degraded(mention: str, status: str, size_bytes: int | None = None) -> FileMentionBlock:
    return FileMentionBlock(
        type="file_mention", path=mention, status=status, text=None, size_bytes=size_bytes
    )


__all__ = [
    "MENTION_FILE_LIST_LIMIT",
    "MENTION_FILE_LIST_TIMEOUT_SECONDS",
    "MENTION_INLINE_MAX_BYTES",
    "MentionDirectory",
    "MentionEntry",
    "MentionIndex",
    "expand_file_mentions",
    "file_mention_request_text",
    "list_mention_directory",
    "list_mention_files",
    "resolve_mention_root",
]
