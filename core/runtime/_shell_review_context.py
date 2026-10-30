"""Bounded, provenance-preserving user authority for shell judgments."""

from __future__ import annotations

import json
from typing import Any

from core.secrets.agent_secrets import SecretMasker
from core.sessions import ChatSessionManager, SessionAddress
from core.subagents.links import read_link
from core.tools.tools import ToolContext

_MESSAGE_LIMIT = 32
_TEXT_BUDGET = 64000
_ANCESTOR_LIMIT = 64


def shell_review_context(
    sessions: ChatSessionManager, context: ToolContext, masker: SecretMasker
) -> dict[str, Any]:
    """Read only canonical conversation text, retaining who actually supplied it.

    Parent instructions and peer messages are evidence, never human consent.
    A Sub-Agent also receives its live ancestors' human requests. Each query
    is generation checked and bounded in SQLite; no Tool output is hydrated.
    """
    address = SessionAddress(context.project_id, context.agent_id, context.session_id)
    seen: set[SessionAddress] = set()
    requests: list[tuple[str, int, str]] = []
    delegated: list[str] = []
    omitted = False
    truncated = False
    remaining = _TEXT_BUDGET
    expected_generation: str | None = None
    while address not in seen and len(seen) < _ANCESTOR_LIMIT:
        seen.add(address)
        session = sessions.get(address)
        generation = session.generation_id
        if generation is None or (expected_generation and generation != expected_generation):
            truncated = True
            break
        page = sessions.conversation_page(
            address,
            expected_generation_id=generation,
            limit=64,
            max_text_chars=20000,
        )
        omitted |= page.has_more
        for message in reversed(page.messages):
            if message.role != "user":
                continue
            human = message.sender_kind != "session" and message.subagent_reply_target != "parent"
            if message.truncated:
                truncated |= human
                omitted = True
                continue
            if len(message.text) > remaining or len(requests) >= _MESSAGE_LIMIT:
                omitted = True
                continue
            remaining -= len(message.text)
            if human:
                requests.append((message.timestamp, message.sequence, masker.mask(message.text)))
            else:
                delegated.append(masker.mask(message.text))
        link = read_link(sessions, address, strict=True)
        if link is None:
            break
        address = link.parent
        expected_generation = link.parent_generation_id
    else:
        truncated = True
    return {
        "user_requests": [text for _, _, text in sorted(requests)],
        "authority_truncated": truncated,
        "permissions": json.dumps(
            {
                "restricted": context.permissions.restricted,
                "read": context.permissions.allowed_summary("read"),
                "write": context.permissions.allowed_summary("write"),
                "command": context.permissions.allowed_summary("command"),
            },
            ensure_ascii=False,
        ),
        "evidence": {
            "delegated_requests": delegated,
            "earlier_messages_omitted": omitted,
        },
    }
