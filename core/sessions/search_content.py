"""Canonical searchable Session content, shared by storage and Recall.

Sessions owns the persisted roles, derived-result exclusions and Content Block
projection. Recall chooses ranking/windowing and never changes this source text.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cached_property
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


def compact_text(text: str) -> str:
    """Collapse Unicode whitespace without changing the text's spelling."""
    return " ".join(text.split())


@dataclass(frozen=True)
class LiteralSearchQuery:
    """The literal text policy shared by canonical and derived Session searches.

    FTS expressions retain Unicode spelling for SQLite's tokenizer. Case folding
    belongs only to the literal candidate check; indexes choose their own tables,
    columns, candidate filters and ranking.
    """

    query: str
    match_mode: str = "all_terms"

    @cached_property
    def compact(self) -> str:
        return compact_text(self.query)

    @cached_property
    def values(self) -> tuple[str, ...]:
        if self.match_mode == "phrase":
            return (self.compact,) if self.compact else ()
        return tuple(self.compact.split())

    @cached_property
    def folded_values(self) -> tuple[str, ...]:
        return tuple(value.casefold() for value in self.values)

    def matches(self, text: str) -> bool:
        haystack = compact_text(text).casefold()
        if not haystack:
            return False
        matches = (value in haystack for value in self.folded_values)
        return any(matches) if self.match_mode == "any_term" else all(matches)

    @property
    def trigram_supported(self) -> bool:
        return bool(self.values) and all(len(value) >= 3 for value in self.values)

    @cached_property
    def fts_expression(self) -> str:
        operator = " OR " if self.match_mode == "any_term" else " AND "
        return operator.join('"' + value.replace('"', '""') + '"' for value in self.values)


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
