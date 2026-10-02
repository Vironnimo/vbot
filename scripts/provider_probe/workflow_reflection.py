"""Provider Tool probe: Reflection review and ``/learn`` decisions in production rendering.

Every attempt seeds a case's Memory, Skills and Session history into a
disposable fixture Agent, renders the System Prompt and Tool definitions
through production, sends the review brief or ``/learn`` instruction as a
System Reminder note and runs the Model's Tool calls through the production
executor. Expectations stay with the observer; only production context and the
case history reach the Model. Chat fork and cadence integration are tested
separately.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
import uuid
from collections.abc import Mapping
from typing import Any

from scripts.provider_probe.common import PROJECT_ROOT
from scripts.provider_probe.learning_eval import ReflectionReport, format_pass_rate_table
from scripts.provider_probe.learning_fixture import EvalWorker, tool_message_content
from scripts.provider_probe.learning_scoring import CallObserver, score_attempt
from scripts.provider_probe.learning_texts import TextPack, load_text_pack

# Mirrors the planned per-Run Tool iteration limit of review Runs; a review that
# needs more has lost its way. /learn keeps a generous safety budget instead.
REVIEW_TOOL_ITERATION_LIMIT = 16
LEARN_TOOL_ITERATION_LIMIT = 30
MAX_REPETITIONS = 50


def _reflection_cases() -> list[dict[str, Any]]:
    path = PROJECT_ROOT / "tests/fixtures/reflection/cases.json"
    cases: list[dict[str, Any]] = json.loads(path.read_text(encoding="utf-8"))
    return cases


def _selected_pairs(args: argparse.Namespace) -> list[tuple[dict[str, Any], str]]:
    cases = _reflection_cases()
    requested = (
        None
        if args.reflection_case == "all"
        else {item.strip() for item in str(args.reflection_case).split(",") if item.strip()}
    )
    if requested is not None:
        unknown = requested - {case["id"] for case in cases}
        if unknown:
            raise ValueError(f"Unknown reflection case: {', '.join(sorted(unknown))}")
    selected = [
        (case, scope)
        for case in cases
        for scope in case["expected"]
        if (requested is None or case["id"] in requested)
        and args.reflection_scope in ("all", scope)
    ]
    if not selected:
        raise ValueError("No matching reflection scenario")
    return selected


def _add_usage(total: dict[str, Any], usage: Any) -> None:
    if not isinstance(usage, Mapping):
        return
    for key, value in usage.items():
        if isinstance(value, bool):
            continue
        if isinstance(value, int | float):
            total[key] = total.get(key, 0) + value


def _sampling_kwargs(
    args: argparse.Namespace, models: Any, agent_temperature: float | None
) -> dict[str, Any]:
    """Resolve temperature and top_p like Chat does for the evaluated Model."""
    if models is None:
        return {}
    from core.chat.model_resolution import resolve_request_temperature, resolve_request_top_p

    return {
        "temperature": resolve_request_temperature(
            agent_temperature, models, args.provider, args.model
        ),
        "top_p": resolve_request_top_p(models, args.provider, args.model),
    }


async def _run_attempt(
    worker: EvalWorker,
    adapter: Any,
    args: argparse.Namespace,
    case: dict[str, Any],
    scope: str,
    repetition: int,
    models: Any,
) -> dict[str, Any]:
    """Run one attempt; a crash is recorded on the attempt, never dropped."""
    from core.chat.wire_shaping import system_reminder_request_message
    from core.providers.adapter import terminal_outcome_from_response

    attempt_id = f"{case['id']}-{scope}-{repetition}-{uuid.uuid4().hex[:8]}"
    started = time.monotonic()
    attempt: dict[str, Any] = {
        "case": case["id"],
        "scope": scope,
        "repetition": repetition,
        "provider": args.provider,
        "model": args.model,
        "passed": False,
        "effect_passed": False,
        "finished": False,
        "stopped_reason": None,
        "error": None,
        "violations": [],
        "steps": 0,
        "tool_iterations": 0,
        "usage": {},
        "final_text": None,
    }
    messages: list[dict[str, Any]] = []
    calls_log: list[dict[str, Any]] = []
    system_prompt = ""
    definitions: list[dict[str, Any]] = []
    limit = LEARN_TOOL_ITERATION_LIMIT if scope == "learn" else REVIEW_TOOL_ITERATION_LIMIT
    try:
        async with asyncio.timeout(args.total_timeout):
            prepared = await worker.prepare(case, scope, attempt_id=attempt_id)
            system_prompt, definitions = prepared.system_prompt, prepared.definitions
            messages = list(prepared.messages)
            before = worker.state()
            observer = CallObserver(case)
            request_kwargs: dict[str, Any] = {
                **_sampling_kwargs(args, models, prepared.agent_temperature),
                **adapter.request_context_kwargs(agent_id="main", session_id=prepared.session_id),
            }
            if args.max_tokens:
                request_kwargs["max_tokens"] = args.max_tokens
            while True:
                attempt["steps"] += 1
                raw = await adapter.send(
                    messages,
                    model_id=args.model,
                    tools=definitions,
                    thinking_effort=args.thinking_effort,
                    **request_kwargs,
                )
                response = adapter.normalize_response(raw, model_id=args.model)
                _add_usage(attempt["usage"], response.get("usage"))
                outcome = terminal_outcome_from_response(response)
                calls = list(response.get("tool_calls") or [])
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
                if not calls:
                    text = response.get("content")
                    attempt["final_text"] = text if isinstance(text, str) else None
                    attempt["finished"] = outcome == "stop" and bool(str(text or "").strip())
                    attempt["stopped_reason"] = "final_answer" if attempt["finished"] else outcome
                    break
                if attempt["tool_iterations"] >= limit:
                    attempt["stopped_reason"] = "iteration_limit"
                    break
                attempt["tool_iterations"] += 1
                for index, call in enumerate(calls):
                    call.setdefault("id", f"call-{attempt['steps']}-{index}")
                    arguments = call.get("arguments")
                    target = arguments if isinstance(arguments, dict) else {}
                    observer.before(
                        str(call.get("name")),
                        arguments,
                        own_file_exists=call.get("name") == "skill_manage"
                        and target.get("action") != "create"
                        and worker.own_skill_file_exists(
                            str(target.get("name") or ""),
                            str(target.get("file_path") or "SKILL.md"),
                        ),
                    )
                notes: list[str] = []
                results = await worker.dispatch(
                    calls,
                    prepared,
                    restriction=prepared.restriction,
                    notes=notes,
                    iteration=attempt["tool_iterations"],
                )
                for call, result in zip(calls, results, strict=True):
                    observer.after(str(call.get("name")), call.get("arguments"), result)
                    calls_log.append(
                        {
                            "step": attempt["steps"],
                            "name": call.get("name"),
                            "arguments": call.get("arguments"),
                            "ok": bool(result.get("ok")),
                            "result": result,
                        }
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "name": call.get("name"),
                            "tool_call_id": call["id"],
                            "content": tool_message_content(result),
                        }
                    )
                if notes:
                    messages.append(system_reminder_request_message(*notes))
            after = worker.state()
            attempt.update(
                score_attempt(case, scope, before, after, observer, finished=attempt["finished"])
            )
            attempt["state"] = {"before": before, "after": after}
    except Exception as error:  # noqa: BLE001 - a crashed attempt stays in the report
        attempt["error"] = {"type": type(error).__name__, "message": str(error)[:2000]}
        attempt["passed"] = False
    attempt["duration_seconds"] = round(time.monotonic() - started, 3)
    attempt["system_prompt"] = system_prompt
    attempt["definitions"] = definitions
    attempt["transcript"] = {
        "messages": [message for message in messages if message.get("role") != "system"],
        "calls": calls_log,
    }
    return attempt


async def _probe_reflection_workflow(
    adapter: Any, args: argparse.Namespace, *, models: Any = None
) -> dict[str, Any]:
    """Run every selected (case, scope) ``--repetitions`` times and report all attempts."""
    import sys

    repetitions = int(args.repetitions)
    if not 1 <= repetitions <= MAX_REPETITIONS:
        raise ValueError(f"--repetitions must be between 1 and {MAX_REPETITIONS}")
    selected = _selected_pairs(args)
    pack: TextPack | None = load_text_pack(args.text_pack) if args.text_pack else None
    # Repetition-major order keeps denominators balanced if a run is interrupted.
    jobs: asyncio.Queue[tuple[dict[str, Any], str, int]] = asyncio.Queue()
    for repetition in range(1, repetitions + 1):
        for case, scope in selected:
            jobs.put_nowait((case, scope, repetition))
    worker_count = max(1, min(int(args.reflection_workers), jobs.qsize()))
    workers: list[EvalWorker] = []
    report: ReflectionReport | None = None
    try:
        for _ in range(worker_count):
            worker = EvalWorker(
                agent_model=f"{args.provider}/{args.model}",
                pack=pack,
                thinking_effort=args.thinking_effort,
            )
            workers.append(worker)
            worker.start()
        applied = workers[0].applied
        assert applied is not None
        for warning in applied.warnings:
            print(f"text pack warning: {warning}", file=sys.stderr)
        report = ReflectionReport.start(
            args,
            applied=applied,
            pack=pack,
            selected=[(case["id"], scope) for case, scope in selected],
            notes={case["id"]: case["note"] for case, _scope in selected if case.get("note")},
        )

        async def work(worker: EvalWorker) -> None:
            assert report is not None
            while True:
                try:
                    case, scope, repetition = jobs.get_nowait()
                except asyncio.QueueEmpty:
                    return
                attempt = await _run_attempt(worker, adapter, args, case, scope, repetition, models)
                report.add(attempt)
                print(
                    f"{attempt['case']}/{attempt['scope']} #{repetition}: "
                    f"{'pass' if attempt['passed'] else 'FAIL'}"
                    + (f" ({attempt['error']['type']})" if attempt["error"] else "")
                    + (f" {attempt['violations']}" if attempt["violations"] else ""),
                    file=sys.stderr,
                )

        await asyncio.gather(*(work(worker) for worker in workers))
    finally:
        for worker in workers:
            await worker.aclose()
        if report is not None:
            report.finish()
    assert report is not None
    print(format_pass_rate_table(report.document()), file=sys.stderr)
    return report.result()
