"""Cross-system contracts that do not belong to one Session owner."""

from __future__ import annotations

from pathlib import Path

from core.chat import ChatMessage
from core.sessions import ChatSessionManager, SessionAddress


def test_committed_message_survives_a_fresh_runtime_open(tmp_path: Path) -> None:
    address = SessionAddress(project_id=None, agent_id="agent", session_id="restart")
    sessions = ChatSessionManager(tmp_path)
    try:
        sessions.create("agent", session_id=address.session_id).append(ChatMessage.user("hello"))
    finally:
        sessions.close()

    reopened = ChatSessionManager(tmp_path)
    try:
        assert [message.content for message in reopened.get(address).load()] == ["hello"]
    finally:
        reopened.close()
