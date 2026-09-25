"""Channel Session anchors, active continuation pointers and metadata.

A conversation's derived anchor Session id is stable. ``/new``, origin-bound
Run taps and platform chat-id migrations move the conversation to another
Session by storing a pointer in the Channel state (the "Wegweiser"); without a
pointer the anchor itself is the active Session. Session metadata carries only
the Channel context of each Session.

The async entry points take one worker hop per database: pointer work runs on
the Channel state's own pool (``ConversationPointerStore.run_async``), Session
work on the Session database's pool (``ChatSessionManager.run_async``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from core.channels.adapter import (
    ConversationFacts,
    ConversationPointerStore,
    ReplyPlanFacts,
    RouteFacts,
    main_conversation_id,
)
from core.sessions import SessionAddress

if TYPE_CHECKING:
    from core.channels.config import ChannelConfig
    from core.sessions import ChatSessionManager


def _session_address(agent_id: str, session_id: str) -> SessionAddress:
    """Address one Channel Session (channels always run on Identity Agents)."""
    return SessionAddress(project_id=None, agent_id=agent_id, session_id=session_id)


class ChannelSessionRouting:
    """Own Channel conversation anchors, active Session pointers and metadata."""

    def __init__(
        self,
        config: ChannelConfig,
        chat_sessions: ChatSessionManager,
        pointers: ConversationPointerStore,
    ) -> None:
        self._config = config
        self._chat_sessions = chat_sessions
        self._pointers = pointers

    def prepare_inbound_route(
        self,
        conversation: ConversationFacts,
    ) -> tuple[RouteFacts, ReplyPlanFacts]:
        """Ensure the routed Session exists and refresh its channel metadata.

        A known conversation whose channel context is unchanged costs only reads;
        creation and a changed context commit together in one write.
        """
        route = self._route_facts(conversation)
        reply_plan = self._reply_plan_for(conversation)
        self._update_session_metadata(route, conversation, reply_plan, create_missing=True)
        return route, reply_plan

    async def _prepare_inbound_route_async(
        self,
        conversation: ConversationFacts,
    ) -> tuple[RouteFacts, ReplyPlanFacts]:
        route = await self._pointers.run_async(self._route_facts, conversation)
        reply_plan = self._reply_plan_for(conversation)
        await self._chat_sessions.run_async(
            self._update_session_metadata,
            route,
            conversation,
            reply_plan,
            create_missing=True,
        )
        return route, reply_plan

    def _reply_plan_for(self, conversation: ConversationFacts) -> ReplyPlanFacts:
        """Build a reply target without creating or changing a Session."""
        return ReplyPlanFacts(
            channel_id=self._config.id,
            platform_target=conversation.chat_id,
            # Group replies reference the triggering message so it is clear which
            # message the bot answers; DM replies stay plain.
            reply_to_message_id=(conversation.message_id if conversation.kind == "group" else None),
            # Replies follow the message into its thread/topic where the platform
            # models topics inside one chat (Telegram forum topics).
            thread_id=conversation.thread_id,
        )

    async def ensure_channel_session(self, conversation: ConversationFacts) -> RouteFacts:
        """Ensure the Session mirroring a conversation exists with channel context."""
        route = await self._pointers.run_async(self._route_facts, conversation)
        # Proactive (outbound-only) Sessions get the same channel metadata as inbound
        # ones, so a channel_send-created session is recognizable as a channel session and has
        # a last_reply_target before any inbound message arrives.
        await self._chat_sessions.run_async(
            self._update_session_metadata,
            route,
            conversation,
            ReplyPlanFacts(
                channel_id=self._config.id,
                platform_target=conversation.chat_id,
                thread_id=conversation.thread_id,
            ),
            create_missing=True,
        )
        return route

    def _route_facts(self, conversation: ConversationFacts) -> RouteFacts:
        # _derive_session_id yields the stable conversation anchor. The active
        # session may have been moved off that anchor by /new (the "Wegweiser"
        # pointer), so route through the pointer instead of straight to the anchor.
        conversation_key = self._derive_session_id(conversation)
        return RouteFacts(
            agent_id=self._config.agent_id,
            session_id=self._resolve_active_session_id(conversation_key),
        )

    def _resolve_active_session_id(self, conversation_key: str) -> str:
        """Follow a conversation anchor's pointer to its currently active session.

        With no pointer the anchor *is* the session. Single hop: the pointer
        always names the newest session directly. A deleted target is fine --
        routing re-creates it empty downstream, keeping the current conversation
        fresh rather than reviving old history.
        """
        active = self._pointers.active_session_id(self._config.id, conversation_key)
        return active or conversation_key

    async def migrate_group_conversation(self, old_chat_id: str, new_chat_id: str) -> bool:
        """Repoint a group conversation anchor after a platform chat-id migration.

        Some platforms change a group's chat id in place (Telegram: group →
        supergroup upgrade). The old anchor's active session keeps the full
        history; the new chat id's anchor points at it (single hop), the
        session's channel metadata moves to the new chat id (so proactive sends
        target the live chat), and a note tells the model.
        Returns False when the old conversation has no session to bridge.
        """
        agent_id = self._config.agent_id
        active_session_id = await self._pointers.run_async(
            self._resolve_active_session_id, self._group_conversation_key(old_chat_id)
        )
        address = _session_address(agent_id, active_session_id)
        if not await self._chat_sessions.run_async(self._chat_sessions.exists, address):
            return False

        await self._pointers.run_async(
            self._pointers.point_conversation,
            self._config.id,
            self._group_conversation_key(new_chat_id),
            "group",
            active_session_id,
        )
        conversation = ConversationFacts(
            platform=self._config.platform,
            channel_id=self._config.id,
            chat_id=new_chat_id,
            user_id=new_chat_id,
            access_scope_id=new_chat_id,
            kind="group",
        )
        await self._chat_sessions.run_async(
            self._update_session_metadata,
            RouteFacts(agent_id=agent_id, session_id=active_session_id),
            conversation,
            ReplyPlanFacts(channel_id=self._config.id, platform_target=new_chat_id),
        )
        async with self._chat_sessions.write_lock(address):
            await self._chat_sessions.run_async(
                self._append_session_note,
                agent_id,
                active_session_id,
                f"This group chat was migrated by the platform to a new chat id "
                f"(old: {old_chat_id}, new: {new_chat_id}). The conversation continues here.",
            )
        return True

    def _append_session_note(self, agent_id: str, session_id: str, note: str) -> None:
        session = self._chat_sessions.get_or_create(_session_address(agent_id, session_id))
        session.add_note(note)

    def _derive_session_id(self, conversation: ConversationFacts) -> str:
        # Group conversations share one session keyed by chat id and ignore dm_scope.
        if conversation.kind == "group":
            return self._group_conversation_key(conversation.chat_id)

        scope = self._config.dm_scope
        if scope == "main":
            return main_conversation_id(self._config.id)
        if scope == "per_peer":
            return f"ch-{self._config.id}-u{conversation.user_id}"
        if scope == "per_account_channel_peer":
            return f"ch-{self._config.id}-{conversation.chat_id}-u{conversation.user_id}"
        return f"ch-{self._config.id}-{conversation.chat_id}"

    def _group_conversation_key(self, chat_id: str) -> str:
        return f"ch-{self._config.id}-{chat_id}"

    def _update_session_metadata(
        self,
        route: RouteFacts,
        conversation: ConversationFacts,
        reply_plan: ReplyPlanFacts,
        *,
        create_missing: bool = False,
    ) -> None:
        """Re-assert the channel context; unchanged context is not rewritten."""
        address = _session_address(route.agent_id, route.session_id)
        last_reply_target: dict[str, Any] = {
            "channel_id": reply_plan.channel_id,
            "platform_target": reply_plan.platform_target,
        }
        # The thread key is present only while the conversation lives in a topic; a
        # later non-topic message rewrites the dict without it (last-target semantics).
        if reply_plan.thread_id is not None:
            last_reply_target["thread_id"] = reply_plan.thread_id

        def update(metadata: dict[str, Any]) -> None:
            metadata.update(
                {
                    "source_channel_id": self._config.id,
                    "platform": conversation.platform,
                    "platform_conv_id": conversation.chat_id,
                    "last_reply_target": last_reply_target,
                }
            )

        self._chat_sessions.ensure_metadata(address, update, create_missing=create_missing)

    async def _apply_continuation_navigation_async(
        self,
        route: RouteFacts,
        conversation: ConversationFacts,
        reply_plan: ReplyPlanFacts,
        conversation_key: str,
    ) -> None:
        await self._chat_sessions.run_async(
            self._update_session_metadata, route, conversation, reply_plan
        )
        await self._pointers.run_async(
            self._pointers.point_conversation,
            self._config.id,
            conversation_key,
            conversation.kind,
            route.session_id,
        )

    def _point_conversation_at_session(
        self,
        conversation: ConversationFacts,
        session_id: str,
    ) -> str | None:
        """Persist a new active Session and return the pointer it replaced."""
        return self._pointers.point_conversation(
            self._config.id,
            self._derive_session_id(conversation),
            conversation.kind,
            session_id,
        )

    def _restore_conversation_pointer(
        self,
        conversation: ConversationFacts,
        previous_session_id: str | None,
        *,
        expected_session_id: str,
    ) -> None:
        """Undo only this tap's pointer, preserving later navigation."""
        self._pointers.restore_conversation_pointer(
            self._config.id,
            self._derive_session_id(conversation),
            previous_session_id,
            expected_session_id=expected_session_id,
        )
