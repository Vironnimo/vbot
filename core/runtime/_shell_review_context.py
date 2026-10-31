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
    tasks: list[str] = []
    automated: list[dict[str, Any]] = []
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
            peer = message.sender_kind == "session" or message.subagent_reply_target == "parent"
            automation = message.input_source_id is not None or (
                message.run_kind not in {None, "user", "channel", "live"}
                and message.subagent_reply_target != "user"
            )
            human = not peer and not automation
            if message.truncated:
                truncated |= not peer
                omitted = True
                continue
            if (
                len(message.text) > remaining
                or len(requests) + len(tasks) + len(automated) >= _MESSAGE_LIMIT
            ):
                omitted = True
                continue
            remaining -= len(message.text)
            text = masker.mask(message.text)
            if human:
                if message.sender_id is not None:
                    text = json.dumps(
                        {
                            "source": "channel",
                            "sender_id": message.sender_id,
                            "role": message.sender_role,
                            "session_id": address.session_id,
                            "text": text,
                        },
                        ensure_ascii=False,
                    )
                requests.append((message.timestamp, message.sequence, text))
            elif not peer and message.input_source_id is not None:
                tasks.append(
                    json.dumps(
                        {
                            "source": "authenticated_task_input",
                            "source_id": message.input_source_id,
                            "text": text,
                        },
                        ensure_ascii=False,
                    )
                )
            elif not peer and automation:
                automated.append(
                    {
                        "source": message.run_kind,
                        "schedule_id": message.schedule_id,
                        "agent_id": address.agent_id,
                        "project_id": address.project_id,
                        "text": text,
                    }
                )
            else:
                delegated.append(text)
        link = read_link(sessions, address, strict=True)
        if link is None:
            break
        address = link.parent
        expected_generation = link.parent_generation_id
    else:
        truncated = True
    return {
        "user_requests": [text for _, _, text in sorted(requests)],
        "authorized_tasks": tasks,
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
            "automated_requests": automated,
            "earlier_messages_omitted": omitted,
        },
    }
