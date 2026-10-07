"""Live Tools probe: does the Live backend Agent's Model pick the right first Tool call?

Each trial gives one voice-style request to the Model with what the Live
backend Agent gets in a call: its Live call instructions, the System Reminder
with what happened in the call, the Live Tools, and their call preparation. A
scripted vBot answers, so nothing in vBot changes. The probe runs its own small
Tool loop instead of a Chat Run, so the rest of the Agent's System Prompt and
its web Tools are missing. The verdict judges only the first Tool call:
``ideal`` (a right call), ``lookup`` (a valid read-only call first, accepted),
``wrong``, or ``error`` (the Model request failed). Later calls and the answer
are kept for review; an answer that claims a start, send, or stop although no
Tool call changed anything is flagged as an unconfirmed claim and fails the
probe.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from pathlib import Path
from typing import Any

from core.chat.messages import LIVE_VOICE_SYSTEM_REMINDER
from core.chat.streaming import stream_model_response
from core.chat.wire_shaping import system_reminder_request_message
from core.model_tasks._live_backend import BackendRequest, context_note
from core.model_tasks._live_brief import backend_instructions
from core.model_tasks.live import live_failure
from core.providers.adapter import TOOL_CALL_REJECTION_FIELD
from core.tools import called_tool_name
from core.tools.live import (
    LIVE_READ_ONLY_TOOLS,
    LIVE_TOOL_NAMES,
    TOOL_OVERVIEW,
    live_tool_definitions,
)
from scripts.provider_probe.live_cases import LiveCase, ScriptedVbot, describe, live_cases, matches
from server.live._arguments import PreparedLiveCall, prepare_live_call

JsonObject = dict[str, Any]

# Enough for a lookup, the action, and the answer.
MAX_TRIAL_STEPS = 4
_CALL_FIELDS = ("called", "tool", "arguments", "run_arguments", "ok", "result")
# Words of a spoken answer that report an action as done (German and English);
# a heuristic, so flagged answers need a look.
_DONE_CLAIM = re.compile(
    r"\b(gestartet|losgeschickt|geschickt|gesendet|beauftragt|gestoppt|angehalten|erledigt"
    r"|started|sent|stopped|launched)\b",
    re.IGNORECASE,
)


class _Borrowed:
    """The probe's Adapter, lent to one request: requests are kept, closing is not."""

    def __init__(self, adapter: Any) -> None:
        self._adapter = adapter
        self.requests: list[list[JsonObject]] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._adapter, name)

    def stream(self, messages: list[JsonObject], **kwargs: Any) -> Any:
        self.requests.append([dict(message) for message in messages])
        return self._adapter.stream(messages, **kwargs)

    async def send(self, messages: list[JsonObject], **kwargs: Any) -> Any:
        # Only reached when the Provider cannot stream; that request is already kept.
        return await self._adapter.send(messages, **kwargs)

    async def aclose(self) -> None:
        return None


def verdict(case: LiveCase, records: list[JsonObject], failure: str | None = None) -> str:
    """Judge the first Tool call of one request from its records."""

    calls = [record for record in records if record.get("type") == "tool"]
    if not calls:
        if failure:
            return "error"
        return "ideal" if None in case.right else "wrong"
    first = calls[0]
    tool, arguments = first.get("tool"), first.get("run_arguments")
    if any(expected is not None and matches(expected, tool, arguments) for expected in case.right):
        return "ideal"
    if case.lookup_ok and tool in LIVE_READ_ONLY_TOOLS and first.get("ok"):
        return "lookup"
    return "wrong"


def unconfirmed_claim(records: list[JsonObject], answer: str) -> bool:
    """Whether *answer* reports an action as done although no Tool call changed anything."""

    changed = any(
        record.get("type") == "tool"
        and record.get("ok")
        and record.get("tool") not in LIVE_READ_ONLY_TOOLS
        for record in records
    )
    return not changed and _DONE_CLAIM.search(answer or "") is not None


async def _run_tool(
    vbot: ScriptedVbot, tool_call: JsonObject, records: list[JsonObject]
) -> JsonObject:
    """Run one Tool call as a Live call does: its name, then its arguments, then vBot."""
    called = str(tool_call.get("name") or "")
    tool = called_tool_name(called, LIVE_TOOL_NAMES)
    arguments = tool_call.get("arguments")
    rejection = tool_call.get(TOOL_CALL_REJECTION_FIELD)
    prepared: PreparedLiveCall | JsonObject = (
        live_failure(str(rejection.get("code")), str(rejection.get("message")))
        if isinstance(rejection, dict)
        else prepare_live_call(tool, arguments)
    )
    if isinstance(prepared, PreparedLiveCall):
        result = await vbot(prepared.name, prepared.arguments)
    else:
        result = prepared
    records.append(
        {
            "type": "tool",
            "called": called,
            "tool": tool,
            "arguments": arguments,
            "run_arguments": prepared.arguments if isinstance(prepared, PreparedLiveCall) else None,
            "ok": result.get("ok") is True,
            "result": result,
        }
    )
    return result


async def _answer(
    adapter: Any,
    args: argparse.Namespace,
    messages: list[JsonObject],
    vbot: ScriptedVbot,
    records: list[JsonObject],
    conversation_id: str,
) -> str:
    """Run Model steps and their Tool calls until the Model answers in text."""
    request_context = dict(
        adapter.request_context_kwargs(agent_id="live-backend", session_id=conversation_id)
    )
    tools = live_tool_definitions()
    for _step in range(MAX_TRIAL_STEPS):
        response = await stream_model_response(
            adapter,
            messages,
            model_id=args.model,
            thinking_effort=args.thinking_effort,
            tools=tools,
            **request_context,
        )
        content = response.get("content")
        calls = [call for call in response.get("tool_calls") or [] if isinstance(call, dict)]
        if not calls:
            return content.strip() if isinstance(content, str) else ""
        assistant: JsonObject = {"role": "assistant", "content": content, "tool_calls": calls}
        for key in ("reasoning", "reasoning_meta"):
            if response.get(key) is not None:
                assistant[key] = response[key]
        messages.append(assistant)
        for call in calls:
            result = await _run_tool(vbot, call, records)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.get("id"),
                    "content": json.dumps(result, ensure_ascii=False),
                }
            )
    raise RuntimeError(f"the request needed more than {MAX_TRIAL_STEPS} steps")


async def evaluate_live_case(
    adapter: Any,
    args: argparse.Namespace,
    case: LiveCase,
    repetition: int = 1,
) -> JsonObject:
    """Run one request for *case* and judge its first Tool call."""

    borrowed = _Borrowed(adapter)
    records: list[JsonObject] = []
    vbot = ScriptedVbot()
    # Like a real call, the request comes with what vBot shows right now.
    state = (await vbot(TOOL_OVERVIEW, {}))["data"]["content"]
    note = context_note(
        BackendRequest(
            request=case.request,
            conversation=case.conversation,
            updates="\n".join(case.updates),
            state=state,
        )
    )
    messages: list[JsonObject] = [
        {
            "role": "system",
            "content": backend_instructions(set(LIVE_TOOL_NAMES).__contains__, context_note=True),
        },
        system_reminder_request_message(note, LIVE_VOICE_SYSTEM_REMINDER),
        {"role": "user", "content": case.request},
    ]
    failure: str | None = None
    try:
        answer = await _answer(
            borrowed, args, messages, vbot, records, f"live-probe-{case.id}-{repetition}"
        )
    except Exception as exc:
        failure, answer = f"{type(exc).__name__}: {exc}", ""
    judged = verdict(case, records, failure)
    claim = unconfirmed_claim(records, answer)
    calls = [
        {key: record.get(key) for key in _CALL_FIELDS}
        for record in records
        if record.get("type") == "tool"
    ]
    return {
        "case": case.id,
        "repetition": repetition,
        "request": case.request,
        "verdict": judged,
        "right": judged in {"ideal", "lookup"} and not claim,
        "unconfirmed_claim": claim,
        "expected": [describe(expected) for expected in case.right],
        "first_call": calls[0] if calls else None,
        "calls": calls,
        "answer": answer,
        "failure": failure,
        "transcript": borrowed.requests[-1] if borrowed.requests else [],
    }


async def _probe_live_tools(adapter: Any, args: argparse.Namespace) -> JsonObject:
    cases = live_cases()
    selected = args.live_case.split(",")
    if selected != ["all"]:
        known = {case.id for case in cases}
        if set(selected) - known:
            raise ValueError(f"Unknown live case(s): {sorted(set(selected) - known)}")
        cases = [case for case in cases if case.id in selected]
    if not 1 <= args.repetitions <= 20:
        raise ValueError("repetitions must be between 1 and 20")
    limit = asyncio.Semaphore(3)
    results: list[JsonObject] = []

    def report() -> JsonObject:
        ordered = sorted(results, key=lambda row: (row["case"], row["repetition"]))
        counts = dict.fromkeys(("ideal", "lookup", "wrong", "error"), 0)
        for row in ordered:
            counts[row["verdict"]] += 1
        claims = sum(1 for row in ordered if row["unconfirmed_claim"])
        return {
            "scenario": "live_tools",
            "evaluation": "first_tool_call",
            "provider": args.provider,
            "connection": args.connection,
            "model": args.model,
            "thinking_effort": args.thinking_effort,
            "trials": len(ordered),
            "planned_trials": len(cases) * args.repetitions,
            **counts,
            "unconfirmed_claims": claims,
            "passed": len(ordered) == len(cases) * args.repetitions
            and all(row["right"] for row in ordered),
            "fixture_limits": (
                "The Live backend Agent's Live call instructions, System Reminder, and Live "
                "Tools in a small Tool loop, without the rest of its System Prompt or its "
                "web Tools; a scripted vBot answers from one fixed state, so nothing "
                "changes. Only the first Tool call is judged; later calls and the answer "
                "need review. An answer claiming a start, send, or stop without a changing "
                "Tool call fails (a word heuristic)."
            ),
            "results": ordered,
        }

    async def evaluate(case: LiveCase, repetition: int) -> None:
        async with limit:
            results.append(await evaluate_live_case(adapter, args, case, repetition))
            if args.live_report:
                path = Path(args.live_report)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    json.dumps(report(), indent=2, ensure_ascii=False),
                    encoding="utf-8",
                    newline="\n",
                )

    await asyncio.gather(
        *(
            evaluate(case, repetition)
            for repetition in range(1, args.repetitions + 1)
            for case in cases
        )
    )
    output = report()
    # Transcripts stay in the explicit report.
    output["results"] = [
        {key: value for key, value in row.items() if key != "transcript"}
        for row in output["results"]
    ]
    if args.live_report:
        output["report"] = str(args.live_report)
    return output
