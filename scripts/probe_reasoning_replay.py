#!/usr/bin/env python
"""Probe whether a provider reads replayed assistant reasoning (full_history vs current_run).

The probe runs a contamination-proof secret-number test:

- Turn 1 asks the model to write a random 5-digit number ONLY into its
  reasoning and to answer with exactly one word (or to call a tool).
- Turn 2 replays the turn-1 assistant message three ways and asks for the
  number: with the reasoning carrier, without it, and with the number in the
  visible content (method control). If the control recalls the number but the
  carrier does not, the engine ignores replayed reasoning — `full_history`
  replay is dead weight and `current_run` is the correct policy.

Scenarios:
- ``tool_loop``: turn 1 includes a tool call, turn 2 continues after the tool
  result — the in-run continuation case (``current_run`` scope).
- ``cross_turn``: plain two-turn continuation (``full_history`` scope).

The probe prints measurements only: secrets, carrier lengths, per-case recall
flags. It never prints API keys, full prompts, or full responses.

Examples:
    python scripts/probe_reasoning_replay.py --provider ollama-cloud --model glm-5.2
    python scripts/probe_reasoning_replay.py --provider ollama-cloud \\
        --model deepseek-v4-flash:0731 --scenario tool_loop
    python scripts/probe_reasoning_replay.py --provider opencode-go --model glm-5.3 \\
        --api-key-env OPENCODE_GO_API_KEY
    python scripts/probe_reasoning_replay.py --base-url https://ollama.com \\
        --endpoint native --carrier thinking
    python scripts/probe_reasoning_replay.py --provider ollama-cloud \\
        --model minimax-m3 --repeats 2 --effort high
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    from curl_cffi import requests as _curl_requests
except ImportError:  # pragma: no cover - degraded mode
    _curl_requests = None  # type: ignore[assignment]
curl_requests: Any = _curl_requests

DEFAULT_PROVIDER = "ollama-cloud"
DEFAULT_MODEL = "glm-5.2"
DEFAULT_DATA_DIR = Path.home() / ".vbot"
DEFAULT_API_KEY_ENV = "OLLAMA_API_KEY"
DEFAULT_EFFORT = "max"
DEFAULT_MAX_TOKENS = 4000
DEFAULT_REPEATS = 3

PROVIDER_ENDPOINTS: dict[str, tuple[str, str]] = {
    "ollama-cloud": ("https://ollama.com", "https://ollama.com/v1"),
    "opencode-go": ("https://opencode.ai/zen/go/v1", "https://opencode.ai/zen/go/v1"),
}
CARRIER_FIELDS = ("reasoning", "reasoning_content", "thinking")
REASONING_EFFORT_FIELDS = ("reasoning", "reasoning_content")

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
    "Pick a random 5-digit number. Write that number in your reasoning so it is "
    "recorded, but do NOT write it in your answer. Then call the get_weather tool "
    "with city='Berlin'. Your final content must be empty."
)
TURN1_PLAIN_PROMPT = (
    "Pick a random 5-digit number. Write that number in your reasoning so it is "
    "recorded, but do NOT write it in your answer. Reply with exactly one word: ok."
)
ASK_PROMPT = (
    "What was the exact 5-digit number you wrote in your reasoning in the first "
    "turn? If you do not know it, answer exactly UNKNOWN."
)


@dataclass(frozen=True)
class TurnResult:
    reasoning: str
    content: str
    tool_calls: list[dict[str, Any]]
    carrier_field: str = "reasoning_content"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--provider", default=DEFAULT_PROVIDER, help="Provider id for default endpoint resolution."
    )
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Exact wire model id.")
    parser.add_argument("--base-url", help="Explicit base URL; overrides the provider default.")
    parser.add_argument(
        "--endpoint",
        choices=("auto", "openai", "native"),
        default="auto",
        help="Wire shape: openai=/v1/chat/completions, native=/api/chat (Ollama).",
    )
    parser.add_argument(
        "--carrier",
        choices=("auto", *CARRIER_FIELDS),
        default="auto",
        help="Reasoning carrier field for the replay; auto scans the turn-1 response.",
    )
    parser.add_argument(
        "--scenario",
        choices=("tool_loop", "cross_turn"),
        default="cross_turn",
        help="tool_loop = in-run tool continuation; cross_turn = next-run continuation.",
    )
    parser.add_argument("--repeats", type=int, default=DEFAULT_REPEATS, help="Rounds per scenario.")
    parser.add_argument("--effort", default=DEFAULT_EFFORT, help="reasoning_effort / think value.")
    parser.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    parser.add_argument(
        "--api-key-env",
        default=DEFAULT_API_KEY_ENV,
        help="Name of the API key variable read from <data-dir>/.env. Never printed.",
    )
    parser.add_argument(
        "--data-dir", type=Path, default=DEFAULT_DATA_DIR, help="Data dir whose .env holds the key."
    )
    parser.add_argument(
        "--impersonate",
        default="chrome",
        help=(
            "curl_cffi TLS/browser impersonation for Cloudflare-protected "
            "gateways; 'none' disables it."
        ),
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=180.0,
        help="Per-request timeout in seconds.",
    )
    return parser


def _load_api_key(env_name: str, data_dir: Path) -> str:
    env_path = data_dir / ".env"
    if not env_path.is_file():
        raise SystemExit(f"no .env found at {env_path}")
    for line in env_path.read_text(encoding="utf-8").splitlines():
        match = re.match(rf"\s*{re.escape(env_name)}\s*=\s*(.+?)\s*$", line)
        if match:
            return match.group(1).strip().strip('"').strip("'")
    raise SystemExit(f"no {env_name} entry in {env_path}")


def _resolve_endpoint(provider: str, base_url: str | None, endpoint: str) -> tuple[str, str]:
    native_base, openai_base = PROVIDER_ENDPOINTS.get(provider, ("", ""))
    if base_url:
        native_base = base_url
        openai_base = base_url.rstrip("/") + (
            "/v1" if not base_url.rstrip("/").endswith("/v1") else ""
        )
    if endpoint == "native":
        if not native_base:
            raise SystemExit(f"provider {provider} has no default native endpoint; pass --base-url")
        return f"{native_base.rstrip('/')}/api/chat", "native"
    if endpoint == "openai":
        if not openai_base:
            raise SystemExit(f"provider {provider} has no default OpenAI endpoint; pass --base-url")
        return f"{openai_base.rstrip('/')}/chat/completions", "openai"
    # auto: prefer the provider's OpenAI-compatible route (verified for both
    # ollama-cloud and opencode-go); native is reachable via --endpoint native.
    if not openai_base:
        raise SystemExit(f"provider {provider} has no known endpoint; pass --base-url")
    return f"{openai_base.rstrip('/')}/chat/completions", "openai"


def _send(
    url: str,
    api_key: str,
    payload: dict[str, Any],
    *,
    timeout: float,
    impersonate: str | None,
) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    if impersonate and curl_requests is not None:
        response = curl_requests.post(
            url,
            json=payload,
            headers=headers,
            impersonate=impersonate,  # type: ignore[arg-type]
            timeout=timeout,
        )
    else:
        import urllib.error
        import urllib.request

        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return dict(json.loads(response.read().decode("utf-8")))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {exc.code}: {body[:300]}") from exc
    if response.status_code >= 400:
        raise RuntimeError(f"HTTP {response.status_code}: {response.text[:300]}")
    return dict(response.json())


def _openai_payload(
    model: str,
    messages: list[dict[str, Any]],
    *,
    effort: str,
    max_tokens: int,
    tools: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "stream": False,
        "temperature": 0.0,
        "reasoning_effort": effort,
    }
    if tools:
        payload["tools"] = tools
    return payload


def _native_payload(
    model: str,
    messages: list[dict[str, Any]],
    *,
    effort: str,
    max_tokens: int,
    tools: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": False,
        "think": effort,
        "options": {"num_predict": max_tokens},
    }
    if tools:
        payload["tools"] = tools
    return payload


def _extract_openai_message(message: dict[str, Any]) -> TurnResult:
    reasoning = ""
    carrier_field = "reasoning_content"
    for field in CARRIER_FIELDS:
        value = message.get(field)
        if isinstance(value, str) and value:
            reasoning = value
            carrier_field = field
            break
    return TurnResult(
        reasoning=reasoning,
        content=message.get("content") or "",
        tool_calls=message.get("tool_calls") or [],
        carrier_field=carrier_field,
    )


def _extract_native_message(result: dict[str, Any]) -> TurnResult:
    message = result.get("message", {})
    return TurnResult(
        reasoning=message.get("thinking") or "",
        content=message.get("content") or "",
        tool_calls=message.get("tool_calls") or [],
        carrier_field="thinking",
    )


def _carrier_of(result: TurnResult, wire: str) -> str:
    """Return the carrier field the engine actually used in this response."""
    if wire == "native":
        return "thinking"
    return result.carrier_field


def _find_secret(reasoning: str) -> str | None:
    numbers = re.findall(r"\b\d{5}\b", reasoning)
    return numbers[0] if numbers else None


def _replay_tool_calls(tool_calls: list[dict[str, Any]], wire: str) -> list[dict[str, Any]]:
    replayed: list[dict[str, Any]] = []
    for position, call in enumerate(tool_calls):
        if not isinstance(call, dict):
            continue
        function = call.get("function", {})
        if not isinstance(function, dict):
            function = {}
        name = function.get("name") or ""
        arguments = function.get("arguments") or {}
        if wire == "openai":
            if isinstance(arguments, dict):
                arguments = json.dumps(arguments, separators=(",", ":"))
            replayed.append(
                {
                    "id": call.get("id") or f"call_{position}",
                    "type": "function",
                    "function": {"name": name, "arguments": arguments},
                }
            )
        else:
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError:
                    arguments = {}
            replayed.append(
                {
                    "id": call.get("id") or f"call_{position}",
                    "function": {"name": name, "arguments": arguments},
                }
            )
    return replayed


def _run_turn(
    url: str,
    wire: str,
    api_key: str,
    *,
    model: str,
    messages: list[dict[str, Any]],
    effort: str,
    max_tokens: int,
    tools: list[dict[str, Any]] | None,
    timeout: float,
    impersonate: str | None,
) -> TurnResult:
    if wire == "native":
        payload = _native_payload(
            model, messages, effort=effort, max_tokens=max_tokens, tools=tools
        )
        result = _send(url, api_key, payload, timeout=timeout, impersonate=impersonate)
        return _extract_native_message(result)
    payload = _openai_payload(model, messages, effort=effort, max_tokens=max_tokens, tools=tools)
    result = _send(url, api_key, payload, timeout=timeout, impersonate=impersonate)
    message = (result.get("choices") or [{}])[0].get("message", {})
    return _extract_openai_message(message)


def _print_case(label: str, answer: str, secret: str, reasoning2: str) -> bool:
    hit = secret in answer or secret in reasoning2
    print(
        f"  {label}: answer={answer[:45]!r} | secret_in_answer={secret in answer} "
        f"| secret_in_reasoning2={secret in reasoning2} | reasoning2_len={len(reasoning2)}"
    )
    return hit


def _run_cross_turn(
    url: str,
    wire: str,
    api_key: str,
    *,
    model: str,
    effort: str,
    max_tokens: int,
    timeout: float,
    impersonate: str | None,
    carrier: str,
) -> tuple[bool, bool, bool] | None:
    turn1 = _run_turn(
        url,
        wire,
        api_key,
        model=model,
        messages=[{"role": "user", "content": TURN1_PLAIN_PROMPT}],
        effort=effort,
        max_tokens=max_tokens,
        tools=None,
        timeout=timeout,
        impersonate=impersonate,
    )
    secret = _find_secret(turn1.reasoning)
    if secret is None:
        print(f"  turn1 invalid: no 5-digit number in reasoning (len={len(turn1.reasoning)})")
        print(f"  reasoning: {turn1.reasoning[:200]!r}")
        return None
    print(
        f"  turn1: secret={secret} reasoning_len={len(turn1.reasoning)} "
        f"content={turn1.content[:40]!r}"
    )

    def turn2(assistant_msg: dict[str, Any], label: str) -> bool:
        history = [
            {"role": "user", "content": TURN1_PLAIN_PROMPT},
            assistant_msg,
            {"role": "user", "content": ASK_PROMPT},
        ]
        turn2_result = _run_turn(
            url,
            wire,
            api_key,
            model=model,
            messages=history,
            effort=effort,
            max_tokens=max_tokens,
            tools=None,
            timeout=timeout,
            impersonate=impersonate,
        )
        return _print_case(label, turn2_result.content, secret, turn2_result.reasoning)

    hit_a = turn2(
        {"role": "assistant", "content": "", carrier: turn1.reasoning}, "with carrier    "
    )
    hit_b = turn2({"role": "assistant", "content": ""}, "without carrier")
    hit_c = turn2(
        {"role": "assistant", "content": f"I recorded the number {secret}."}, "visible control "
    )
    print(f"  verdict: with={hit_a} without={hit_b} control={hit_c}")
    return hit_a, hit_b, hit_c


def _run_tool_loop(
    url: str,
    wire: str,
    api_key: str,
    *,
    model: str,
    effort: str,
    max_tokens: int,
    timeout: float,
    impersonate: str | None,
    carrier: str,
) -> tuple[bool, bool, bool] | None:
    turn1 = _run_turn(
        url,
        wire,
        api_key,
        model=model,
        messages=[{"role": "user", "content": TURN1_TOOL_PROMPT}],
        effort=effort,
        max_tokens=max_tokens,
        tools=[TOOL_DEFINITION],
        timeout=timeout,
        impersonate=impersonate,
    )
    secret = _find_secret(turn1.reasoning)
    if secret is None or not turn1.tool_calls:
        print(
            f"  turn1 invalid: reasoning_len={len(turn1.reasoning)} "
            f"tool_calls={len(turn1.tool_calls)}"
        )
        print(f"  reasoning: {turn1.reasoning[:200]!r}")
        return None
    print(
        f"  turn1: secret={secret} reasoning_len={len(turn1.reasoning)} "
        f"tool_calls={len(turn1.tool_calls)}"
    )

    replayed_calls = _replay_tool_calls(turn1.tool_calls, wire)
    tool_result = {
        "role": "tool",
        "tool_call_id": replayed_calls[0].get("id", "call_1"),
        "content": '{"temperature": 20, "condition": "sunny"}',
    }

    def turn2(assistant_msg: dict[str, Any], label: str) -> bool:
        history = [
            {"role": "user", "content": TURN1_TOOL_PROMPT},
            assistant_msg,
            tool_result,
            {"role": "user", "content": ASK_PROMPT},
        ]
        turn2_result = _run_turn(
            url,
            wire,
            api_key,
            model=model,
            messages=history,
            effort=effort,
            max_tokens=max_tokens,
            tools=None,
            timeout=timeout,
            impersonate=impersonate,
        )
        return _print_case(label, turn2_result.content, secret, turn2_result.reasoning)

    hit_a = turn2(
        {
            "role": "assistant",
            "content": "",
            carrier: turn1.reasoning,
            "tool_calls": replayed_calls,
        },
        "with carrier    ",
    )
    hit_b = turn2(
        {"role": "assistant", "content": "", "tool_calls": replayed_calls}, "without carrier"
    )
    hit_c = turn2(
        {
            "role": "assistant",
            "content": f"I recorded the number {secret}.",
            "tool_calls": replayed_calls,
        },
        "visible control ",
    )
    print(f"  verdict: with={hit_a} without={hit_b} control={hit_c}")
    return hit_a, hit_b, hit_c


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    api_key = _load_api_key(args.api_key_env, args.data_dir)
    print(f"api key length: {len(api_key)}")

    url, wire = _resolve_endpoint(args.provider, args.base_url, args.endpoint)
    print(
        f"endpoint: {url} ({wire}) | model: {args.model} | "
        f"effort: {args.effort} | scenario: {args.scenario}"
    )

    # One calibration turn determines the observed carrier when --carrier auto.
    calibration = _run_turn(
        url,
        wire,
        api_key,
        model=args.model,
        messages=[{"role": "user", "content": "Reply with exactly one word: ok."}],
        effort=args.effort,
        max_tokens=args.max_tokens,
        tools=None,
        timeout=args.timeout,
        impersonate=args.impersonate,
    )
    if calibration.reasoning:
        observed = _carrier_of(calibration, wire)
        print(
            f"calibration: reasoning returned as '{observed}' ({len(calibration.reasoning)} chars)"
        )
    else:
        observed = "thinking" if wire == "native" else REASONING_EFFORT_FIELDS[0]
        print(f"calibration: no reasoning returned; using default carrier '{observed}'")
    carrier = args.carrier if args.carrier != "auto" else observed

    results: list[tuple[bool, bool, bool]] = []
    runner = _run_tool_loop if args.scenario == "tool_loop" else _run_cross_turn
    for index in range(1, args.repeats + 1):
        print(f"\n--- round {index} ---")
        result = runner(
            url,
            wire,
            api_key,
            model=args.model,
            effort=args.effort,
            max_tokens=args.max_tokens,
            timeout=args.timeout,
            impersonate=args.impersonate,
            carrier=carrier,
        )
        if result is not None:
            results.append(result)

    print("\n=== SUMMARY ===")
    for index, (hit_a, hit_b, hit_c) in enumerate(results, 1):
        print(f"round {index}: with_carrier={hit_a} | without={hit_b} | control={hit_c}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
