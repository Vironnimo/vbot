#!/usr/bin/env python
"""Streaming token-accounting reasoning-replay probe for Ollama Cloud /v1.

Measures whether replayed assistant reasoning actually reaches the model on
the OpenAI-compatible wire vBot uses for ollama-cloud (``/v1/chat/completions``,
``stream: true`` + ``stream_options.include_usage``). The verdict is the
provider-reported billed input (``usage.prompt_tokens``), never the answer
text: if the gateway strips the reasoning carrier before the model, the input
count does not grow; if it passes through, the input grows by roughly the
reasoning size.

Two shapes are measured separately (the user-established discipline: judge
replay by billed-input deltas in vBot's real request shapes):

- ``in_run`` (``current_run`` scope): the assistant tool-call turn carrying the
  reasoning carrier, exactly what the agentic loop sends as the continuation
  request after a tool result.
- ``cross_run`` (``full_history`` scope): a plain history turn carrying the
  reasoning carrier, exactly what a new Run sends.

Each shape runs three variants with identical history except the assistant
turn: WITH the reasoning carrier, WITHOUT it, and the reasoning text in
VISIBLE content (accounting control - a positive visible delta proves the
token accounting works, so a zero carrier delta means stripping, not a broken
measurement).

The replayed reasoning is REAL model output: the first request produces
reasoning plus a tool call (in-run) or a plain answer (cross-run), and that
exact text is replayed. The replay carrier is auto-detected from the model's
own response (first non-empty of ``reasoning_content`` / ``reasoning`` /
``thinking`` / ``reasoning_details``); ``--carrier`` forces a specific field
(e.g. vBot's default ``reasoning_content`` for a model whose responses carry
an opaque field vBot never replays).

Prints measurements only: carrier, lengths, per-variant input tokens and
deltas. Never prints API keys, full prompts, or full responses.

Examples:
    python scripts/probe_reasoning_replay_tokens.py --model glm-5.2
    python scripts/probe_reasoning_replay_tokens.py --model qwen3.5:397b \\
        --carrier reasoning_content
    python scripts/probe_reasoning_replay_tokens.py --models \\
        "gemma4:31b,glm-5.1,glm-5.2" --effort max
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Any

try:
    import httpx
except ImportError:  # pragma: no cover - degraded mode
    httpx = None  # type: ignore[assignment]

DEFAULT_DATA_DIR = Path.home() / ".vbot"
DEFAULT_API_KEY_ENV = "OLLAMA_API_KEY"
DEFAULT_BASE_URL = "https://ollama.com"
DEFAULT_EFFORT = "max"
DEFAULT_MAX_TOKENS = 4000
REQUEST_TIMEOUT = 300.0

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
FOLLOW_UP_PROMPT = "Reply with exactly one word: ok."
TOOL_RESULT_CONTENT = '{"temperature": 20, "condition": "sunny"}'

# Carrier fields probed in response order; the first non-empty one is the
# model's emitted carrier. vBot replays only the first two (its scan tuple).
CARRIER_FIELDS = ("reasoning_content", "reasoning", "thinking", "reasoning_details")
VBOT_REPLAY_FIELDS = ("reasoning_content", "reasoning")


def _load_api_key(env_name: str, data_dir: Path) -> str:
    env_path = data_dir / ".env"
    if not env_path.is_file():
        raise SystemExit(f"no .env found at {env_path}")
    for line in env_path.read_text(encoding="utf-8").splitlines():
        match = re.match(rf"\s*{re.escape(env_name)}\s*=\s*(.+?)\s*$", line)
        if match:
            return match.group(1).strip().strip('"').strip("'")
    raise SystemExit(f"no {env_name} entry in {env_path}")


def _detect_carrier(message: dict[str, Any]) -> tuple[str, str]:
    """Return (field, text) of the first non-empty reasoning carrier."""
    for field in CARRIER_FIELDS:
        value = message.get(field)
        if isinstance(value, str) and value:
            return field, value
    return "", ""


async def _send_stream(
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict[str, Any]],
    *,
    tools: list[dict[str, Any]] | None,
    effort: str,
    max_tokens: int,
) -> dict[str, Any]:
    """One streaming /v1/chat/completions request; returns measured facts."""
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
    url = f"{base_url.rstrip('/')}/v1/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}"}

    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    carrier_field = ""
    tool_calls: list[dict[str, Any]] = []
    prompt_tokens: int | None = None
    error_text = ""

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
                error_text = line[:200]
                continue
            if chunk.get("error"):
                raise RuntimeError(f"stream error: {chunk['error']}")
            choice = (chunk.get("choices") or [{}])[0]
            delta = choice.get("delta") or {}
            if delta.get("content"):
                content_parts.append(delta["content"])
            for field in CARRIER_FIELDS:
                value = delta.get(field)
                if isinstance(value, str) and value:
                    reasoning_parts.append(value)
                    if not carrier_field:
                        carrier_field = field
                    break
            if delta.get("tool_calls"):
                tool_calls.extend(delta["tool_calls"])
            usage = chunk.get("usage")
            if isinstance(usage, dict) and usage.get("prompt_tokens") is not None:
                prompt_tokens = int(usage["prompt_tokens"])

    if prompt_tokens is None:
        raise RuntimeError(
            "no usage.prompt_tokens in stream (include_usage missing?)"
            + (f"; parse issue: {error_text}" if error_text else "")
        )
    return {
        "content": "".join(content_parts),
        "reasoning": "".join(reasoning_parts),
        "carrier_field": carrier_field,
        "tool_calls": tool_calls,
        "prompt_tokens": prompt_tokens,
    }


def _assistant_variant(
    *,
    content: str,
    tool_calls: list[dict[str, Any]] | None,
    carrier: str,
    reasoning: str,
    mode: str,
) -> dict[str, Any]:
    """Build the assistant history turn for one variant.

    mode: "with" (carrier field), "without" (no carrier), "visible" (reasoning
    text in visible content - same size, validates accounting).
    """
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    if mode == "with" and carrier and reasoning:
        if carrier == "reasoning_details":
            # MiniMax's OpenAI-compatible format carries thinking as an item
            # array; the docs require replaying the field verbatim.
            message[carrier] = [{"type": "reasoning.text", "text": reasoning}]
        else:
            message[carrier] = reasoning
    elif mode == "visible" and reasoning:
        message["content"] = content + "\n\n" + reasoning
    return message


async def _send_stream_raw(
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict[str, Any]],
    *,
    tools: list[dict[str, Any]] | None,
    effort: str,
    max_tokens: int,
) -> dict[str, Any]:
    """One streaming /v1/chat/completions request; returns the aggregated
    assistant delta fields plus usage, for wire-shape inspection."""
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
    url = f"{base_url.rstrip('/')}/v1/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}"}

    fields: dict[str, list[Any]] = {}
    prompt_tokens: int | None = None

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
            for key, value in delta.items():
                if value is None:
                    continue
                fields.setdefault(key, []).append(value)
            usage = chunk.get("usage")
            if isinstance(usage, dict) and usage.get("prompt_tokens") is not None:
                prompt_tokens = int(usage["prompt_tokens"])

    if prompt_tokens is None:
        raise RuntimeError("no usage.prompt_tokens in stream (include_usage missing?)")
    return {"fields": fields, "prompt_tokens": prompt_tokens}


def _describe_field(name: str, parts: list[Any]) -> str:
    """Compact description of one aggregated delta field."""
    if name == "content":
        text = "".join(p for p in parts if isinstance(p, str))
        tags = text.count(" thinking") + text.count("<thinking>")
        return f"len={len(text)} thinking_tags~{tags}"
    if name == "tool_calls":
        return f"calls={len(parts)}"
    if name == "reasoning_details":
        texts = [p.get("text", "") for p in parts if isinstance(p, dict)]
        return f"items={len(parts)} text_len={sum(len(t) for t in texts)}"
    text = "".join(p for p in parts if isinstance(p, str))
    return f"len={len(text)}"


async def _inspect_model(
    base_url: str,
    api_key: str,
    model: str,
    *,
    effort: str,
    max_tokens: int,
) -> dict[str, Any]:
    """Run a real tool-call turn and report every assistant delta field."""
    print(f"\n=== INSPECT {model} (effort={effort}, max_tokens={max_tokens}) ===")
    result = await _send_stream_raw(
        base_url,
        api_key,
        model,
        [{"role": "user", "content": TURN1_TOOL_PROMPT}],
        tools=[TOOL_DEFINITION],
        effort=effort,
        max_tokens=max_tokens,
    )
    fields = result["fields"]
    print(f"  input={result['prompt_tokens']}")
    for name, parts in sorted(fields.items()):
        print(f"  delta field '{name}': {_describe_field(name, parts)}")
    return {"model": model, "fields": sorted(fields)}


async def _measure_shape(
    base_url: str,
    api_key: str,
    model: str,
    *,
    effort: str,
    max_tokens: int,
    carrier: str,
    in_run: bool,
    turn1: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Measure one shape (in-run or cross-run) for one model.

    ``turn1`` may be passed in from the carrier-detection request (in-run
    shape) to avoid a duplicate first request.
    """
    if in_run:
        if turn1 is None:
            turn1 = await _send_stream(
                base_url,
                api_key,
                model,
                [{"role": "user", "content": TURN1_TOOL_PROMPT}],
                tools=[TOOL_DEFINITION],
                effort=effort,
                max_tokens=max_tokens,
            )
        if not turn1["tool_calls"]:
            return {
                "error": "no tool call produced",
                "turn1_input": turn1["prompt_tokens"],
                "turn1_reasoning_len": len(turn1["reasoning"]),
            }
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
        )
        tool_calls = None
        history = [{"role": "user", "content": TURN1_PLAIN_PROMPT}]
        base_content = turn1["content"]

    reasoning = turn1["reasoning"]
    if not reasoning:
        return {
            "error": "no reasoning produced",
            "turn1_input": turn1["prompt_tokens"],
            "turn1_content_len": len(turn1["content"]),
        }

    results: dict[str, int] = {}
    for mode in ("with", "without", "visible"):
        assistant = _assistant_variant(
            content=base_content,
            tool_calls=tool_calls,
            carrier=carrier,
            reasoning=reasoning,
            mode=mode,
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
        result = await _send_stream(
            base_url,
            api_key,
            model,
            messages,
            tools=[TOOL_DEFINITION] if in_run else None,
            effort=effort,
            max_tokens=max_tokens,
        )
        results[mode] = result["prompt_tokens"]

    return {
        "turn1_input": turn1["prompt_tokens"],
        "reasoning_len": len(reasoning),
        "with": results["with"],
        "without": results["without"],
        "visible": results["visible"],
        "delta_with": results["with"] - results["without"],
        "delta_visible": results["visible"] - results["without"],
    }


async def _probe_model(
    base_url: str,
    api_key: str,
    model: str,
    *,
    effort: str,
    max_tokens: int,
    carrier_override: str,
) -> dict[str, Any]:
    """Full measurement for one model: carrier detection + both shapes."""
    print(f"\n=== {model} (effort={effort}, max_tokens={max_tokens}) ===")

    # Detect the emitted carrier from a real tool-call turn.
    detect = await _send_stream(
        base_url,
        api_key,
        model,
        [{"role": "user", "content": TURN1_TOOL_PROMPT}],
        tools=[TOOL_DEFINITION],
        effort=effort,
        max_tokens=max_tokens,
    )
    emitted = carrier_override or detect["carrier_field"]
    print(
        f"  emitted carrier: {emitted or '(none)'} | "
        f"reasoning_len={len(detect['reasoning'])} | input={detect['prompt_tokens']}"
    )

    if carrier_override:
        carriers = [carrier_override]
    else:
        carriers = [emitted] if emitted in VBOT_REPLAY_FIELDS else []
        if emitted and emitted not in VBOT_REPLAY_FIELDS:
            carriers.append(VBOT_REPLAY_FIELDS[0])  # vBot's default replay field
        if not carriers:
            carriers = [VBOT_REPLAY_FIELDS[0]]

    summary: dict[str, Any] = {"model": model, "effort": effort, "carriers": {}}
    for carrier in carriers:
        print(f"  -- carrier: {carrier}")
        in_run = await _measure_shape(
            base_url,
            api_key,
            model,
            effort=effort,
            max_tokens=max_tokens,
            carrier=carrier,
            in_run=True,
            turn1=detect,
        )
        cross_run = await _measure_shape(
            base_url,
            api_key,
            model,
            effort=effort,
            max_tokens=max_tokens,
            carrier=carrier,
            in_run=False,
        )
        summary["carriers"][carrier] = {"in_run": in_run, "cross_run": cross_run}
        print(f"     in_run:    {in_run}")
        print(f"     cross_run: {cross_run}")
    return summary


async def _run(args: argparse.Namespace) -> int:
    api_key = _load_api_key(args.api_key_env, args.data_dir)
    print(f"api key length: {len(api_key)}")
    print(
        f"endpoint: {args.base_url.rstrip('/')}/v1/chat/completions | "
        f"effort: {args.effort} | streaming with include_usage"
    )
    models = args.models.split(",") if args.models else [args.model]
    if args.inspect:
        for model in models:
            model = model.strip()
            if not model:
                continue
            try:
                await _inspect_model(
                    args.base_url,
                    api_key,
                    model,
                    effort=args.effort,
                    max_tokens=args.max_tokens,
                )
            except Exception as exc:  # noqa: BLE001 - sweep must continue
                print(f"  ERROR: {exc}")
        return 0
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
                    effort=args.effort,
                    max_tokens=args.max_tokens,
                    carrier_override=args.carrier,
                )
            )
        except Exception as exc:  # noqa: BLE001 - sweep must continue
            print(f"  ERROR: {exc}")
            summaries.append({"model": model, "error": str(exc)})

    print("\n\n=== SUMMARY ===")
    for summary in summaries:
        if "error" in summary and "carriers" not in summary:
            print(f"{summary['model']}: ERROR {summary['error']}")
            continue
        for carrier, shapes in summary["carriers"].items():
            in_run = shapes["in_run"]
            cross_run = shapes["cross_run"]
            if "error" in in_run:
                print(
                    f"{summary['model']} [{carrier}] in_run: {in_run.get('error')} "
                    f"(input={in_run.get('turn1_input')})"
                )
            else:
                print(
                    f"{summary['model']} [{carrier}] in_run: "
                    f"with={in_run['with']} without={in_run['without']} "
                    f"delta={in_run['delta_with']:+d} (visible {in_run['delta_visible']:+d})"
                )
            if "error" in cross_run:
                print(
                    f"{summary['model']} [{carrier}] cross_run: {cross_run.get('error')} "
                    f"(input={cross_run.get('turn1_input')})"
                )
            else:
                print(
                    f"{summary['model']} [{carrier}] cross_run: "
                    f"with={cross_run['with']} without={cross_run['without']} "
                    f"delta={cross_run['delta_with']:+d} (visible {cross_run['delta_visible']:+d})"
                )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", help="Exact wire model id (or use --models).")
    parser.add_argument("--models", help="Comma-separated model ids for a sweep.")
    parser.add_argument("--carrier", default="", help="Force the replay carrier field.")
    parser.add_argument(
        "--inspect",
        action="store_true",
        help="Only run a real tool-call turn and report every assistant delta "
        "field (wire-shape inspection, no replay measurement).",
    )
    parser.add_argument("--effort", default=DEFAULT_EFFORT)
    parser.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument(
        "--api-key-env",
        default=DEFAULT_API_KEY_ENV,
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
