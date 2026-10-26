"""Canonical searchable Session content, shared by storage and Recall.

Sessions owns the persisted roles, derived-result exclusions and Content Block
projection. Recall chooses ranking/windowing and never changes this source text.
"""

from __future__ import annotations

from typing import Any

from core.chat.content_blocks import FileBlock, FileMentionBlock, MediaBlock, TextBlock

# Roles indexed by the Sessions-owned canonical search projection.
SESSION_RECALL_CONVERSATION_ROLES = (
    "user",
    "assistant",
    "tool",
    "error",
    "compaction_checkpoint",
)
# Ordinary recall searches conversation content and labeled summaries.
# Tool Results and operational errors require explicit lower-level diagnostic reads.
SESSION_RECALL_DEFAULT_ROLES = (
    "user",
    "assistant",
    "compaction_checkpoint",
)
# Names of the built-in recall tools whose results are persisted into sessions
# as ``role="tool"`` messages. Indexing or returning those results creates a
# feedback loop where every search matches its own prior output, so they are
# excluded from recall (the canonical scan and semantic
# index). Keep the retired ``session_read`` name for historical results too.
# Sessions owns this stored-history policy without importing Recall or the Tool layer.
RECALL_TOOL_RESULT_NAMES = frozenset({"session_search", "session_read"})


def is_recall_artifact_message(message: Any) -> bool:
    """True for a persisted Session Recall Tool result.

    Such a message is derived recall output, not conversation content. Indexing
    or returning it makes a search match its own prior results, so it is
    excluded from matches, context/bookends, and the semantic index.
    """

    return (
        getattr(message, "role", "") == "tool"
        and getattr(message, "name", None) in RECALL_TOOL_RESULT_NAMES
    )


def message_search_text(message: Any) -> str:
    return content_to_text(message.content)


def content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(content_block_to_text(block) for block in content)
    return ""


def content_block_to_text(block: Any) -> str:
    if isinstance(block, TextBlock):
        return block.text
    if isinstance(block, MediaBlock) and block.transcription is not None:
        return f"{block.filename} {block.media_type}\n{block.transcription}"
    if isinstance(block, (FileBlock, MediaBlock)):
        return f"{block.filename} {block.media_type}"
    if isinstance(block, FileMentionBlock):
        # Index the mentioned path only: the snapshot text is repo content, not
        # conversation content, and would bloat the recall index.
        return block.path
    return ""


def message_is_searchable(message: Any) -> bool:
    """Test canonical eligibility without materializing Content Block text."""
    return (
        getattr(message, "role", "") in SESSION_RECALL_CONVERSATION_ROLES
        and not getattr(message, "programmatic", False)
        and not is_recall_artifact_message(message)
        and bool(getattr(message, "content", None))
    )
