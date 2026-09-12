"""Channel Session anchors, active continuation pointers and metadata."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from core.channels.adapter import (
    ConversationFacts,
    ReplyPlanFacts,
    RouteFacts,
)
from core.chat.errors import ChatSessionError
from core.sessions import SessionAddress
from core.utils.workers import BoundedWorkerPool

if TYPE_CHECKING:
    from core.channels.config import ChannelConfig
    from core.sessions import ChatSession, ChatSessionManager

_CHANNEL_SESSION_WORKERS = BoundedWorkerPool(name="channel-session", max_workers=4)


# Metadata-sidecar key on a conversation anchor that points at the chat's currently
# active session (the "Wegweiser" pointer). Absent = the anchor itself is the session.
ACTIVE_SESSION_METADATA_KEY = "active_session_id"


def _session_address(agent_id: str, session_id: str) -> SessionAddress:
    """Address one Channel Session (channels always run on Identity Agents)."""
    return SessionAddress(project_id=None, agent_id=agent_id, session_id=session_id)


class ChannelSessionRouting:
    """Own Channel conversation anchors, active Session pointers and metadata."""

    def __init__(self, config: ChannelConfig, chat_sessions: ChatSessionManager) -> None:
        self._config = config
        self._chat_sessions = chat_sessions

    def prepare_inbound_route(
        self,
        conversation: ConversationFacts,
    ) -> tuple[RouteFacts, ReplyPlanFacts]:
        """Ensure the routed Session exists and refresh its channel metadata."""
        route, _session = self._ensure_channel_session(conversation)
        reply_plan = self._reply_plan_for(conversation)
        self._update_session_metadata(route, conversation, reply_plan)
        return route, reply_plan

    async def _prepare_inbound_route_async(
        self,
        conversation: ConversationFacts,
    ) -> tuple[RouteFacts, ReplyPlanFacts]:
        return await _CHANNEL_SESSION_WORKERS.run(self.prepare_inbound_route, conversation)

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

    def ensure_channel_session(self, conversation: ConversationFacts) -> RouteFacts:
        """Ensure the Session mirroring a conversation exists with channel context."""
        route, _session = self._ensure_channel_session(conversation)
        # Proactive (outbound-only) Sessions get the same channel metadata as inbound
        # ones, so a channel_send-created session is recognizable as a channel session and has
        # a last_reply_target before any inbound message arrives. No participant is recorded:
        # an outbound target has no real sender.
        self._update_session_metadata(
            route,
            conversation,
            ReplyPlanFacts(channel_id=self._config.id, platform_target=conversation.chat_id),
            track_participant=False,
        )
        return route

    def _ensure_channel_session(
        self,
        conversation: ConversationFacts,
    ) -> tuple[RouteFacts, ChatSession]:
        route = self._route_facts(conversation)
        session = self._chat_sessions.get_or_create(
            _session_address(route.agent_id, route.session_id)
        )
        return route, session

    def _route_facts(self, conversation: ConversationFacts) -> RouteFacts:
        # _derive_session_id yields the stable conversation anchor. The active
        # session may have been moved off that anchor by /new (the "Wegweiser"
        # pointer), so route through the pointer instead of straight to the anchor.
        conversation_key = self._derive_session_id(conversation)
        return RouteFacts(
            agent_id=self._config.agent_id,
            session_id=self._resolve_active_session_id(self._config.agent_id, conversation_key),
        )

    def _resolve_active_session_id(self, agent_id: str, conversation_key: str) -> str:
        """Follow a conversation anchor's pointer to its currently active session.

        ``/new`` stores an ``active_session_id`` pointer in the anchor's metadata
        sidecar and creates a fresh session as the live one. With no pointer the
        anchor *is* the session, so a channel that never ran ``/new`` routes
        exactly as before (no migration, no legacy branch — the default state).
        """
        try:
            metadata = self._chat_sessions.get_metadata(
                _session_address(agent_id, conversation_key)
            )
        except ChatSessionError:
            # Anchor session does not exist yet -> nothing has moved off it.
            return conversation_key
        active = metadata.get(ACTIVE_SESSION_METADATA_KEY)
        # Single hop: the pointer always names the newest session directly. A
        # deleted target is fine -- get_or_create re-creates it empty downstream,
        # keeping the current conversation fresh rather than reviving old history.
        if isinstance(active, str) and active:
            return active
        return conversation_key

    async def migrate_group_conversation(self, old_chat_id: str, new_chat_id: str) -> bool:
        """Repoint a group conversation anchor after a platform chat-id migration.

        Some platforms change a group's chat id in place (Telegram: group →
        supergroup upgrade). The old anchor's active session keeps the full
        history; the new chat id's anchor gets an ``active_session_id`` pointer at
        it (single hop), the session's channel sidecar moves to the new chat id
        (so proactive sends target the live chat), and a note tells the model.
        Returns False when the old conversation has no session to bridge.
        """
        prepared = await _CHANNEL_SESSION_WORKERS.run(
            self._prepare_group_migration,
            old_chat_id,
            new_chat_id,
        )
        if prepared is None:
            return False
        agent_id, active_session_id = prepared
        async with self._chat_sessions.write_lock(_session_address(agent_id, active_session_id)):
            await _CHANNEL_SESSION_WORKERS.run(
                self._append_session_note,
                agent_id,
                active_session_id,
                f"This group chat was migrated by the platform to a new chat id "
                f"(old: {old_chat_id}, new: {new_chat_id}). The conversation continues here.",
            )
        return True

    def _prepare_group_migration(
        self,
        old_chat_id: str,
        new_chat_id: str,
    ) -> tuple[str, str] | None:
        agent_id = self._config.agent_id
        old_anchor = self._group_conversation_key(old_chat_id)
        active_session_id = self._resolve_active_session_id(agent_id, old_anchor)
        if not self._chat_sessions.exists(_session_address(agent_id, active_session_id)):
            return None

        new_anchor = self._group_conversation_key(new_chat_id)
        self._set_active_session_pointer(agent_id, new_anchor, active_session_id)
        conversation = ConversationFacts(
            platform=self._config.platform,
            channel_id=self._config.id,
            chat_id=new_chat_id,
            user_id=new_chat_id,
            access_scope_id=new_chat_id,
            kind="group",
        )
        self._update_session_metadata(
            RouteFacts(agent_id=agent_id, session_id=active_session_id),
            conversation,
            ReplyPlanFacts(channel_id=self._config.id, platform_target=new_chat_id),
            track_participant=False,
        )
        return agent_id, active_session_id

    def _append_session_note(self, agent_id: str, session_id: str, note: str) -> None:
        session = self._chat_sessions.get_or_create(_session_address(agent_id, session_id))
        session.add_note(note)

    def _derive_session_id(self, conversation: ConversationFacts) -> str:
        # Group conversations share one session keyed by chat id and ignore dm_scope.
        if conversation.kind == "group":
            return self._group_conversation_key(conversation.chat_id)

        scope = self._config.dm_scope
        if scope == "main":
            return f"ch-{self._config.id}-main"
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
        track_participant: bool = True,
    ) -> None:
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
                    "conversation_kind": conversation.kind,
                    "last_reply_target": last_reply_target,
                }
            )
            if track_participant and conversation.kind == "group":
                participants = metadata.get("participants")
                if not isinstance(participants, dict):
                    participants = {}
                participants[conversation.user_id] = {
                    "display_name": conversation.user_display_name or conversation.user_id,
                    "last_seen_at": datetime.now(UTC).isoformat(),
                }
                metadata["participants"] = participants

        self._chat_sessions.mutate_metadata(address, update)

    def _apply_continuation_navigation(
        self,
        route: RouteFacts,
        conversation: ConversationFacts,
        reply_plan: ReplyPlanFacts,
        conversation_key: str,
    ) -> None:
        self._update_session_metadata(
            route,
            conversation,
            reply_plan,
            track_participant=False,
        )
        self._set_active_session_pointer(
            route.agent_id,
            conversation_key,
            route.session_id,
        )

    def _set_active_session_pointer(self, agent_id: str, anchor: str, new_session_id: str) -> None:
        """Point the conversation anchor at the newest session (single hop).

        Read-modify-write on the anchor's sidecar so the anchor's other channel
        metadata is preserved. The anchor normally already exists (it was the
        active session); the get_or_create is a defensive floor for the rare case
        where it does not.
        """
        address = _session_address(agent_id, anchor)
        self._chat_sessions.get_or_create(address)
        self._chat_sessions.mutate_metadata(
            address,
            lambda metadata: metadata.__setitem__(ACTIVE_SESSION_METADATA_KEY, new_session_id),
        )

    def _point_conversation_at_session(
        self,
        conversation: ConversationFacts,
        session_id: str,
    ) -> dict[str, Any]:
        """Persist a new active Session and return the exact prior anchor metadata."""
        anchor = self._derive_session_id(conversation)
        address = _session_address(self._config.agent_id, anchor)
        self._chat_sessions.get_or_create(address)
        previous, _updated = self._chat_sessions.mutate_metadata_with_previous(
            address,
            lambda metadata: metadata.__setitem__(ACTIVE_SESSION_METADATA_KEY, session_id),
        )
        return previous

    def _restore_conversation_pointer(
        self,
        conversation: ConversationFacts,
        metadata: dict[str, Any],
    ) -> None:
        """Restore the anchor when a bound tap could not enter the Queue."""
        anchor = self._derive_session_id(conversation)
        self._chat_sessions.set_metadata(_session_address(self._config.agent_id, anchor), metadata)
