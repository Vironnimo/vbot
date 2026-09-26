"""Black-box task trials: no prescribed Tool, arguments, or call count."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from core.chat.wire_shaping import model_facing_request
from core.providers.adapter import terminal_outcome_from_response
from core.tools.tools import tool_failure
from scripts.provider_probe.first_use_cases import BASE_FILES, assess, first_use_cases
from scripts.provider_probe.first_use_fixture import FirstUseFixture, FixtureBoundaryError


async def first_use_trial(
    adapter: Any, args: argparse.Namespace, case: dict, repetition: int
) -> dict:
    record: dict[str, Any] = {
        "case": case["id"],
        "repetition": repetition,
        "evaluation": "black_box_task",
        "responses": [],
        "calls": [],
        "passed": False,
        "first_attempt_success": False,
    }
    with TemporaryDirectory(prefix="vbot-first-use-") as directory:
        fixture = FirstUseFixture(
            Path(directory),
            nested=case.get("nested", False),
            outside_cwd=case.get("outside_cwd", False),
        )
        try:
            fixture.write({**BASE_FILES, **case.get("files", {})})
            (fixture.repo / "src/empty").mkdir()
            messages: list[dict] = [{"role": "system", "content": fixture.system_prompt()}]
            if case.get("seed"):
                fixture.hold_children = True
                seed = {
                    "id": "seed-review",
                    "name": "subagent",
                    "arguments": {
                        "action": "run",
                        "agent_id": "reviewer",
                        "content": (
                            "Read-only review of src/a.py. Return findings with line numbers."
                        ),
                    },
                }
                fixture.seed_result = await fixture.dispatch(seed)
                if not fixture.seed_result["ok"] or not fixture.received:
                    raise RuntimeError(f"Cannot seed existing work: {fixture.seed_result}")
                fixture.seed_run = fixture.runs.get(fixture.received[0]["run_id"])
                messages.extend(
                    [
                        {
                            "role": "user",
                            "content": "Have reviewer independently review src/a.py, read-only.",
                        },
                        {"role": "assistant", "content": None, "tool_calls": [seed]},
                        {
                            "role": "tool",
                            "name": "subagent",
                            "tool_call_id": seed["id"],
                            "content": json.dumps(fixture.seed_result),
                        },
                        {"role": "assistant", "content": "The review is running."},
                    ]
                )
            initial_received = len(fixture.received)
            messages.append(
                {"role": "user", "content": case["task"].replace("{repo}", fixture.repo.as_posix())}
            )
            definitions = fixture.registry.provider_definitions()
            record["definitions"] = definitions
            _, record["model_definitions"] = model_facing_request([], definitions)
            record["definition_sha256"] = hashlib.sha256(
                json.dumps(definitions, sort_keys=True).encode()
            ).hexdigest()
            record["initial_messages"] = list(messages)
            final = ""
            failed_calls = 0
            expected_rejections = 0
            boundary_failure = False
            started = time.monotonic()
            async with asyncio.timeout(args.total_timeout):
                for step in range(8):
                    raw = await adapter.send(
                        messages,
                        model_id=args.model,
                        tools=definitions,
                        thinking_effort=args.thinking_effort,
                        max_tokens=args.max_tokens or 6000,
                        **adapter.request_context_kwargs(
                            agent_id="first-use", session_id=fixture.root.name
                        ),
                    )
                    response = adapter.normalize_response(raw, model_id=args.model)
                    record["responses"].append(response)
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
                        final = str(response.get("content") or "")
                        record["finished"] = terminal_outcome_from_response(response) == "stop"
                        break
                    for call in calls:
                        try:
                            result = await fixture.dispatch(call)
                        except FixtureBoundaryError as error:
                            result = tool_failure("fixture_boundary", str(error))
                            boundary_failure = True
                        except Exception as error:
                            result = tool_failure(
                                "dispatch_rejected", str(error) or type(error).__name__
                            )
                        row = {
                            "step": step + 1,
                            "name": call.get("name"),
                            "arguments": call.get("arguments"),
                            "result": result,
                        }
                        record["calls"].append(row)
                        failed_calls += int(not result["ok"])
                        if case.get("unavailable") and (result.get("error") or {}).get("code") in {
                            "agent_not_allowed",
                            "agent_not_found",
                        }:
                            expected_rejections += 1
                        messages.append(
                            {
                                "role": "tool",
                                "name": call.get("name"),
                                "tool_call_id": call.get("id"),
                                "content": json.dumps(result),
                            }
                        )
                    if boundary_failure:
                        # Do not leak the grader's preferred Tool through recovery text.
                        break
                if case.get("continuation"):
                    queued = fixture.runs.list_queued(
                        "reviewer", fixture.seed_result["data"]["session_id"], project_id=None
                    )
                    fixture.release.set()
                    await fixture.seed_run.wait()
                    for item in queued:
                        child = await item.future
                        await child.wait()
            outcome, detail = assess(case, fixture, record["calls"], final, initial_received)
            outcome = outcome and record.get("finished", False)
            record.update(detail)
            record.update(
                final=final,
                elapsed_seconds=round(time.monotonic() - started, 2),
                failed_calls=failed_calls,
                expected_rejections=expected_rejections,
                boundary_failure=boundary_failure,
                outcome_success=outcome,
                passed=outcome and not boundary_failure,
                first_attempt_success=outcome
                and failed_calls == expected_rejections
                and not boundary_failure,
            )
        except Exception as error:
            record["exception"] = {"type": type(error).__name__, "message": str(error)}
        finally:
            await fixture.close()
    return record


async def _probe_first_use(adapter: Any, args: argparse.Namespace) -> dict:
    selected = args.first_use_case.split(",")
    cases = [case for case in first_use_cases() if args.first_use_tool in ("all", case["tool"])]
    if selected != ["all"]:
        known = {case["id"] for case in cases}
        if set(selected) - known:
            raise ValueError(f"Unknown first-use case(s): {sorted(set(selected) - known)}")
        cases = [case for case in cases if case["id"] in selected]
    if not 1 <= args.repetitions <= 20:
        raise ValueError("repetitions must be between 1 and 20")
    limit = asyncio.Semaphore(3)
    results: list[dict[str, Any]] = []

    def report() -> dict[str, Any]:
        return {
            "scenario": "tool_first_use",
            "evaluation": "black_box_task",
            "provider": args.provider,
            "connection": args.connection,
            "model": args.model,
            "thinking_effort": args.thinking_effort,
            "max_tokens": args.max_tokens or 6000,
            "trials": len(results),
            "planned_trials": len(cases) * args.repetitions,
            "first_attempt_successes": sum(r["first_attempt_success"] for r in results),
            "outcome_successes": sum(r["passed"] for r in results),
            "passed": len(results) == len(cases) * args.repetitions
            and all(r["first_attempt_success"] for r in results),
            "fixture_limits": (
                "Real search/read/edit/safe-shell dispatch and Sub-Agent lifecycle. Child Model "
                "work is a deterministic receiver; unexpected shell commands are recorded "
                "and stopped, never substituted. Final-answer semantics beyond fixture "
                "facts require review of retained responses. Tool choice and shell bypasses "
                "are measurements separate from task outcome."
            ),
            "results": results,
        }

    async def evaluate(case, repetition):
        async with limit:
            result = await first_use_trial(adapter, args, case, repetition)
            results.append(result)
            if args.first_use_report:
                path = Path(args.first_use_report)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    json.dumps(report(), indent=2, ensure_ascii=False), encoding="utf-8"
                )
            logging.getLogger("vbot.first_use").info(
                "%s trial %s: first=%s outcome=%s",
                case["id"],
                repetition,
                result["first_attempt_success"],
                result["passed"],
            )

    await asyncio.gather(
        *(
            evaluate(case, repetition)
            for repetition in range(1, args.repetitions + 1)
            for case in cases
        )
    )
    output = report()
    # Full synthetic traces stay in the explicit report, including failures.
    if args.first_use_report:
        output["report"] = str(args.first_use_report)
        output["results"] = [
            {
                k: r[k]
                for k in (
                    "case",
                    "repetition",
                    "passed",
                    "first_attempt_success",
                    "failed_calls",
                    "exception",
                    "boundary_failure",
                )
                if k in r
            }
            for r in results
        ]
    return output
