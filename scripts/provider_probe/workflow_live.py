"""Live Tools probe: does the backend model pick the right first Tool call?

Each trial runs the production ``LiveBrain`` (delegation instructions, Live
Tools, call preparation) on one voice-style request against a scripted vBot,
so nothing in vBot changes. The verdict judges only the first Tool call:
``ideal`` (a right call), ``lookup`` (a valid read-only call first, accepted),
``wrong``, or ``error`` (the backend model request failed). Later calls and the
spoken answer are kept for review; an answer that claims a start, send, or stop
although no Tool call changed anything is flagged as an unconfirmed claim and
fails the probe.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from core.model_tasks._live_brain import BrainTarget, DelegationInput, LiveBrain
from scripts.provider_probe.live_cases import LiveCase, ScriptedVbot, describe, live_cases, matches
from server.live._arguments import run_live_call
from server.live._brief import DELEGATION_INSTRUCTIONS, LIVE_READ_ONLY_TOOLS, live_tools

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
    """The probe's Adapter, lent to one delegation: requests are kept, closing is not."""

    def __init__(self, adapter: Any) -> None:
        self._adapter = adapter
        self.requests: list[list[JsonObject]] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._adapter, name)

    async def send(self, messages: list[JsonObject], **kwargs: Any) -> Any:
        self.requests.append([dict(message) for message in messages])
        return await self._adapter.send(messages, **kwargs)

    async def aclose(self) -> None:
        return None


def verdict(case: LiveCase, records: list[JsonObject]) -> str:
    """Judge the first Tool call of one delegation from its records."""

    calls = [record for record in records if record.get("type") == "tool"]
    if not calls:
        delegation = next((r for r in records if r.get("type") == "delegation"), {})
        if delegation.get("failure"):
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


async def evaluate_live_case(
    adapter: Any,
    args: argparse.Namespace,
    case: LiveCase,
    repetition: int = 1,
    *,
    models: Any = None,
) -> JsonObject:
    """Run one delegation for *case* and judge its first Tool call."""

    borrowed = _Borrowed(adapter)
    records: list[JsonObject] = []
    vbot = ScriptedVbot()

    async def run_tool(name: Any, arguments: Any, *, rejection: JsonObject | None = None) -> Any:
        return await run_live_call(
            name, arguments, execute=vbot, rejection=rejection, record=records.append
        )

    brain = LiveBrain(
        SimpleNamespace(get_adapter=lambda _ref: borrowed, models=models),
        BrainTarget(
            provider_id=args.provider,
            connection_id=args.connection,
            model_id=args.model,
            thinking_effort=args.thinking_effort,
        ),
        instructions=DELEGATION_INSTRUCTIONS,
        tools=live_tools(),
        run_tool=run_tool,
        conversation_id=f"live-probe-{case.id}-{repetition}",
        record=records.append,
        max_steps=MAX_TRIAL_STEPS,
    )
    answer = await brain.answer(
        DelegationInput(
            request=case.request, conversation=case.conversation, updates="\n".join(case.updates)
        )
    )
    judged = verdict(case, records)
    claim = unconfirmed_claim(records, answer)
    calls = [
        {key: record.get(key) for key in _CALL_FIELDS}
        for record in records
        if record.get("type") == "tool"
    ]
    delegation = next((r for r in records if r.get("type") == "delegation"), {})
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
        "failure": delegation.get("failure"),
        "transcript": borrowed.requests[-1] if borrowed.requests else [],
    }


async def _probe_live_tools(
    adapter: Any, args: argparse.Namespace, *, models: Any = None
) -> JsonObject:
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
                "Production LiveBrain with the delegation instructions and Live Tools; a "
                "scripted vBot answers from one fixed state, so nothing changes. Only the "
                "first Tool call is judged; later calls and the answer need review. An "
                "answer claiming a start, send, or stop without a changing Tool call fails "
                "(a word heuristic)."
            ),
            "results": ordered,
        }

    async def evaluate(case: LiveCase, repetition: int) -> None:
        async with limit:
            results.append(await evaluate_live_case(adapter, args, case, repetition, models=models))
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
