#!/usr/bin/env python
"""Behavioral reasoning-replay probe: does the model READ replayed reasoning?

The token-delta method (probe_reasoning_replay_tokens.py) answers whether the
gateway BILLS replayed reasoning as input. A gateway may instead feed the
carrier to the model without billing it as prompt tokens - token deltas of 0
then lie. This probe plants a random secret word inside the reasoning carrier
of an assistant turn and asks the model for that word in the follow-up turn.
If the model reproduces the secret, the carrier reached it; the no-carrier
control proves the word was not guessed.

Shapes measured (vBot's real request shapes):
- in_run: assistant tool-call turn carrying the carrier, then tool result,
  then the question - the agentic-loop continuation.
- cross_run: a plain history assistant turn carrying the carrier, then the
  question - what a new Run sends.

Prints only verdicts and short answer snippets; never API keys or full
prompts. Exit code 0 when every shape behaved consistently.

Examples:
    python scripts/probe_reasoning_replay_behavior.py --model glm-5.3-flash \
        --base-url https://opencode.ai/zen/go --api-key-env OPENCODE_GO_API_KEY
    python scripts/probe_reasoning_replay_behavior.py --model glm-5.2 \
        --base-url https://ollama.com --api-key-env OLLAMA_API_KEY
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import re
import sys
from pathlib import Path
from typing import Any

try:
    import httpx
except ImportError:  # pragma: no cover - degraded mode
    httpx = None  # type: ignore[assignment]

DEFAULT_DATA_DIR = Path.home() / ".vbot"
DEFAULT_BASE_URL = "https://ollama.com"
DEFAULT_EFFORT = "max"
DEFAULT_MAX_TOKENS = 4000
REQUEST_TIMEOUT = 300.0
SECRET_ALPHABET = "QWXZKM"
SECRET_LENGTH = 8

TOOL_DEFINITION: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Get the current weather in a city",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    },
}

TURN1_TOOL_PROMPT = (
    "Call the get_weather tool with city='Berlin'. Your final content must be empty."
)
TURN1_PLAIN_PROMPT = (
    "A bat and a ball cost $1.10 in total. The bat costs $1.00 more than the ball. "
    "How much does the ball cost? Show your reasoning, then give the final answer."
)
FOLLOW_UP_PROMPT = (
    "A secret word was planted in your previous reasoning. "
    "Answer with exactly that word and nothing else."
)
TOOL_RESULT_CONTENT = '{"temperature": 20, "condition": "sunny"}'


def _load_api_key(env_name: str, data_dir: Path) -> str:
    env_path = data_dir / ".env"
    if not env_path.is_file():
        raise SystemExit(f"no .env found at {env_path}")
    for line in env_path.read_text(encoding="utf-8").splitlines():
        match = re.match(rf"\s*{re.escape(env_name)}\s*=\s*(.+?)\s*$", line)
        if match:
            return match.group(1).strip().strip('"').strip("'")
    raise SystemExit(f"no {env_name} entry in {env_path}")


def _random_secret() -> str:
    return "".join(random.choice(SECRET_ALPHABET) for _ in range(SECRET_LENGTH))


async def _send_stream(
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict[str, Any]],
    *,
    tools: list[dict[str, Any]] | None,
    effort: str,
    max_tokens: int,
    extra_body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One streaming /v1/chat/completions request; returns content + tool calls."""
    if httpx is None:
        raise SystemExit("httpx is required for the probe (pip install httpx)")
    body: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": True,
        "stream_options": {"include_usage": True},
        "reasoning_effort": effort,
        "max_tokens": max_tokens,
    }
    if tools:
        body["tools"] = tools
    if extra_body:
        body.update(extra_body)
    url = f"{base_url.rstrip('/')}/v1/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}"}

    content_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []

    async with (
        httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client,
        client.stream("POST", url, json=body, headers=headers) as response,
    ):
        if response.status_code != 200:
            raw = (await response.aread()).decode("utf-8", "replace")
            raise RuntimeError(f"HTTP {response.status_code}: {raw[:300]}")
        async for line in response.aiter_lines():
            if not line or not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                continue
            try:
                chunk = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if chunk.get("error"):
                raise RuntimeError(f"stream error: {chunk['error']}")
            choice = (chunk.get("choices") or [{}])[0]
            delta = choice.get("delta") or {}
            if delta.get("content"):
                content_parts.append(delta["content"])
            if delta.get("tool_calls"):
                tool_calls.extend(delta["tool_calls"])

    return {"content": "".join(content_parts), "tool_calls": tool_calls}


def _assistant_turn(
    *,
    carrier: str,
    secret: str,
    mode: str,
    tool_calls: list[dict[str, Any]] | None,
    base_content: str,
) -> dict[str, Any]:
    """Build the assistant history turn carrying (or not) the secret."""
    message: dict[str, Any] = {"role": "assistant", "content": base_content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    if mode == "with":
        if carrier == "content_tags":
            message["content"] = f" thinking\n{secret}\n response\n" + base_content
        else:
            message[carrier] = f"Let me think carefully about this task. {secret}"
    elif mode == "visible":
        message["content"] = base_content + f"\n\n{secret}"
    return message


def _answer_contains_secret(answer: str, secret: str) -> bool:
    return secret.casefold() in answer.casefold()


async def _measure_shape(
    base_url: str,
    api_key: str,
    model: str,
    *,
    carrier: str,
    effort: str,
    max_tokens: int,
    in_run: bool,
    extra_body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run the with/without/visible variants for one shape; return verdicts."""
    secret = _random_secret()
    if in_run:
        turn1 = await _send_stream(
            base_url,
            api_key,
            model,
            [{"role": "user", "content": TURN1_TOOL_PROMPT}],
            tools=[TOOL_DEFINITION],
            effort=effort,
            max_tokens=max_tokens,
            extra_body=extra_body,
        )
        if not turn1["tool_calls"]:
            return {"error": "no tool call produced"}
        tool_calls = turn1["tool_calls"][:1]
        history = [{"role": "user", "content": TURN1_TOOL_PROMPT}]
        base_content = ""
    else:
        turn1 = await _send_stream(
            base_url,
            api_key,
            model,
            [{"role": "user", "content": TURN1_PLAIN_PROMPT}],
            tools=None,
            effort=effort,
            max_tokens=max_tokens,
            extra_body=extra_body,
        )
        tool_calls = None
        history = [{"role": "user", "content": TURN1_PLAIN_PROMPT}]
        base_content = turn1["content"]

    results: dict[str, bool] = {}
    answers: dict[str, str] = {}
    for mode in ("with", "without", "visible"):
        assistant = _assistant_turn(
            carrier=carrier,
            secret=secret,
            mode=mode,
            tool_calls=tool_calls,
            base_content=base_content,
        )
        messages = [*history, assistant]
        if in_run:
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_calls[0].get("id", "tool_call_0"),
                    "content": TOOL_RESULT_CONTENT,
                }
            )
        messages.append({"role": "user", "content": FOLLOW_UP_PROMPT})
        answer = await _send_stream(
            base_url,
            api_key,
            model,
            messages,
            tools=[TOOL_DEFINITION] if in_run else None,
            effort=effort,
            max_tokens=max_tokens,
            extra_body=extra_body,
        )
        results[mode] = _answer_contains_secret(answer["content"], secret)
        answers[mode] = answer["content"].strip()

    return {
        "secret": secret,
        "with": results["with"],
        "without": results["without"],
        "visible": results["visible"],
        "answers": answers,
    }


async def _probe_model(
    base_url: str,
    api_key: str,
    model: str,
    *,
    carrier: str,
    effort: str,
    max_tokens: int,
    extra_body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    print(f"\n=== {model} (carrier={carrier}, effort={effort}) ===")
    summary: dict[str, Any] = {"model": model, "carrier": carrier}
    for shape_name, in_run in (("in_run", True), ("cross_run", False)):
        result = await _measure_shape(
            base_url,
            api_key,
            model,
            carrier=carrier,
            effort=effort,
            max_tokens=max_tokens,
            in_run=in_run,
            extra_body=extra_body,
        )
        summary[shape_name] = result
        if "error" in result:
            print(f"  {shape_name}: {result['error']}")
        else:
            print(
                f"  {shape_name}: with={result['with']} without={result['without']} "
                f"visible={result['visible']} secret={result['secret']}"
            )
            for mode in ("with", "without", "visible"):
                answer = result["answers"][mode]
                shown = answer if len(answer) <= 120 else answer[:117] + "..."
                print(f"    {mode}: {shown!r}")
    return summary


async def _run(args: argparse.Namespace) -> int:
    api_key = _load_api_key(args.api_key_env, args.data_dir)
    print(f"api key length: {len(api_key)}")
    print(f"endpoint: {args.base_url.rstrip('/')}/v1/chat/completions | streaming")
    extra_body: dict[str, Any] | None = None
    if args.extra_body:
        try:
            extra_body = json.loads(args.extra_body)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"--extra-body must be valid JSON: {exc}") from exc
        print(f"extra body: {extra_body}")
    models = args.models.split(",") if args.models else [args.model]
    summaries = []
    for model in models:
        model = model.strip()
        if not model:
            continue
        try:
            summaries.append(
                await _probe_model(
                    args.base_url,
                    api_key,
                    model,
                    carrier=args.carrier,
                    effort=args.effort,
                    max_tokens=args.max_tokens,
                    extra_body=extra_body,
                )
            )
        except Exception as exc:  # noqa: BLE001 - sweep must continue
            print(f"  ERROR: {exc}")
            summaries.append({"model": model, "error": str(exc)})

    print("\n\n=== SUMMARY ===")
    for summary in summaries:
        if "error" in summary and "in_run" not in summary:
            print(f"{summary['model']}: ERROR {summary['error']}")
            continue
        for shape_name in ("in_run", "cross_run"):
            shape = summary[shape_name]
            if "error" in shape:
                print(f"{summary['model']} [{summary['carrier']}] {shape_name}: {shape['error']}")
                continue
            verdict = (
                "replay reaches the model"
                if shape["with"] and not shape["without"]
                else (
                    "NO replay effect (control clean)"
                    if not shape["with"] and not shape["without"]
                    else "inconclusive (control also answered)"
                )
            )
            print(
                f"{summary['model']} [{summary['carrier']}] {shape_name}: "
                f"with={shape['with']} without={shape['without']} visible={shape['visible']} "
                f"-> {verdict}"
            )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", help="Exact wire model id (or use --models).")
    parser.add_argument("--models", help="Comma-separated model ids for a sweep.")
    parser.add_argument(
        "--carrier",
        default="reasoning_content",
        help="Carrier field to plant the secret into (default reasoning_content).",
    )
    parser.add_argument(
        "--extra-body",
        default="",
        help="Optional JSON object merged into every request body (e.g. "
        '{\\"clear_thinking\\": false} to test Preserved-Thinking parameters).',
    )
    parser.add_argument("--effort", default=DEFAULT_EFFORT)
    parser.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument(
        "--api-key-env",
        default="OLLAMA_API_KEY",
        help="Name of the API key variable read from <data-dir>/.env. Never printed.",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=DEFAULT_DATA_DIR,
        help="Data dir whose .env holds the key.",
    )
    args = parser.parse_args(argv)
    if not args.model and not args.models:
        parser.error("--model or --models is required")
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
