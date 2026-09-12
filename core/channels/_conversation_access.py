"""Channel response eligibility and group sender authority."""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import replace
from typing import TYPE_CHECKING

from core.channels.adapter import (
    ChannelAccessRegistry,
    ConversationFacts,
)
from core.chat.messages import GroupRole, MessageSender

if TYPE_CHECKING:
    from core.channels.config import ChannelConfig

_MEMBER_TOOL_NAMES = ("web_search", "web_fetch")


_MEMBER_TOOL_DENIAL = (
    "Tool access denied: the current sender is a group member. "
    "Group members may use only web_search and web_fetch."
)


class ChannelAccessPolicy:
    """Resolve response eligibility and group sender authority at admission/dispatch."""

    def __init__(
        self, config: ChannelConfig, access_registry: ChannelAccessRegistry | None
    ) -> None:
        self._config = config
        self._access_registry = access_registry
        self._mention_patterns = tuple(
            re.compile(pattern, re.IGNORECASE) for pattern in config.mention_patterns
        )

    def should_respond(
        self,
        conversation: ConversationFacts,
        gating_texts: Sequence[str | None] = (),
    ) -> bool:
        """Decide whether one inbound non-command message may trigger a Run.

        Direct conversations always respond. Group conversations respond in
        ``response_mode: "all"``, or in ``"mention"`` mode when the message is addressed:
        platform bot mention, reply to a bot message, or a ``mention_patterns`` wake-word
        match against the supplied texts (message text or media captions).
        """
        if conversation.kind != "group":
            return True
        if self._config.response_mode == "all":
            return True
        if conversation.mentioned_bot or conversation.is_reply_to_bot:
            return True
        return self._matches_mention_patterns(gating_texts)

    def _matches_mention_patterns(self, gating_texts: Sequence[str | None]) -> bool:
        for text in gating_texts:
            if not isinstance(text, str):
                continue
            for pattern in self._mention_patterns:
                if pattern.search(text):
                    return True
        return False

    def _command_sender_authorized(self, conversation: ConversationFacts) -> bool:
        # DM commands retain their existing behavior. Group Commands use the same
        # immutable ingress role snapshot as messages and reserved Run buttons.
        if conversation.kind != "group":
            return True
        return conversation.sender_role == "admin"

    def _sender_for(self, conversation: ConversationFacts) -> MessageSender | None:
        # Sender identity is group-only in v1; DM turns stay unattributed.
        if conversation.kind != "group":
            return None
        return MessageSender(
            id=conversation.user_id,
            display_name=conversation.user_display_name or conversation.user_id,
            role=conversation.sender_role or "member",
        )

    def _snapshot_group_sender(self, conversation: ConversationFacts) -> ConversationFacts:
        """Persist and freeze one group sender's role at Channel ingress."""
        if conversation.kind != "group" or conversation.sender_role is not None:
            return conversation
        access_scope_id = conversation.access_scope_id or conversation.chat_id
        registry = self._access_registry
        role: GroupRole = (
            registry.snapshot_participant_role(
                self._config.id,
                access_scope_id,
                conversation.user_id,
                conversation.user_display_name or conversation.user_id,
            )
            if registry is not None
            else "member"
        )
        return replace(
            conversation,
            access_scope_id=access_scope_id,
            sender_role=role,
        )

    def _tool_access_for(
        self,
        conversation: ConversationFacts,
    ) -> tuple[Sequence[str] | None, Callable[[str], str | None] | None]:
        """Return the admission ceiling and live pre-dispatch denial resolver."""
        if conversation.kind != "group":
            return None, None
        admitted_role = conversation.sender_role or "member"
        access_scope_id = conversation.access_scope_id or conversation.chat_id
        user_id = conversation.user_id
        registry = self._access_registry

        def denial_resolver(tool_name: str) -> str | None:
            if tool_name in _MEMBER_TOOL_NAMES:
                return None
            current_role = (
                registry.role_for(self._config.id, access_scope_id, user_id)
                if registry is not None
                else admitted_role
            )
            if admitted_role != "admin" or current_role != "admin":
                return _MEMBER_TOOL_DENIAL
            return None

        restriction: Sequence[str] | None = (
            _MEMBER_TOOL_NAMES if admitted_role == "member" else None
        )
        return restriction, denial_resolver
