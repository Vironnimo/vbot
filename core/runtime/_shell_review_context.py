"""Bounded, provenance-preserving user authority for shell judgments."""

from __future__ import annotations

import json
from typing import Any, Literal

from core.automation.cron import CronJobNotFoundError
from core.runs import Run, RunKind, RunStatus
from core.secrets.agent_secrets import SecretMasker
from core.sessions import ChatSessionManager, SessionAddress
from core.subagents.links import read_link
from core.tools.tools import ToolContext

_MESSAGE_LIMIT = 32
_TEXT_BUDGET = 64000
_ANCESTOR_LIMIT = 64


async def current_input_authority(
    sessions: ChatSessionManager,
    context: ToolContext,
    *,
    run: Run | None,
    cron: Any = None,
    masker: SecretMasker | None = None,
) -> Literal["user", "authorized_task", "none"]:
    """Classify this Run's latest input, never older or ancestral authority.

    Unlike shell review evidence, this read is an ingress classification, not a
    judgment that conversation text authorizes some particular side effect.
    """
    address = SessionAddress(context.project_id, context.agent_id, context.session_id)
    if (
        run is None
        or run.id != context.run_id
        or (run.project_id, run.agent_id, run.session_id)
        != (address.project_id, address.agent_id, address.session_id)
        or run.status != RunStatus.RUNNING
        or run.cancel_requested
        or context.subagent_session
        or context.execution_owner is not None
        or run.execution_owner is not None
    ):
        return "none"
    session = await sessions.get_async(address)
    generation = session.generation_id
    if generation is None or run.expected_session_generation_id not in (None, generation):
        return "none"
    page = await sessions.run_async(
        sessions.conversation_page,
        address,
        expected_generation_id=generation,
        limit=1,
        max_text_chars=20_000,
    )
    latest = page.latest_input
    if latest is None or latest.run_id != run.id or latest.truncated:
        return "none"
    if latest.sender_kind == "session" or latest.subagent_reply_target == "parent":
        return "none"
    # Task inputs run as USER too. Their accepted receipt takes precedence over
    # direct_user_input, which deliberately also covers those ordinary Runs.
    if latest.input_source_id is not None:
        if not run.accessor_input_id:
            return "none"
        receipt = await sessions.lookup_accessor_input_async(run.accessor_input_id)
        return (
            "authorized_task"
            if receipt is not None
            and receipt.live
            and receipt.address == address
            and receipt.generation_id == generation
            and receipt.run_id == run.id
            and receipt.state == "started"
            and receipt.input_id.partition(":")[0] == latest.input_source_id
            else "none"
        )
    if context.direct_user_input:
        if latest.sender_id is not None and latest.sender_role != "admin":
            return "none"
        return "user"
    if run.run_kind != RunKind.CRON or not run.schedule_id or cron is None:
        return "none"
    try:
        job = cron.get_job(run.schedule_id)
    except CronJobNotFoundError:
        return "none"
    prompt = masker.mask(job.prompt) if masker is not None else job.prompt
    return (
        "authorized_task"
        if job.operator_approved
        and job.agent_id == context.agent_id
        and job.project_id == context.project_id
        and prompt == latest.text
        else "none"
    )


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
