"""``@``-mention file support: cwd file listing and send-time snapshots.

The composer's ``@`` picker lists files under the session cwd (``files.list``
RPC). On send, every mentioned file is snapshotted once into a ``file_mention``
content block — content inline for reasonably sized text files — and stamped as
read in the session's read-before-write guard, so the agent can edit a mentioned
file without a separate read tool call. The snapshot is durable in the Session
JSONL: the model always sees the file as it was when the user sent the message,
and the stale-guard catches a later edit when the file changed afterwards.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from core.agents import default_workspace_dir
from core.attachments import sniff_media_type
from core.chat.content_blocks import ContentBlock, FileMentionBlock, TextBlock
from core.tools.search import SearchBudget, ignore_rules_apply, iter_search_entries
from core.utils.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from core.runtime.interfaces import RuntimeServices
    from core.tools.file_state import FileReadState

_LOGGER = get_logger("chat.file_mentions")

# Inline cap for a mentioned text file: larger files degrade to a reference note
# the agent can read selectively, so one @-mention cannot flood the context window.
MENTION_INLINE_MAX_BYTES = 128 * 1024

# Hard ceiling for the picker file list; the files.list response marks truncation.
MENTION_FILE_LIST_LIMIT = 5000

# Wall-clock budget for one listing walk. A tree that cannot be enumerated within
# this is too big for an interactive picker; the truncated prefix is returned.
MENTION_FILE_LIST_TIMEOUT_SECONDS = 5.0


def resolve_mention_root(runtime: RuntimeServices, agent_id: str, project_id: str | None) -> Path:
    """Resolve the directory ``@``-mentions work against for one chat address.

    Mirrors tool path resolution (``ToolContext.effective_cwd``): a project
    session uses the project's repo cwd, an identity session the agent's
    workspace — so the picker lists exactly the tree that relative tool paths
    resolve against.
    """
    if project_id is not None:
        return Path(runtime.projects.get(project_id).cwd)
    agent = runtime.agent_resolver.resolve_agent(None, agent_id)
    workspace = getattr(agent, "workspace", None)
    if workspace:
        return Path(workspace)
    return default_workspace_dir(runtime.storage.data_dir, agent_id)


def list_mention_files(root: Path) -> tuple[list[str], bool]:
    """List candidate files under ``root`` for the ``@`` picker.

    Returns ``(relative posix paths, truncated)``. The walk honors .gitignore
    (same walker and semantics as the glob/grep tools) and stops at the count
    cap or the wall-clock budget, marking either as truncation. A missing root
    lists as empty — a fresh workspace is a valid, empty picker source.
    """
    resolved_root = root.expanduser().resolve()
    if not resolved_root.is_dir():
        return [], False

    budget = SearchBudget(None, timeout_seconds=MENTION_FILE_LIST_TIMEOUT_SECONDS)
    files: list[str] = []
    truncated = False
    for path, _is_directory in iter_search_entries(
        resolved_root,
        budget=budget,
        apply_ignore_rules=ignore_rules_apply(resolved_root, include_ignored=False),
        include_directories=False,
    ):
        if len(files) >= MENTION_FILE_LIST_LIMIT:
            truncated = True
            break
        files.append(path.relative_to(resolved_root).as_posix())
    if budget.timed_out:
        truncated = True
    return files, truncated


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
    # what a read/edit call on the same mention string resolves to.
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
    "expand_file_mentions",
    "file_mention_request_text",
    "list_mention_files",
    "resolve_mention_root",
]
