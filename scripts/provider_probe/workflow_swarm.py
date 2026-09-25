"""Provider Tool probe: workflow swarm."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast

from core.chat.messages import ToolCall
from core.providers.tool_schema import render_tool_definitions
from core.tools import tool_failure
from core.tools.contracts import ToolContractError
from scripts.provider_probe.common import PROJECT_ROOT
from scripts.provider_probe.transport import _expected_profile

_POST_HEADER = re.compile(r"^\[(pst_[A-Za-z0-9]+)\]", re.MULTILINE)


def _continuation(line: str) -> dict[str, Any]:
    """Return the copyable argument object that ends a Swarm result line."""

    return cast(dict[str, Any], json.loads(line[line.index("{") :]))


def _received_post_ids(data: dict[str, Any]) -> set[str]:
    """Return the post IDs a successful Swarm result showed the Agent."""

    received = {entry["id"] for entry in data.get("entries", []) if "id" in entry}
    content = data.get("content")
    if isinstance(content, str):
        received.update(_POST_HEADER.findall(content))
    return received


async def _probe_swarm_tool(adapter: Any, args: argparse.Namespace) -> dict[str, Any]:
    """Probe actual registered Swarm contracts and canonical receipt effects in isolation."""
    from core.agents.temporary import (
        TemporaryAgentConfig,
        TemporaryAgentRegistry,
        TemporaryExecutionGroups,
    )
    from core.chat import ChatMessage
    from core.database import write_bootstrap_marker
    from core.extensions import ExtensionRegistrationIdentity, ExtensionRegistry
    from core.extensions.databases import ExtensionDatabases
    from core.extensions.extensions import purge_extension_modules
    from core.extensions.operations import ExtensionHost
    from core.runs import ChatRunManager, RunExecutionOwner
    from core.sessions import ChatSessionManager
    from core.tools import ToolContext, ToolRegistry
    from core.tools.availability import ToolAccess

    tool_name = args.swarm_tool
    from resources.extensions.swarm.agent_text import DEFAULT_INSTRUCTIONS

    with TemporaryDirectory(prefix="vbot-swarm-probe-") as directory:
        root = Path(directory)
        write_bootstrap_marker(root)
        sessions = ChatSessionManager(root)
        manager = ChatRunManager()
        identity = ExtensionRegistrationIdentity("swarm", "probe-registration")
        databases = ExtensionDatabases(root)
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
            open_database=databases.opener(identity),
            sample=unavailable,
            resolve_agent=lambda *_: None,
            resolve_tool_agent=lambda context: None,
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
                "Work with your peers on a three-step checklist for reviewing a short text report. "
                "Keep the checklist in the conversation; no files are needed."
                if args.swarm_case == "unassisted"
                else "probe goal",
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
            list_more = _continuation(listed["data"]["more"])
            read_older = _continuation(read["data"]["older"])
            peer_name = next(
                row["display_name"] for row in swarm["participants"] if row["id"] == peer
            )
            post = {"action": "post", "text": "probe contribution"}
            create = {
                "action": "create",
                "title": "New topic",
                "text": "Opening contribution",
            }
            cases: list[tuple[str, dict[str, Any], bool]] = [
                ("list_default", {"action": "list"}, True),
                ("list_one", {"action": "list", "limit": 1}, True),
                ("list_max", {"action": "list", "limit": 100}, True),
                ("list_cursor", list_more, True),
                ("read_default", {"action": "read"}, True),
                ("read_discussion", {"action": "read", "discussion_id": disc}, True),
                ("read_one", {"action": "read", "limit": 1}, True),
                ("read_max", {"action": "read", "limit": 100}, True),
                ("read_before", read_older, True),
                ("read_message", {"action": "read", "message_id": topic["opening_post_id"]}, True),
                ("post_default", post, True),
                ("post_repeat", post, True),
                ("post_changed", {**post, "text": "changed"}, True),
                ("post_main", {**post, "discussion_id": main}, True),
                ("post_empty_pings", {**post, "recipients": []}, True),
                ("post_discussion", {**post, "discussion_id": disc}, True),
                (
                    "post_reply",
                    {
                        **post,
                        "discussion_id": disc,
                        "reply_to": topic["opening_post_id"],
                    },
                    True,
                ),
                ("post_ping", {**post, "recipients": [peer]}, True),
                (
                    "post_duplicate_pings",
                    {**post, "recipients": [peer, peer]},
                    True,
                ),
                (
                    "post_nonmember_ping",
                    {
                        **post,
                        "discussion_id": disc,
                        "recipients": [bindings[2].participant_id],
                    },
                    True,
                ),
                (
                    "post_unicode",
                    {**post, "text": "GrÃ¼ÃŸe æ—¥æœ¬èªž"},
                    True,
                ),
                (
                    "reply_inferred",
                    {**post, "reply_to": topic["opening_post_id"]},
                    True,
                ),
                ("create", create, True),
                (
                    "create_empty_pings",
                    {**create, "recipients": []},
                    True,
                ),
                (
                    "create_ping",
                    {**create, "recipients": [peer, peer]},
                    True,
                ),
                (
                    "create_ping_repeat",
                    {**create, "recipients": [peer, peer]},
                    True,
                ),
                (
                    "create_changed_pings",
                    {**create, "recipients": []},
                    True,
                ),
                (
                    "create_foreign_ping",
                    {**create, "recipients": ["foreign"]},
                    False,
                ),
                ("create_repeat", create, True),
                ("join", {"action": "join", "discussion_id": disc}, True),
                ("join_replay", {"action": "join", "discussion_id": disc}, True),
                ("leave", {"action": "leave", "discussion_id": disc}, True),
                ("leave_replay", {"action": "leave", "discussion_id": disc}, True),
                ("leave_main", {"action": "leave", "discussion_id": main}, False),
                (
                    "foreign_recipient",
                    {**post, "recipients": ["foreign"]},
                    False,
                ),
                ("foreign_discussion", {"action": "read", "discussion_id": "foreign"}, False),
                ("foreign_message", {"action": "read", "message_id": "foreign"}, False),
                (
                    "foreign_cursor",
                    {
                        "action": "read",
                        "cursor": list_more["cursor"],
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
                    },
                    False,
                ),
            ]
            invalids = [
                {},
                {"action": "unsupported"},
                {"action": "list", "swarm_id": "foreign"},
                {"action": "list", "sender": "foreign"},
                {"action": "read", "text": "wrong"},
                {"action": "list", "limit": True},
                {"action": "list", "limit": 0},
                {"action": "post"},
                {**post, "text": ""},
                {"action": "create", "text": "missing title"},
                {"action": "join"},
                {"action": "leave"},
            ]
            cases.extend(
                [
                    ("recovered_count", {"action": "list", "limit": "1"}, True),
                    ("recovered_scope", {"action": "list", "swarm_id": sid, "sender": pid}, True),
                    ("recovered_null_cursor", {"action": "read", "cursor": None}, True),
                    ("recovered_wrapper", {"request": {"operation": "LIST", "limti": "1.0"}}, True),
                    ("recovered_limit_clamp", {"action": "list", "limit": 101}, True),
                    (
                        "recovered_message_limit",
                        {"action": "read", "message_id": topic["opening_post_id"], "limit": 20},
                        True,
                    ),
                    ("recovered_inferred_post", {"text": "inferred contribution"}, True),
                    ("recovered_action_synonym", {"action": "send", "message": "hello"}, True),
                    ("recovered_name_ping", {**post, "recipients": [peer_name]}, True),
                    ("recovered_all_ping", {**post, "recipients": ["all"]}, True),
                    ("recovered_post_number", {"action": "read", "message_id": "pst_1"}, True),
                    ("reply_post_number", {**post, "reply_to": "pst_1"}, False),
                    ("foreign_action", {"action": "status"}, False),
                ]
            )
            cases.extend(
                (f"invalid_{index}", cast(dict[str, Any], value), False)
                for index, value in enumerate(invalids)
            )
            cases.extend(
                [
                    (
                        "bounds_post_max",
                        {**post, "text": "x" * 16000},
                        True,
                    ),
                    (
                        "bounds_post_over",
                        {**post, "text": "x" * 16001},
                        False,
                    ),
                    (
                        "bounds_title_max",
                        {**create, "title": "x" * 120},
                        True,
                    ),
                    (
                        "bounds_title_over",
                        {**create, "title": "x" * 121},
                        False,
                    ),
                    (
                        "bounds_title_empty",
                        {**create, "title": ""},
                        False,
                    ),
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
                    ("recovered_count", {"limit": "1"}, True),
                    ("recovered_null_limit", {"limit": None}, True),
                    ("recovered_action", {"action": "receive"}, True),
                    ("recovered_wrapper", {"receive": {"limti": "1"}}, True),
                    ("recovered_scope", {"swarm_id": sid, "participant_id": pid}, True),
                ]
                cases.extend(
                    (f"invalid_{index}", cast(dict[str, Any], value), False)
                    for index, value in enumerate(
                        [
                            {"swarm_id": "foreign"},
                            {"participant_id": "foreign"},
                            {"cursor": "invented"},
                            {"limit": True},
                            {"limit": 0},
                            {"limit": 101},
                            {"limit": 1.5},
                        ]
                    )
                )
            if tool_name == "swarm_state":
                await store.record_run_started(sid, pid, run_id=context.run_id, expected_epoch=0)
                status = await store.participant_status(sid, pid, limit=1)
                cases = [
                    ("status_default", {}, True),
                    ("recovered_count", {"limit": "1"}, True),
                    ("recovered_null_cursor", {"cursor": None}, True),
                    ("recovered_action", {"action": "status"}, True),
                    ("recovered_wrapper", {"request": {"operation": "STATUS", "limit": "1"}}, True),
                    ("recovered_scope", {"swarm_id": sid, "participant_id": pid}, True),
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
                    # An empty value under a retired name requests nothing.
                    ("recovered_empty_unknown", {"include_summaries": None}, True),
                ]
                cases.extend(
                    (f"invalid_{index}", cast(dict[str, Any], value), False)
                    for index, value in enumerate(
                        [
                            {"action": "unsupported"},
                            {"swarm_id": "foreign"},
                            {"participant_id": "foreign"},
                            {"limit": True},
                            {"limit": 0},
                            {"limit": 101},
                            {"cursor": "foreign"},
                            {"include_summaries": "true"},
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
            if tool_name == "swarm_wiki":
                from scripts.provider_probe.swarm_wiki_cases import wiki_cases

                cases = await wiki_cases(store, sid, pid)
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
                valid_call = len(calls) == 1 and calls[0].get("name") == tool_name
                if valid_call:
                    # Conformance input is independently prescribed, including invalid
                    # values. Do not derive the oracle through runtime repair.
                    valid_call = json.dumps(
                        calls[0].get("arguments"), sort_keys=True
                    ) == json.dumps(expected, sort_keys=True)
                success = False
                actual_code = None
                durable = True
                if valid_call:
                    call_context = replace(context, tool_call_id=f"{name}-{calls[0]['id']}")
                    try:
                        result = await registry.dispatch(
                            call_context, calls[0]["arguments"], allowed_tools=[tool_name]
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
                    if tool_name == "swarm_wiki":
                        from scripts.provider_probe.swarm_wiki_cases import verify_wiki_effect

                        durable = await verify_wiki_effect(store, sid, pid, name, result)
                    receipts = call_context._delivery_receipts
                    if success and receipts:
                        assistant = ChatMessage.assistant(
                            model=args.model,
                            content=None,
                            tool_calls=[ToolCall(id=call_context.tool_call_id, name=tool_name)],
                        )
                        await sessions.get(binding.address).append_async(assistant)
                        durable = all(
                            [not await store.reconcile_delivery(receipt[0]) for receipt in receipts]
                        )
                        await sessions.append_messages_with_receipts_async(
                            binding.address,
                            generation_id=binding.generation_id,
                            owner_name="swarm",
                            assistant_message_id=assistant.id,
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
                    "arguments": calls[0].get("arguments") if len(calls) == 1 else None,
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
            databases.close()
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
    from resources.extensions.swarm.agent_text import INITIAL_MESSAGE, RESUME_REMINDER

    names = ("swarm_board", "swarm_inbox", "swarm_state", "swarm_wiki")
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
            "content": INITIAL_MESSAGE.format(
                goal_post_id=(await store.get_swarm(sid))["goal_post_id"]
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
        assistant = ChatMessage.assistant(
            model=args.model,
            content=response.get("content"),
            tool_calls=[ToolCall.from_dict(call) for call in calls],
        )
        await sessions.get(binding.address).append_async(assistant)
        carriers = []
        receipts: list[tuple[int, str, str, str, str]] = []
        for index, call in enumerate(calls):
            name, arguments = call["name"], call["arguments"]
            call_context = replace(
                context, tool_name=name, tool_call_id=call["id"], tool_call_index=index
            )
            try:
                result = await registry.dispatch(call_context, arguments, allowed_tools=names)
            except ToolContractError as error:
                result = tool_failure("invalid_arguments", str(error))
            carriers.append(
                ChatMessage.tool(tool_call_id=call["id"], name=name, content=json.dumps(result))
            )
            if result["ok"]:
                data = result["data"]
                received_ids.update(_received_post_ids(data))
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
            assistant_message_id=assistant.id,
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
    goal_read = (await store.get_swarm(sid))["goal_post_id"] in received_ids
    coordinated = (
        (goal_read and feedback_received and published_after_feedback) if unassisted else resumed
    )
    strict_count = sum(
        item.get("strict") is True
        for item in render_tool_definitions(definitions, profile=_expected_profile(args))
    )
    return {
        "scenario": "swarm_unassisted" if unassisted else "swarm_workflow",
        "goal_read": goal_read,
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
