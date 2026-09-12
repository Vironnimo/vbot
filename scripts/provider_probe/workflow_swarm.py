"""Provider Tool probe: workflow swarm."""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any

from core.providers.tool_schema import render_tool_definitions
from core.tools.contracts import ToolContractError
from scripts.provider_probe.common import PROJECT_ROOT
from scripts.provider_probe.transport import _expected_profile


async def _probe_swarm_tool(adapter: Any, args: argparse.Namespace) -> dict[str, Any]:
    """Probe actual registered Swarm contracts and canonical receipt effects in isolation."""
    from core.agents.temporary import (
        TemporaryAgentConfig,
        TemporaryAgentRegistry,
        TemporaryExecutionGroups,
    )
    from core.chat import ChatMessage
    from core.extensions import ExtensionRegistry
    from core.extensions.extensions import purge_extension_modules
    from core.extensions.operations import ExtensionHost
    from core.runs import ChatRunManager, RunExecutionOwner
    from core.sessions import ChatSessionManager
    from core.sessions.format import write_bootstrap_marker
    from core.tools import ToolContext, ToolRegistry
    from core.tools.availability import ToolAccess

    tool_name = args.swarm_tool
    from resources.extensions.swarm.agent_text import DEFAULT_INSTRUCTIONS

    with TemporaryDirectory(prefix="vbot-swarm-probe-") as directory:
        root = Path(directory)
        write_bootstrap_marker(root)
        sessions = ChatSessionManager(root)
        manager = ChatRunManager()
        identity = SimpleNamespace(name="swarm", epoch="probe-registration")
        groups = TemporaryExecutionGroups(
            TemporaryAgentRegistry(sessions),
            None,
            lambda value: value is identity,
            identity,
            run_manager=manager,
        )
        extension_root = PROJECT_ROOT / "resources" / "extensions"
        # Runtime may have imported a different checkout's bundled package already.
        # Reload the isolated fixture from this script's own source tree.
        purge_extension_modules()
        extensions = ExtensionRegistry.load(
            root / "extensions",
            bundled_dir=extension_root,
            disabled={path.name for path in extension_root.iterdir() if path.name != "swarm"},
        )
        if not any(
            record.name == "swarm" and record.status == "loaded" for record in extensions.records()
        ):
            raise RuntimeError("Production Swarm package did not load")
        registry = ToolRegistry()
        extensions.apply_tools(registry)

        async def unavailable(*_arguments: Any) -> Any:
            raise RuntimeError("No Model Runs are admitted by the contract fixture")

        host = ExtensionHost(
            data_dir=root,
            state_dir=root,
            temporary_agents=groups,
            sample=unavailable,
            resolve_agent=lambda *_: None,
            store_attachment=lambda *_: None,
            resolve_credential=lambda *_: "",
            set_credential=lambda *_: None,
        )
        registered_handler: Any = registry.get("swarm_board").handler
        service = registered_handler.__self__
        await service.start(host)
        try:
            store = service.store
            profile = await store.save_profile(
                {
                    "schema_version": 1,
                    "slug": "probe",
                    "instructions": DEFAULT_INSTRUCTIONS,
                    "name": "Probe",
                    "participants": [{"model": "probe/model", "count": 3}],
                    "working_directory": {"kind": "directory", "path": directory},
                    "tool_access": {"mode": "selected", "allowed": []},
                },
                expected_revision=None,
            )
            started = await store.create_swarm(
                profile["id"],
                "probe goal",
                {"cwd": directory},
                request_id="start",
                expected_profile_revision=1,
            )
            swarm = await store.get_swarm(started["swarm_id"])
            sid = swarm["id"]
            bindings = []
            for row in swarm["participants"]:
                binding = await groups.create(
                    sid,
                    row["id"],
                    TemporaryAgentConfig(
                        model="probe/model",
                        cwd=root,
                        tool_access=ToolAccess(mode="selected", allowed=()),
                        allowed_skills=[],
                        tools={},
                        name=row["display_name"],
                    ),
                )
                await store.bind_participant_session(binding)
                bindings.append(binding)
            handle = await groups.open_group(sid)
            await store.bind_execution_epoch(sid, expected_epoch=0, execution_epoch=handle.epoch)
            binding = bindings[0]
            pid, peer = binding.participant_id, bindings[1].participant_id
            context = ToolContext(
                agent_id=binding.address.agent_id,
                session_id=binding.address.session_id,
                project_id=binding.address.project_id,
                run_id="probe-run",
                tool_call_id="probe",
                tool_name=tool_name,
                tool_call_index=0,
                workspace=root,
                vbot_root=root,
                data_root=root,
                session_tool_grants=tuple(tool.name for tool in registry.list_tools()),
                execution_owner=RunExecutionOwner(
                    extension="swarm",
                    group_id=sid,
                    participant_id=pid,
                    generation_id=binding.generation_id,
                    epoch=handle.epoch,
                ),
                delivery_receipt_hook=lambda *_: None,
                request_turn_end_hook=lambda *_: None,
            )
            topic = await store.create_discussion(
                sid, peer, title="Topic", text="opening", request_id="seed-topic"
            )
            disc = topic["discussion_id"]
            for index in range(3):
                await store.post(sid, peer, text=f"seed-{index}", request_id=f"seed-{index}")
            listed = await service.board(context, {"action": "list", "limit": 1})
            read = await service.board(context, {"action": "read", "limit": 1})
            main = swarm["main_discussion_id"]
            post = {"action": "post", "text": "probe contribution", "request_id": "post-default"}
            create = {
                "action": "create",
                "title": "New topic",
                "text": "Opening contribution",
                "request_id": "create-default",
            }
            cases: list[tuple[str, dict[str, Any], bool]] = [
                ("list_default", {"action": "list"}, True),
                ("list_one", {"action": "list", "limit": 1}, True),
                ("list_max", {"action": "list", "limit": 100}, True),
                ("list_cursor", listed["data"]["next_call"]["arguments"], True),
                ("read_default", {"action": "read"}, True),
                ("read_discussion", {"action": "read", "discussion_id": disc}, True),
                ("read_one", {"action": "read", "limit": 1}, True),
                ("read_max", {"action": "read", "limit": 100}, True),
                ("read_cursor", read["data"]["next_call"]["arguments"], True),
                ("read_message", {"action": "read", "message_id": topic["opening_post_id"]}, True),
                ("post_default", post, True),
                ("post_replay", post, True),
                ("post_conflict", {**post, "text": "changed"}, False),
                ("post_main", {**post, "discussion_id": main, "request_id": "main"}, True),
                ("post_empty_pings", {**post, "recipients": [], "request_id": "empty-pings"}, True),
                ("post_discussion", {**post, "discussion_id": disc, "request_id": "disc"}, True),
                (
                    "post_reply",
                    {
                        **post,
                        "discussion_id": disc,
                        "reply_to": topic["opening_post_id"],
                        "request_id": "reply",
                    },
                    True,
                ),
                ("post_ping", {**post, "recipients": [peer], "request_id": "ping"}, True),
                (
                    "post_duplicate_pings",
                    {**post, "recipients": [peer, peer], "request_id": "ping-duplicate"},
                    True,
                ),
                (
                    "post_nonmember_ping",
                    {
                        **post,
                        "discussion_id": disc,
                        "recipients": [bindings[2].participant_id],
                        "request_id": "nonmember",
                    },
                    True,
                ),
                ("post_unicode", {**post, "text": "Grüße 日本語", "request_id": "unicode"}, True),
                (
                    "reply_inferred",
                    {**post, "reply_to": topic["opening_post_id"], "request_id": "inferred"},
                    True,
                ),
                ("create", create, True),
                (
                    "create_empty_pings",
                    {**create, "recipients": [], "request_id": "create-empty"},
                    True,
                ),
                (
                    "create_ping",
                    {**create, "recipients": [peer, peer], "request_id": "create-ping"},
                    True,
                ),
                (
                    "create_ping_replay",
                    {**create, "recipients": [peer, peer], "request_id": "create-ping"},
                    True,
                ),
                (
                    "create_ping_conflict",
                    {**create, "recipients": [], "request_id": "create-ping"},
                    False,
                ),
                (
                    "create_foreign_ping",
                    {**create, "recipients": ["foreign"], "request_id": "create-foreign"},
                    False,
                ),
                ("create_replay", create, True),
                ("join", {"action": "join", "discussion_id": disc}, True),
                ("join_replay", {"action": "join", "discussion_id": disc}, True),
                ("leave", {"action": "leave", "discussion_id": disc}, True),
                ("leave_replay", {"action": "leave", "discussion_id": disc}, True),
                ("leave_main", {"action": "leave", "discussion_id": main}, False),
                (
                    "foreign_recipient",
                    {**post, "request_id": "foreign", "recipients": ["foreign"]},
                    False,
                ),
                ("foreign_discussion", {"action": "read", "discussion_id": "foreign"}, False),
                ("foreign_message", {"action": "read", "message_id": "foreign"}, False),
                (
                    "foreign_cursor",
                    {
                        "action": "read",
                        "cursor": listed["data"]["next_call"]["arguments"]["cursor"],
                    },
                    False,
                ),
                ("invalid_cursor", {"action": "list", "cursor": "invalid"}, False),
                (
                    "reply_mismatch",
                    {
                        **post,
                        "discussion_id": main,
                        "reply_to": topic["opening_post_id"],
                        "request_id": "mismatch",
                    },
                    False,
                ),
            ]
            invalids = [
                {},
                {"action": "unsupported"},
                {"action": "list", "swarm_id": sid},
                {"action": "list", "sender": pid},
                {"action": "read", "text": "wrong"},
                {"action": "list", "limit": True},
                {"action": "list", "limit": "1"},
                {"action": "list", "limit": 0},
                {"action": "list", "limit": 101},
                {"action": "read", "message_id": topic["opening_post_id"], "limit": 20},
                {"action": "read", "cursor": None},
                {"action": "post", "text": "missing request"},
                {"action": "post", "request_id": "missing-text"},
                {**post, "text": "", "request_id": "empty"},
                {"action": "create", "text": "missing title", "request_id": "missing-title"},
                {"action": "join"},
                {"action": "leave"},
            ]
            cases.extend((f"invalid_{index}", value, False) for index, value in enumerate(invalids))
            cases.extend(
                [
                    (
                        "bounds_post_max",
                        {**post, "text": "x" * 16000, "request_id": "max-text"},
                        True,
                    ),
                    (
                        "bounds_post_over",
                        {**post, "text": "x" * 16001, "request_id": "over-text"},
                        False,
                    ),
                    (
                        "bounds_title_max",
                        {**create, "title": "x" * 120, "request_id": "max-title"},
                        True,
                    ),
                    (
                        "bounds_title_over",
                        {**create, "title": "x" * 121, "request_id": "over-title"},
                        False,
                    ),
                    ("bounds_request_max", {**post, "request_id": "x" * 128}, True),
                    ("bounds_request_over", {**post, "request_id": "x" * 129}, False),
                    (
                        "bounds_title_empty",
                        {**create, "title": "", "request_id": "empty-title"},
                        False,
                    ),
                    ("bounds_request_empty", {**post, "request_id": ""}, False),
                ]
            )
            if tool_name == "swarm_inbox":
                for index in range(25):
                    await store.post(
                        sid, peer, text=f"backlog-{index}", request_id=f"backlog-{index}"
                    )
                cases = [
                    ("receive_one", {"limit": 1}, True),
                    ("receive_continuation", {"limit": 1}, True),
                    ("receive_default", {}, True),
                    ("receive_max", {"limit": 100}, True),
                    ("receive_empty", {}, True),
                ]
                cases.extend(
                    (f"invalid_{index}", value, False)
                    for index, value in enumerate(
                        [
                            {"action": "receive"},
                            {"swarm_id": sid},
                            {"participant_id": pid},
                            {"cursor": "invented"},
                            {"limit": True},
                            {"limit": "1"},
                            {"limit": 0},
                            {"limit": 101},
                            {"limit": None},
                            {"limit": 1.5},
                        ]
                    )
                )
            if tool_name == "swarm_state":
                await store.record_run_started(sid, pid, run_id=context.run_id, expected_epoch=0)
                status = await store.participant_status(sid, pid, limit=1)
                cases = [
                    ("status_default", {}, True),
                    ("status_one", {"limit": 1}, True),
                    ("status_max", {"limit": 100}, True),
                    (
                        "status_cursor",
                        {"cursor": status["cursor"], "limit": 1},
                        True,
                    ),
                    (
                        "status_cursor_changed_detail",
                        {
                            "cursor": status["cursor"],
                            "limit": 1,
                            "include_summaries": True,
                        },
                        False,
                    ),
                    (
                        "status_cursor_changed_limit",
                        {
                            "cursor": status["cursor"],
                            "limit": 2,
                        },
                        False,
                    ),
                    ("name_rejected", {"action": "name", "name": "Analyst"}, False),
                    ("name_field_rejected", {"name": "Analyst"}, False),
                ]
                cases.extend(
                    (f"invalid_{index}", value, False)
                    for index, value in enumerate(
                        [
                            {"action": "status"},
                            {"action": "unsupported"},
                            {"swarm_id": sid},
                            {"participant_id": pid},
                            {"limit": True},
                            {"limit": "1"},
                            {"limit": 0},
                            {"limit": 101},
                            {"cursor": None},
                            {"cursor": "foreign"},
                            {"include_summaries": "true"},
                            {"include_summaries": None},
                            {"action": "wait", "include_summaries": True},
                            {"action": "name"},
                            {"action": "name", "name": "Another", "limit": 1},
                            {"action": "wait", "needs_user": "true"},
                            {"action": "wait", "needs_user": 1},
                            {"action": "wait", "needs_user": None},
                            {"action": "wait", "reason": None},
                            {"action": "wait", "summary": "inapplicable"},
                            {"action": "done"},
                            {"action": "done", "summary": ""},
                            {"action": "done", "summary": None},
                            {"action": "done", "summary": "done", "artifacts": "report.md"},
                            {"action": "done", "summary": "done", "artifacts": [123]},
                        ]
                    )
                )
            if args.swarm_case in {"workflow", "unassisted"}:
                return await _probe_swarm_workflow(
                    adapter, args, extensions, registry, service, sessions, context, binding, peer
                )
            definitions = registry.provider_definitions(
                [tool_name], session_grants=context.session_tool_grants
            )
            rendered = render_tool_definitions(definitions, profile=_expected_profile(args))
            strict_count = sum(item.get("strict") is True for item in rendered)
            rows = []
            for name, expected, should_succeed in cases:
                # Exact character-count copying is a resource-boundary diagnostic,
                # separate from the action/default/type invocation matrix. The
                # deterministic adapter test runs these through production dispatch.
                if args.swarm_case == "all" and name.startswith("bounds_"):
                    continue
                if (
                    args.swarm_case != "all"
                    and name != args.swarm_case
                    and not name.startswith(args.swarm_case + "_")
                ):
                    continue
                async with asyncio.timeout(args.total_timeout):
                    raw = await adapter.send(
                        [
                            {
                                "role": "system",
                                "content": "This is an isolated Tool contract conformance test. "
                                "Emit exactly one Tool Call with the supplied arguments, including "
                                "intentional invalid values. "
                                "Do not repair intentional test inputs. "
                                "Do not add optional fields.",
                            },
                            {
                                "role": "user",
                                "content": f"Call {tool_name} with this test input: "
                                + json.dumps(expected, ensure_ascii=False),
                            },
                        ],
                        model_id=args.model,
                        tools=definitions,
                        thinking_effort=args.thinking_effort,
                        max_tokens=args.max_tokens or 3500,
                    )
                response = adapter.normalize_response(raw, model_id=args.model)
                calls = response.get("tool_calls") or []
                valid_call = (
                    len(calls) == 1
                    and calls[0].get("name") == tool_name
                    and calls[0].get("arguments") == expected
                )
                success = False
                actual_code = None
                durable = True
                if valid_call:
                    call_context = replace(context, tool_call_id=f"{name}-{calls[0]['id']}")
                    try:
                        result = await registry.dispatch(
                            call_context, expected, allowed_tools=[tool_name]
                        )
                        success = result["ok"]
                        actual_code = (result.get("error") or {}).get("code")
                    except ToolContractError:
                        result = {
                            "ok": False,
                            "error": {"code": "invalid_arguments"},
                            "data": None,
                            "artifacts": [],
                        }
                        actual_code = "invalid_arguments"
                    receipts = call_context._delivery_receipts
                    if success and receipts:
                        durable = all(
                            [not await store.reconcile_delivery(receipt[0]) for receipt in receipts]
                        )
                        await sessions.append_messages_with_receipts_async(
                            binding.address,
                            generation_id=binding.generation_id,
                            owner_name="swarm",
                            messages=[
                                ChatMessage.tool(
                                    tool_call_id=call_context.tool_call_id,
                                    name=tool_name,
                                    content=json.dumps(result),
                                )
                            ],
                            receipts=[(0, *receipt, "tool") for receipt in receipts],
                        )
                        durable = durable and all(
                            [await store.reconcile_delivery(receipt[0]) for receipt in receipts]
                        )
                row = {
                    "case": name,
                    "model_call_valid": valid_call,
                    "runtime_ok": success,
                    "expected_ok": should_succeed,
                    "error_code": actual_code,
                    "durable_receipt_verified": durable,
                    "passed": valid_call and success == should_succeed and durable,
                }
                rows.append(row)
                print(json.dumps(row), flush=True)
            return {
                "scenario": "swarm_tool",
                "tool": tool_name,
                "model": args.model,
                "strict_true_tool_count": strict_count,
                "cases": rows,
                "passed": bool(rows) and not strict_count and all(row["passed"] for row in rows),
            }
        finally:
            await service.close()
            await manager.aclose()
            sessions.close()


async def _probe_swarm_workflow(
    adapter: Any,
    args: argparse.Namespace,
    extensions: Any,
    registry: Any,
    service: Any,
    sessions: Any,
    context: Any,
    binding: Any,
    peer: str,
) -> dict[str, Any]:
    """Evaluate first-use choices separately from exact-argument conformance.

    This disposable fixture uses production scoped wording, definitions, handlers,
    Board effects and canonical receipts. Actual Chat admission/terminal ownership
    is verified by the owning lifecycle integration tests, not simulated here.
    """
    from core.chat import ChatMessage
    from core.chat.wire_shaping import _notes_to_request_messages
    from resources.extensions.swarm.agent_text import RESUME_REMINDER

    names = ("swarm_board", "swarm_inbox", "swarm_state")
    definitions = registry.provider_definitions(names, session_grants=names)
    if {tool["name"] for tool in definitions} != set(names):
        raise RuntimeError("Fresh-participant evaluation requires the complete production Tool set")
    store = service.store
    sid, pid = binding.group_id, binding.participant_id
    profile = (await store.get_swarm(sid))["profile_snapshot"]
    context = replace(context, session_tool_grants=names)
    await store.record_run_started(sid, pid, run_id=context.run_id, expected_epoch=0)
    unassisted = args.swarm_case == "unassisted"
    peers = [row["id"] for row in (await store.get_swarm(sid))["participants"] if row["id"] != pid]
    if unassisted:
        for index, participant_id in enumerate(peers):
            await store.post(
                sid,
                participant_id,
                text="I suggest a short checklist covering factual accuracy, clarity, and "
                "completeness. I agree to that approach and can review your contribution.",
                request_id=f"unassisted-proposal-{index}",
            )
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": profile["instructions"]},
        {
            "role": "user",
            "content": (
                "Work with your peers to agree on a three-step checklist for reviewing a "
                "short text report. Incorporate their feedback and finish your contribution. "
                "Keep the checklist in this conversation; no files are needed."
            )
            if unassisted
            else "Prepare a three-step checklist for reviewing a short text report with "
            "your peers. Inspect the group and existing Board, introduce your approach, and "
            "join the existing Topic discussion. Create a review discussion containing your "
            "draft checklist and publicly ping a peer for feedback. Receive incoming messages "
            "while working. Yield once while awaiting feedback. When I resume your work, "
            "review the feedback and finish your contribution after receiving pending work. "
            "This task needs no filesystem changes.",
        },
    ]
    seen: set[tuple[str, str]] = set()
    rows: list[dict[str, Any]] = []
    receipts_verified = 0
    resumed = False
    finished = False
    feedback_ids: set[str] = set()
    published_ids: set[str] = set()
    feedback_received = False
    received_ids: set[str] = set()
    published_after_feedback = False
    for step in range(32):
        async with asyncio.timeout(args.total_timeout):
            raw = await adapter.send(
                messages,
                model_id=args.model,
                tools=definitions,
                thinking_effort=args.thinking_effort,
                max_tokens=args.max_tokens or 3500,
            )
        response = adapter.normalize_response(raw, model_id=args.model)
        calls = response.get("tool_calls") or []
        messages.append(
            {
                **{
                    key: response[key]
                    for key in ("content", "tool_calls", "reasoning", "reasoning_meta")
                    if key in response
                },
                "role": "assistant",
            }
        )
        if not calls:
            if not unassisted and not resumed:
                await store.post(
                    sid,
                    peer,
                    text="I reviewed the draft: check factual accuracy, clarity, and completeness. "
                    "The checklist is ready; no further changes are needed.",
                    request_id="workflow-feedback",
                )
                await store.reconcile_run_finished(
                    sid, pid, run_id=context.run_id, expected_epoch=0, outcome="completed"
                )
                context = replace(context, run_id="workflow-resumed")
                await store.record_run_started(sid, pid, run_id=context.run_id, expected_epoch=0)
                reminder = ChatMessage.note(RESUME_REMINDER)
                await sessions.append_messages_with_receipts_async(
                    binding.address,
                    generation_id=binding.generation_id,
                    owner_name="swarm",
                    messages=[reminder],
                    receipts=[],
                )
                messages.extend(_notes_to_request_messages([reminder]))
                resumed = True
                continue
            finished = True
            break
        carriers = []
        receipts: list[tuple[int, str, str, str, str]] = []
        for index, call in enumerate(calls):
            name, arguments = call["name"], call["arguments"]
            call_context = replace(
                context, tool_name=name, tool_call_id=call["id"], tool_call_index=index
            )
            result = await registry.dispatch(call_context, arguments, allowed_tools=names)
            carriers.append(
                ChatMessage.tool(tool_call_id=call["id"], name=name, content=json.dumps(result))
            )
            if result["ok"]:
                data = result["data"]
                received = {entry["id"] for entry in data.get("entries", [])}
                received.update(entry["id"] for entry in data.get("recent", {}).get("entries", []))
                received_ids.update(received)
                if feedback_ids and feedback_ids.issubset(received_ids):
                    feedback_received = True
                if name == "swarm_board" and arguments.get("action") in {"post", "create"}:
                    post_id = data.get("post_id", data.get("opening_post_id"))
                    if post_id and post_id not in published_ids:
                        published_ids.add(post_id)
                        published_after_feedback |= feedback_received
                seen.add((name, arguments.get("action", "")))
                receipts.extend(
                    (index, *receipt, "tool") for receipt in call_context._delivery_receipts
                )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "name": name,
                    "content": json.dumps(result),
                }
            )
            row = {
                "step": step,
                "tool": name,
                "action": arguments.get("action"),
                "ok": result["ok"],
                "error_code": (result.get("error") or {}).get("code"),
            }
            rows.append(row)
            print(json.dumps(row), flush=True)
        await sessions.append_messages_with_receipts_async(
            binding.address,
            generation_id=binding.generation_id,
            owner_name="swarm",
            messages=carriers,
            receipts=receipts,
        )
        for receipt in receipts:
            if not await store.reconcile_delivery(receipt[1]):
                raise RuntimeError(
                    "Workflow delivery did not reconcile against its canonical carrier"
                )
            receipts_verified += 1
        if unassisted and published_ids and not feedback_ids:
            for index, participant_id in enumerate(peers):
                feedback = await store.post(
                    sid,
                    participant_id,
                    text="I reviewed your contribution. Please make the checklist actionable: "
                    "verify consequential claims against sources, check whether the intended "
                    "reader can follow it, and check the report against the requested scope. "
                    "I agree to conclude once those checks appear in the final checklist.",
                    request_id=f"unassisted-feedback-{index}",
                )
                feedback_ids.add(feedback["post_id"])
    required = {
        ("swarm_state", ""),
        ("swarm_board", "post"),
        ("swarm_board", "create"),
        ("swarm_board", "join"),
        ("swarm_inbox", ""),
    }
    if unassisted:
        required = set()
    coordinated = feedback_received and published_after_feedback if unassisted else resumed
    strict_count = sum(
        item.get("strict") is True
        for item in render_tool_definitions(definitions, profile=_expected_profile(args))
    )
    return {
        "scenario": "swarm_unassisted" if unassisted else "swarm_workflow",
        "feedback_received": feedback_received,
        "published_after_feedback": published_after_feedback,
        "model": args.model,
        "strict_true_tool_count": strict_count,
        "calls": rows,
        "missing_actions": sorted(required - seen),
        "durable_receipts": receipts_verified,
        "resumed": resumed,
        "final_response_received": finished,
        "scope": "Model choices, Board effects and canonical carriers; "
        "actual Chat lifecycle is tested separately",
        "passed": required.issubset(seen)
        and coordinated
        and finished
        and receipts_verified > 0
        and strict_count == 0,
    }
