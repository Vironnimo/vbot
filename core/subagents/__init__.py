"""Sub-Agent coordination domain."""

from core.subagents._constants import SUBAGENT_SESSION_STARTED_EVENT, SUBAGENT_STATUS_CHANGED_EVENT
from core.subagents.catalog import SubAgentPromptTarget, build_subagent_prompt_targets
from core.subagents.subagents import SubAgentCoordinator

__all__ = [
    "SUBAGENT_SESSION_STARTED_EVENT",
    "SUBAGENT_STATUS_CHANGED_EVENT",
    "SubAgentCoordinator",
    "SubAgentPromptTarget",
    "build_subagent_prompt_targets",
]
