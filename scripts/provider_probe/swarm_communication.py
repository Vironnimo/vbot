"""Bounded continuation tasks with production Board delivery and durable effects."""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from core.chat import ChatMessage
from core.chat.messages import ToolCall
from core.chat.wire_shaping import _notes_to_request_messages
from core.extensions._declarations import SessionRequestContext
from core.providers.adapter import terminal_outcome_from_response
from core.tools import tool_failure
from core.tools.contracts import ToolContractError

COMMUNICATION_GOAL = (
    "Work with your peers to prepare a release note. Keep the release tag, validation "
    "result, and any remaining release blocker accurate as new evidence arrives. "
    "Keep the note in the shared conversation; no files are needed."
    " The release engineer supplied these verified validation facts: release tag "
    "ORCHID-73; smoke check CHECK-42 passed; no blockers were known at the time of that check."
)
PRIOR_CONTRIBUTION = (
    "Release note: tag ORCHID-73. The CHECK-42 smoke check passed. "
    "There are no known release blockers based on that result."
)
COMMUNICATION_CASES = {
    "communication_ack": "Confirmed, thank you. I acknowledge your confirmation. "
    "I have no new information or questions.",
    "communication_question": "Which release tag did you verify? I need the exact tag "
    "for the release announcement. Please tell me here.",
    "communication_refinement": "A new clean-install check failed with LOCK-19. "
    "The signed build cannot start after installation. Please correct the shared "
    "release note to reflect this blocker; the earlier smoke check alone is insufficient.",
}


def assess_communication(case: str, posts: list[dict[str, Any]], finished: bool) -> dict[str, Any]:
    """Inspect shared effects, independent of Tool shape/count or final wording."""
    first = "\n".join(row["text"] for row in posts if row["wake"] == 0)
    after_ack = [row for row in posts if row["wake"] > 0]
    if case == "communication_ack":
        useful = not posts
    elif case == "communication_question":
        useful = "ORCHID-73" in first
    else:
        useful = "LOCK-19" in first and "ORCHID-73" in first
    return {
        "requested_effect_observed": useful,
        "posts_after_trigger": len(posts) if case == "communication_ack" else len(after_ack),
        "passed": useful and not after_ack and finished,
    }


async def communication_trial(
    adapter: Any,
    args: argparse.Namespace,
    registry: Any,
    service: Any,
    sessions: Any,
    context: Any,
    binding: Any,
    peer: str,
) -> dict[str, Any]:
    """Replay a completed contribution, then observe two natural wake boundaries.

    The peer is scripted and never calls a Model. Every reply it supplies is retained.
    Actual Chat admission remains covered by lifecycle tests; the production owner
    prepares every automatic delivery and canonical Session receipts acknowledge it.
    """
    names = ("swarm_board", "swarm_inbox", "swarm_state", "swarm_wiki")
    definitions = registry.provider_definitions(names, session_grants=names)
    store = service.store
    sid, pid = binding.group_id, binding.participant_id
    swarm = await store.get_swarm(sid)
    context = replace(context, session_tool_grants=names)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": swarm["profile_snapshot"]["instructions"]},
        {"role": "user", "content": COMMUNICATION_GOAL},
        {"role": "assistant", "content": PRIOR_CONTRIBUTION},
    ]
    record: dict[str, Any] = {
        "case": args.swarm_case,
        "evaluation": "black_box_continuation",
        "fixture_seed": "verified_validation_facts",
        "model": args.model,
        "thinking_effort": args.thinking_effort,
        "max_tokens": args.max_tokens or 3500,
        "max_requests_per_wake": 4,
        "max_wakes": 2,
        "grading": "Required facts, durable posts and normal end; semantic outcome needs review.",
        "initial_messages": list(messages),
        "definitions": definitions,
        "responses": [],
        "calls": [],
        "posts": [],
        "deliveries": [],
        "wakes": [],
        "passed": False,
        "first_attempt_success": False,
    }
    started = time.monotonic()
    try:
        await store.record_run_started(sid, pid, run_id=context.run_id, expected_epoch=0)
        await store.post(sid, pid, text=PRIOR_CONTRIBUTION, request_id="prior-contribution")
        await store.reconcile_run_finished(
            sid, pid, run_id=context.run_id, expected_epoch=0, outcome="completed"
        )
        finished = False
        for wake in range(2):
            incoming = (
                COMMUNICATION_CASES[args.swarm_case]
                if wake == 0
                else ("Thanks, confirmed. I acknowledge your reply and have nothing new to add.")
            )
            await store.post(sid, peer, text=incoming, request_id=f"peer-{wake}")
            prepared = await store.prepare_wake(sid, pid, expected_epoch=0)
            record["wakes"].append({"wake": wake, "admitted": prepared["wake"], "peer": incoming})
            if not prepared["wake"]:
                raise RuntimeError("Default delivery did not wake the idle participant")
            context = replace(context, run_id=f"communication-{wake}")
            claim = await store.claim_wake(sid, pid, expected_epoch=0)
            await store.mark_wake_admitted(
                sid, pid, expected_epoch=0, run_id=context.run_id, boundary=claim["boundary"]
            )
            owner_context = SessionRequestContext(
                binding=binding,
                run_id=context.run_id,
                agent_id=context.agent_id,
                session_id=context.session_id,
                execution_owner=context.execution_owner,
            )
            finished = False
            for step in range(4):
                delivery = await service._before_request(owner_context)
                if delivery is not None:
                    notes = [ChatMessage.note(entry) for entry in delivery.entries]
                    await sessions.append_messages_with_receipts_async(
                        binding.address,
                        generation_id=binding.generation_id,
                        owner_name="swarm",
                        messages=notes,
                        receipts=[
                            (
                                0,
                                delivery.delivery_id,
                                delivery.content_hash,
                                delivery.effect_kind,
                                "note",
                            )
                        ],
                    )
                    await service._acknowledge_delivery(owner_context, delivery)
                    visible = _notes_to_request_messages(notes)
                    messages.extend(visible)
                    record["deliveries"].append({"wake": wake, "step": step, "messages": visible})
                async with asyncio.timeout(args.total_timeout):
                    raw = await adapter.send(
                        messages,
                        model_id=args.model,
                        tools=definitions,
                        thinking_effort=args.thinking_effort,
                        max_tokens=args.max_tokens or 3500,
                        **adapter.request_context_kwargs(
                            agent_id=context.agent_id, session_id=context.session_id
                        ),
                    )
                response = adapter.normalize_response(raw, model_id=args.model)
                record["responses"].append({"wake": wake, "step": step, "response": response})
                messages.append(
                    {
                        "role": "assistant",
                        **{
                            key: response[key]
                            for key in ("content", "tool_calls", "reasoning", "reasoning_meta")
                            if key in response
                        },
                    }
                )
                calls = response.get("tool_calls") or []
                if not calls:
                    finished = terminal_outcome_from_response(response) == "stop"
                    break
                assistant = ChatMessage.assistant(
                    model=args.model,
                    content=response.get("content"),
                    tool_calls=[ToolCall.from_dict(call) for call in calls],
                )
                await sessions.get(binding.address).append_async(assistant)
                carriers = []
                receipts: list[tuple[int, str, str, str, str]] = []
                for index, call in enumerate(calls):
                    call_context = replace(
                        context,
                        tool_name=call["name"],
                        tool_call_id=call["id"],
                        tool_call_index=index,
                        iteration_number=step + 1,
                    )
                    try:
                        result = await registry.dispatch(
                            call_context, call["arguments"], allowed_tools=names
                        )
                    except ToolContractError as error:
                        result = tool_failure("invalid_arguments", str(error))
                    record["calls"].append({"wake": wake, "step": step, **call, "result": result})
                    if result["ok"] and call["name"] == "swarm_board":
                        post_id = result["data"].get(
                            "post_id", result["data"].get("opening_post_id")
                        )
                        if post_id and post_id not in {row["id"] for row in record["posts"]}:
                            saved = await store.read_posts(sid, pid, message_id=post_id)
                            record["posts"].append(
                                {"wake": wake, "text": saved.entries[0]["text"], "id": post_id}
                            )
                    carriers.append(
                        ChatMessage.tool(
                            tool_call_id=call["id"], name=call["name"], content=json.dumps(result)
                        )
                    )
                    receipts.extend(
                        (index, *receipt, "tool") for receipt in call_context._delivery_receipts
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "name": call["name"],
                            "tool_call_id": call["id"],
                            "content": json.dumps(result),
                        }
                    )
                await sessions.append_messages_with_receipts_async(
                    binding.address,
                    generation_id=binding.generation_id,
                    owner_name="swarm",
                    assistant_message_id=assistant.id,
                    messages=carriers,
                    receipts=receipts,
                )
                for receipt in receipts:
                    if not await store.reconcile_delivery(receipt[1]):
                        raise RuntimeError("Tool delivery receipt did not reconcile")
            await store.reconcile_run_finished(
                sid,
                pid,
                run_id=context.run_id,
                expected_epoch=0,
                outcome="completed" if finished else "interrupted",
            )
            if not finished:
                break
            # A quiet initial acknowledgment already has the requested effect.
            if args.swarm_case == "communication_ack" and not record["posts"]:
                break
        record.update(assess_communication(args.swarm_case, record["posts"], finished))
        record["finished"] = finished
        record["first_attempt_success"] = record["passed"] and all(
            row["result"]["ok"] for row in record["calls"]
        )
    except Exception as error:
        record["exception"] = {"type": type(error).__name__, "message": str(error)}
    record["elapsed_seconds"] = round(time.monotonic() - started, 2)
    return record


async def communication_suite(adapter: Any, args: argparse.Namespace) -> dict[str, Any]:
    from scripts.provider_probe.workflow_swarm import _probe_swarm_tool

    cases = (
        list(COMMUNICATION_CASES)
        if args.swarm_case == "communication"
        else args.swarm_case.split(",")
    )
    if set(cases) - COMMUNICATION_CASES.keys() or not 1 <= args.repetitions <= 20:
        raise ValueError("Choose known communication cases and 1-20 repetitions")
    results: list[dict[str, Any]] = []
    planned = len(cases) * args.repetitions
    report_path = Path(args.swarm_report) if args.swarm_report else None
    report: dict[str, Any] = {
        "scenario": "swarm_communication",
        "evaluation": "black_box_continuation",
        "fixture_seed": "verified_validation_facts",
        "provider": args.provider,
        "connection": args.connection,
        "model": args.model,
        "thinking_effort": args.thinking_effort,
        "max_tokens": args.max_tokens or 3500,
        "max_requests_per_wake": 4,
        "max_wakes": 2,
        "planned_trials": planned,
        "passed": False,
        "results": results,
        "fixture_limits": (
            "Scripted peer; two wake boundaries with at most four Model requests each. "
            "Production automatic delivery and durable Board/Session effects; real Chat "
            "admission is covered separately. Factual sentinels check outcomes; semantic "
            "refinement needs transcript review."
        ),
    }
    if report_path:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    for repetition in range(1, args.repetitions + 1):
        for case in cases:
            selected = argparse.Namespace(**{**vars(args), "swarm_case": case})
            result = await _probe_swarm_tool(adapter, selected)
            result["repetition"] = repetition
            results.append(result)
            report["passed"] = len(results) == planned and all(row["passed"] for row in results)
            report["trials"] = len(results)
            report["outcome_successes"] = sum(row["passed"] for row in results)
            report["first_attempt_successes"] = sum(row["first_attempt_success"] for row in results)
            if report_path:
                report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report
