#!/usr/bin/env python
"""Streaming reasoning-replay probe for Ollama Cloud's two non-OpenAI wires.

The main probe (``probe_reasoning_replay_tokens.py``) covers
``/v1/chat/completions``. This one covers the other two Ollama Cloud wires with
documented reasoning fields:

- ``/api/chat`` (native): assistant messages carry ``thinking``; usage is
  ``prompt_eval_count`` on the final NDJSON line.
- ``/v1/messages`` (Anthropic-compatible): assistant content blocks carry
  ``thinking`` + ``signature``; usage is ``input_tokens`` from the stream.

Same discipline as the main probe: streaming only, verdict = billed input
delta, three variants per shape (with carrier / without / same text in VISIBLE
content as accounting control), both shapes (in-run tool continuation and
cross-run plain history turn).

Prints measurements only; never prints API keys, full prompts, or full
responses.

Examples:
    python scripts/probe_reasoning_replay_alt_wires.py --model minimax-m2.7
    python scripts/probe_reasoning_replay_alt_wires.py --models \\
        "minimax-m2.7,minimax-m3" --wire native
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

ANTHROPIC_VERSION = "2023-06-01"


def _load_api_key(env_name: str, data_dir: Path) -> str:
    env_path = data_dir / ".env"
    if not env_path.is_file():
        raise SystemExit(f"no .env found at {env_path}")
    for line in env_path.read_text(encoding="utf-8").splitlines():
        match = re.match(rf"\s*{re.escape(env_name)}\s*=\s*(.+?)\s*$", line)
        if match:
            return match.group(1).strip().strip('"').strip("'")
    raise SystemExit(f"no {env_name} entry in {env_path}")


def _tool_use_blocks(tool_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert /v1-style tool_calls into Anthropic tool_use content blocks."""
    blocks: list[dict[str, Any]] = []
    for call in tool_calls:
        function = call.get("function") or {}
        try:
            arguments = json.loads(function.get("arguments") or "{}")
        except json.JSONDecodeError:
            arguments = {}
        blocks.append(
            {
                "type": "tool_use",
                "id": call.get("id", "toolu_0"),
                "name": function.get("name", "get_weather"),
                "input": arguments,
            }
        )
    return blocks


async def _send_native_real(
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict[str, Any]],
    *,
    tools: list[dict[str, Any]] | None,
    max_tokens: int,
) -> dict[str, Any]:
    """One streaming /api/chat (NDJSON) request; returns measured facts."""
    if httpx is None:
        raise SystemExit("httpx is required for the probe (pip install httpx)")
    body: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": True,
        "options": {"num_predict": max_tokens},
    }
    if tools:
        body["tools"] = tools
    url = f"{base_url.rstrip('/')}/api/chat"
    headers = {"Authorization": f"Bearer {api_key}"}

    content_parts: list[str] = []
    thinking_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    prompt_eval_count: int | None = None
    error_text = ""

    async with (
        httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client,
        client.stream("POST", url, json=body, headers=headers) as response,
    ):
        if response.status_code != 200:
            raw = (await response.aread()).decode("utf-8", "replace")
            raise RuntimeError(f"HTTP {response.status_code}: {raw[:300]}")
        async for line in response.aiter_lines():
            if not line:
                continue
            try:
                chunk = json.loads(line)
            except json.JSONDecodeError:
                error_text = line[:200]
                continue
            if chunk.get("error"):
                raise RuntimeError(f"stream error: {chunk['error']}")
            message = chunk.get("message") or {}
            if message.get("content"):
                content_parts.append(message["content"])
            thinking = message.get("thinking")
            if isinstance(thinking, str) and thinking:
                thinking_parts.append(thinking)
            if message.get("tool_calls"):
                tool_calls.extend(message["tool_calls"])
            if chunk.get("done"):
                if chunk.get("prompt_eval_count") is not None:
                    prompt_eval_count = int(chunk["prompt_eval_count"])
                break

    if prompt_eval_count is None:
        raise RuntimeError(
            "no prompt_eval_count in final chunk"
            + (f"; parse issue: {error_text}" if error_text else "")
        )
    return {
        "content": "".join(content_parts),
        "reasoning": "".join(thinking_parts),
        "tool_calls": tool_calls,
        "prompt_tokens": prompt_eval_count,
    }


async def _send_anthropic(
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict[str, Any]],
    *,
    tools: list[dict[str, Any]] | None,
    max_tokens: int,
    auth: str = "x-api-key",
) -> dict[str, Any]:
    """One streaming /v1/messages request; returns measured facts."""
    if httpx is None:
        raise SystemExit("httpx is required for the probe (pip install httpx)")
    body: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "stream": True,
    }
    if tools:
        body["tools"] = [
            {
                "name": tool["function"]["name"],
                "description": tool["function"]["description"],
                "input_schema": tool["function"]["parameters"],
            }
            for tool in tools
        ]
    url = f"{base_url.rstrip('/')}/v1/messages"
    if auth == "bearer":
        headers = {
            "Authorization": f"Bearer {api_key}",
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        }
    else:
        headers = {
            "x-api-key": api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        }

    content_parts: list[str] = []
    thinking_parts: list[str] = []
    tool_use_blocks: list[dict[str, Any]] = []
    input_tokens: int | None = None
    error_text = ""

    async with (
        httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client,
        client.stream("POST", url, json=body, headers=headers) as response,
    ):
        if response.status_code != 200:
            raw = (await response.aread()).decode("utf-8", "replace")
            raise RuntimeError(f"HTTP {response.status_code}: {raw[:300]}")
        async for line in response.aiter_lines():
            if not line:
                continue
            if line.startswith("event:"):
                continue
            if line.startswith("data:"):
                payload = line[5:].strip()
                if payload == "[DONE]":
                    continue
                try:
                    event = json.loads(payload)
                except json.JSONDecodeError:
                    error_text = line[:200]
                    continue
                if event.get("type") == "error":
                    raise RuntimeError(f"stream error: {event.get('error')}")
                if event.get("type") == "message_start":
                    usage = event.get("message", {}).get("usage") or {}
                    if usage.get("input_tokens") is not None:
                        input_tokens = int(usage["input_tokens"])
                elif event.get("type") == "content_block_delta":
                    delta = event.get("delta") or {}
                    if delta.get("type") == "text_delta" and delta.get("text"):
                        content_parts.append(delta["text"])
                    elif delta.get("type") == "thinking_delta" and delta.get("thinking"):
                        thinking_parts.append(delta["thinking"])
                elif event.get("type") == "content_block_start":
                    block = event.get("content_block") or {}
                    if block.get("type") == "tool_use":
                        tool_use_blocks.append(
                            {
                                "id": block.get("id", "toolu_0"),
                                "name": block.get("name", ""),
                                "input": block.get("input", {}),
                            }
                        )
                elif event.get("type") == "message_delta":
                    usage = event.get("usage") or {}
                    if usage.get("input_tokens") is not None:
                        input_tokens = int(usage["input_tokens"])

    if input_tokens is None:
        raise RuntimeError(
            "no usage.input_tokens in stream"
            + (f"; parse issue: {error_text}" if error_text else "")
        )
    return {
        "content": "".join(content_parts),
        "reasoning": "".join(thinking_parts),
        "tool_calls": tool_use_blocks,
        "prompt_tokens": input_tokens,
    }


def _assistant_variant(
    *,
    wire: str,
    content: str,
    tool_calls: list[dict[str, Any]] | None,
    reasoning: str,
    mode: str,
) -> dict[str, Any]:
    """Build the assistant history turn for one variant on one wire."""
    if wire == "anthropic":
        blocks: list[dict[str, Any]] = []
        if mode == "with" and reasoning:
            blocks.append({"type": "thinking", "thinking": reasoning, "signature": ""})
        if mode == "visible" and reasoning:
            blocks.append({"type": "text", "text": content + "\n\n" + reasoning})
        elif content:
            blocks.append({"type": "text", "text": content})
        if tool_calls:
            blocks.extend(_thinking_use_blocks(tool_calls))
        return {"role": "assistant", "content": blocks}
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    if mode == "with" and reasoning:
        message["thinking"] = reasoning
    elif mode == "visible" and reasoning:
        message["content"] = content + "\n\n" + reasoning
    return message


def _thinking_use_blocks(tool_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert native-wire tool_calls into Anthropic tool_use blocks."""
    blocks: list[dict[str, Any]] = []
    for call in tool_calls:
        function = call.get("function") or {}
        try:
            arguments = json.loads(function.get("arguments") or "{}")
        except json.JSONDecodeError:
            arguments = {}
        blocks.append(
            {
                "type": "tool_use",
                "id": call.get("id", "toolu_0"),
                "name": function.get("name", "get_weather"),
                "input": arguments,
            }
        )
    return blocks


async def _measure_shape(
    base_url: str,
    api_key: str,
    model: str,
    *,
    wire: str,
    max_tokens: int,
    in_run: bool,
    auth: str = "x-api-key",
) -> dict[str, Any]:
    """Measure one shape (in-run or cross-run) for one model on one wire."""
    send = _send_anthropic if wire == "anthropic" else _send_native_real
    send_kwargs = {"auth": auth} if wire == "anthropic" else {}
    if in_run:
        turn1 = await send(
            base_url,
            api_key,
            model,
            [{"role": "user", "content": TURN1_TOOL_PROMPT}],
            tools=[TOOL_DEFINITION],
            max_tokens=max_tokens,
            **send_kwargs,
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
        turn1 = await send(
            base_url,
            api_key,
            model,
            [{"role": "user", "content": TURN1_PLAIN_PROMPT}],
            tools=None,
            max_tokens=max_tokens,
            **send_kwargs,
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
            wire=wire,
            content=base_content,
            tool_calls=tool_calls,
            reasoning=reasoning,
            mode=mode,
        )
        messages = [*history, assistant]
        if in_run:
            if wire == "anthropic":
                messages.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": tool_calls[0].get("id", "toolu_0"),
                                "content": TOOL_RESULT_CONTENT,
                            }
                        ],
                    }
                )
            else:
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_calls[0].get("id", "tool_call_0"),
                        "content": TOOL_RESULT_CONTENT,
                    }
                )
        messages.append({"role": "user", "content": FOLLOW_UP_PROMPT})
        result = await send(
            base_url,
            api_key,
            model,
            messages,
            tools=[TOOL_DEFINITION] if in_run else None,
            max_tokens=max_tokens,
            **send_kwargs,
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
    wire: str,
    max_tokens: int,
    auth: str = "x-api-key",
) -> dict[str, Any]:
    """Full measurement for one model on one wire."""
    print(f"\n=== {model} (wire={wire}, max_tokens={max_tokens}) ===")
    in_run = await _measure_shape(
        base_url,
        api_key,
        model,
        wire=wire,
        max_tokens=max_tokens,
        in_run=True,
        auth=auth,
    )
    cross_run = await _measure_shape(
        base_url,
        api_key,
        model,
        wire=wire,
        max_tokens=max_tokens,
        in_run=False,
        auth=auth,
    )
    print(f"     in_run:    {in_run}")
    print(f"     cross_run: {cross_run}")
    return {"model": model, "wire": wire, "in_run": in_run, "cross_run": cross_run}


async def _run(args: argparse.Namespace) -> int:
    api_key = _load_api_key(args.api_key_env, args.data_dir)
    print(f"api key length: {len(api_key)}")
    print(f"endpoint: {args.base_url.rstrip('/')}/{args.wire} | streaming")
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
                    wire=args.wire,
                    max_tokens=args.max_tokens,
                    auth=args.auth,
                )
            )
        except Exception as exc:  # noqa: BLE001 - sweep must continue
            print(f"  ERROR: {exc}")
            summaries.append({"model": model, "error": str(exc)})

    print("\n\n=== SUMMARY ===")
    for summary in summaries:
        if "error" in summary and "in_run" not in summary:
            print(f"{summary['model']} [{summary.get('wire', '?')}]: ERROR {summary['error']}")
            continue
        for shape_name in ("in_run", "cross_run"):
            shape = summary[shape_name]
            if "error" in shape:
                print(
                    f"{summary['model']} [{summary['wire']}] {shape_name}: "
                    f"{shape.get('error')} (input={shape.get('turn1_input')})"
                )
            else:
                print(
                    f"{summary['model']} [{summary['wire']}] {shape_name}: "
                    f"with={shape['with']} without={shape['without']} "
                    f"delta={shape['delta_with']:+d} (visible {shape['delta_visible']:+d})"
                )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", help="Exact wire model id (or use --models).")
    parser.add_argument("--models", help="Comma-separated model ids for a sweep.")
    parser.add_argument(
        "--wire",
        choices=("native", "anthropic"),
        default="anthropic",
        help="Wire to probe: native /api/chat or Anthropic /v1/messages.",
    )
    parser.add_argument(
        "--auth",
        choices=("x-api-key", "bearer"),
        default="x-api-key",
        help="Auth header for the Anthropic wire (opencode-go/ollama use bearer).",
    )
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
