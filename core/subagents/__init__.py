"""Sub-agent coordination domain."""

from core.subagents.catalog import SubAgentPromptTarget, build_subagent_prompt_targets
from core.subagents.subagents import (
    SUBAGENT_SESSION_STARTED_EVENT,
    SUBAGENT_STATUS_CHANGED_EVENT,
    SubAgentCoordinator,
)
from core.subagents.tracker import SubAgentBatchTracker

__all__ = [
    "SUBAGENT_SESSION_STARTED_EVENT",
    "SUBAGENT_STATUS_CHANGED_EVENT",
    "SubAgentBatchTracker",
    "SubAgentCoordinator",
    "SubAgentPromptTarget",
    "build_subagent_prompt_targets",
]
