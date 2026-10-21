"""Fold prompt-epoch inputs and finalize checkpoint context."""

from __future__ import annotations

from typing import Any

from core.chat._effort_epoch import (
    EffortPin,
    EffortState,
    without_other_epoch_effort_changes,
)
from core.chat._message_history import finalize_checkpoint_guidance
from core.chat._prompt_block_epoch import (
    PromptBlockPin,
    without_other_epoch_prompt_block_changes,
)
from core.chat._scratch_context import latest_goal_note_id, preserve_notes
from core.chat._tool_epoch import (
    ToolEpochPin,
    ToolEpochView,
    without_other_epoch_tool_changes,
)
from core.chat.messages import (
    ChatMessage,
)
from core.tools import (
    SUBAGENT_SESSION_TOOL_NAMES,
    UPDATE_GOAL_TOOL_NAME,
)
from core.tools.model_names import SUBAGENT_TOOL_NAME


def _finalize_compaction_checkpoint(
    checkpoint: ChatMessage,
    session_messages: list[ChatMessage],
    scratch_note_id: object,
    goal_open: bool,
) -> ChatMessage:
    ordinal = sum(message.role == "compaction_checkpoint" for message in session_messages) + 1
    kept_note_ids = [scratch_note_id]
    if goal_open:
        kept_note_ids.append(latest_goal_note_id(session_messages))
    return preserve_notes(
        finalize_checkpoint_guidance(checkpoint, ordinal=ordinal), session_messages, kept_note_ids
    )


def _live_session_tool_grants(
    session_capability: Any | None, *, subagent_session: bool, goal_session: bool
) -> tuple[str, ...]:
    """Return the session-scoped Tools the Session grants now.

    A temporary Session grants its capability's Tools; a Sub-Agent Session grants
    the Tools a Sub-Agent uses to reach its Parent Agent; a Session with an open
    goal grants the Tool that ends its automatic turns.
    """
    grants = tuple(session_capability.tool_names) if session_capability is not None else ()
    if subagent_session:
        grants = (*grants, *SUBAGENT_SESSION_TOOL_NAMES)
    if goal_session:
        grants = (*grants, UPDATE_GOAL_TOOL_NAME)
    return grants


def _withheld_session_tools(*, subagent_tool_withheld: bool) -> tuple[str, ...]:
    """Return the Tools the Session leaves out of its Agent's Tool list now.

    A Sub-Agent Session at the nesting limit has no use for ``subagent``: there
    it could only start a Sub-Agent, which is refused.
    """
    return (SUBAGENT_TOOL_NAME,) if subagent_tool_withheld else ()


def _fold_prompt_epoch(
    pin: ToolEpochPin,
    prompt_blocks: PromptBlockPin,
    effort: EffortPin,
    session_messages: list[ChatMessage],
) -> tuple[ToolEpochView, EffortState, list[ChatMessage]]:
    """Return the epoch's Tool and effort state and the history without other epochs' notes.

    They walk the whole Session, so they run on a Chat worker.
    """
    messages = without_other_epoch_effort_changes(
        without_other_epoch_prompt_block_changes(
            without_other_epoch_tool_changes(session_messages, pin.epoch), prompt_blocks.epoch
        ),
        effort.epoch,
    )
    return ToolEpochView.fold(pin, messages), EffortState.fold(effort, messages), messages
