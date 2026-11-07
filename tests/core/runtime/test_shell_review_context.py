"""Current input authority is exact-Run provenance, not historical consent."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from core.chat import ChatMessage
from core.chat.messages import MessageSender
from core.runs import Run, RunKind
from core.runtime._shell_review_context import current_input_authority
from core.sessions import ChatSessionManager
from tests.core.extensions.extension_test_support import tool_context


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("user", "user"),
        ("task", "authorized_task"),
        ("cron", "authorized_task"),
        ("unapproved_cron", "none"),
        ("changed_cron", "none"),
        ("member", "none"),
        ("admin", "user"),
        ("peer", "none"),
        ("subagent", "none"),
        ("owned", "none"),
        ("system", "none"),
        ("earlier", "none"),
    ],
)
async def test_current_input_authority_only_uses_this_runs_trusted_input(
    tmp_path, source, expected
):
    sessions = ChatSessionManager(tmp_path)
    try:
        session = sessions.create(
            "main", metadata={"input_source_id": "feed"} if source == "task" else {}
        )
        session.append(ChatMessage.user("Older human authority"))
        run = Run(
            run_id="current-run",
            agent_id="main",
            session_id=session.id,
            run_kind=RunKind.CRON
            if "cron" in source
            else RunKind.SYSTEM
            if source == "system"
            else RunKind.USER,
            expected_session_generation_id=session.generation_id,
        )
        if "cron" in source:
            run.schedule_id = "schedule"
        if source == "task":
            run.accessor_input_id = "feed:request"
            await sessions.accept_accessor_input_async(
                session.address,
                run.accessor_input_id,
                request_hash="hash",
                payload={},
                queue_id="queued",
            )
        await sessions.start_run(run)
        sender = (
            MessageSender(
                "sender",
                "Sender",
                "admin" if source == "admin" else "member",
                kind="session" if source == "peer" else "user",
            )
            if source in {"member", "admin", "peer"}
            else None
        )
        if source != "earlier":
            session.for_run(run.id).append(ChatMessage.user("Current task", sender=sender))
        context = replace(
            tool_context("factory_card", tmp_path),
            agent_id="main",
            session_id=session.id,
            run_id=run.id,
            run_kind=run.run_kind,
            direct_user_input=source
            not in {"cron", "unapproved_cron", "changed_cron", "system", "earlier"},
            subagent_session=source == "subagent",
        )
        if source == "owned":
            context = replace(context, execution_owner=SimpleNamespace(extension="factory"))
        job = SimpleNamespace(
            operator_approved=source != "unapproved_cron",
            agent_id="main",
            project_id=None,
            prompt="Other task" if source == "changed_cron" else "Current task",
        )
        assert (
            await current_input_authority(
                sessions, context, run=run, cron=SimpleNamespace(get_job=lambda _: job)
            )
            == expected
        )
        if source == "task":
            # Human steering is a separate input, not another authenticated task.
            session.for_run(run.id).append(ChatMessage.user("Human follow-up"))
            assert await current_input_authority(sessions, context, run=run) == "user"
            assert (
                await current_input_authority(sessions, replace(context, run_id="other"), run=run)
                == "none"
            )
    finally:
        sessions.close()
