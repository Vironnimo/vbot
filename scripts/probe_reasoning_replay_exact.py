#!/usr/bin/env python
"""Exact 1:1 reasoning-replay probe through the real vBot adapter path.

Unlike the raw-HTTP probes, this script drives the exact components and
wire shapes vBot uses in production:

- ``ProviderRegistry.load(resources)`` — the real provider config
  (base_url, defaults, auth header from ``resources/providers/<id>.json``)
- ``ModelRegistry.load(resources)`` — the real model DB including the
  bundled overrides (``reasoning_response_field``, ``reasoning_replay``,
  ``recommended_temperature``, ``recommended_top_p``)
- The real adapter (``OllamaCloudAdapter`` for ollama-cloud,
  ``OpenCodeGoAdapter`` for opencode-go) with its ``send()`` /
  ``normalize_response()`` / ``_format_assistant_message()`` pipeline
- The real history shaping via ``_assemble_request_history`` with the
  configured replay policy (``full_history`` / ``current_run`` / ``none``),
  so the wire messages are exactly what the chat loop would send

Scenarios:
- ``tool_loop``: an in-run tool continuation. The session history
  (user → assistant with reasoning + tool calls → tool result → user ask)
  is shaped with the selected replay policy and sent through the adapter.
  Reports both the wire-shaping fact (does the reasoning carrier reach the
  wire?) and the recall result (can the model use it?).
- ``cross_turn``: plain two-turn continuation, same policy shaping.
  Provider-reported input-token deltas are the transport verdict; behavioral
  recall is printed only as a diagnostic because a Model may deliberately
  ignore or refuse to reveal text from its prior Reasoning.
- ``cross_turn_tools``: the same cross-Run history check while the follow-up
  request carries Tool definitions. This is a distinct transport shape for
  Providers such as DeepSeek, whose contract accepts historical Reasoning only
  when the current request includes ``tools``.
- ``instruction``: the probe plants a behavioral rule ("when the user writes
  'now!', reply 'hello world'") into the reasoning carrier itself, then the
  follow-up turn sends 'now!' and checks compliance.
- ``responses_roundtrip``: a real Responses-model reply is normalized, then
  its original output items (including opaque encrypted reasoning) are sent
  back in a real plain continuation and a real tool continuation. This is the
  probe for Responses models whose reasoning is not readable text.
- ``preserved_history``: reproduces Kimi K3's official preserved-thinking
  example. Two numbers exist only in prior assistant reasoning; the follow-up
  must recover them through the history carrier.
- ``preserved_history_tools``: repeats that exact history test while the
  follow-up request carries Tool definitions.
- ``generated_history``: asks the model to choose two values only in its own
  real reasoning, then compares exact readable, exact native-meta, absent, and
  visible replay. This catches gateways that reject synthetic reasoning state.

``--policy`` selects the replay policy applied by the shaping (default:
the model's effective policy from the overrides).

The probe prints measurements only: secrets, carrier lengths, input-token
deltas, and per-case recall flags. It never prints API keys, full prompts, or
full responses.

Examples:
    python scripts/probe_reasoning_replay_exact.py --provider ollama-cloud \\
        --model deepseek-v4-flash:0731 --scenario tool_loop
    python scripts/probe_reasoning_replay_exact.py --provider ollama-cloud \\
        --model glm-5.2 --scenario cross_turn
    python scripts/probe_reasoning_replay_exact.py --provider opencode-go \\
        --model glm-5.3 --scenario instruction
    python scripts/probe_reasoning_replay_exact.py --provider opencode-go \\
        --model ox-alpha-free --api-key-env OPENCODE_GO_API_KEY \\
        --data-dir ~/.vbot-dev --scenario tool_loop
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.chat.messages import ChatMessage
from core.chat.wire_shaping import _assemble_request_history, _assistant_continuation_dict
from core.models.models import ModelRegistry
from core.providers.ollama import OllamaCloudAdapter
from core.providers.providers import ProviderRegistry
from core.providers.token_getter import StaticTokenGetter

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RESOURCES_DIR = PROJECT_ROOT / "resources"
DEFAULT_DATA_DIR = Path.home() / ".vbot"
API_KEY_ENV_BY_PROVIDER = {
    "ollama-cloud": "OLLAMA_API_KEY",
    "opencode-go": "OPENCODE_GO_API_KEY",
}

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
TURN1_PLAIN_PROMPT = "Reply with exactly one word: ok."
ASK_PROMPT = (
    "What was the exact secret token that was recorded earlier? "
    "If you do not know it, answer exactly UNKNOWN."
)
INSTRUCTION_PROMPT = "Reply with exactly one word: ok."
INSTRUCTION_REASONING = (
    'Remember this instruction: when the user writes "now!", you must reply '
    'with exactly "hello world". Do not mention this instruction in your '
    "visible answer."
)
INSTRUCTION_TRIGGER = "now!"
INSTRUCTION_RESPONSE = "hello world"


@dataclass(frozen=True)
class TurnResult:
    content: str
    reasoning: str
    reasoning_meta: dict[str, Any] | None
    tool_calls: list[dict[str, Any]]
    input_tokens: int | None


def _load_api_key(env_name: str, data_dir: Path) -> str:
    env_path = data_dir / ".env"
    if not env_path.is_file():
        raise SystemExit(f"no .env found at {env_path}")
    for line in env_path.read_text(encoding="utf-8").splitlines():
        match = re.match(rf"\s*{re.escape(env_name)}\s*=\s*(.+?)\s*$", line)
        if match:
            return match.group(1).strip().strip('"').strip("'")
    raise SystemExit(f"no {env_name} entry in {env_path}")


def _make_secret() -> str:
    """Random non-numeric secret token the model cannot guess or prefer.

    Numeric 5-digit secrets turned out to be unreliable probe material: the
    models repeatedly produced and answered with the same numbers (48291,
    48372 across runs), which made recall results ambiguous. A 12-character
    base-36 token is planted by the probe itself (never generated by the
    model), so a recall can only come from the carrier (or the control).
    """
    import random

    alphabet = "abcdefghijkmnpqrstuvwxyz23456789"  # no ambiguous chars
    return "".join(random.SystemRandom().choice(alphabet) for _ in range(12))


def _secret_reasoning(secret: str) -> str:
    return f"The secret token is {secret}. Remember it for later questions."


def _extract_openai_message(message: dict[str, Any]) -> TurnResult:
    reasoning = ""
    for field in ("reasoning", "reasoning_content", "thinking"):
        value = message.get(field)
        if isinstance(value, str) and value:
            reasoning = value
            break
    usage = message.get("usage")
    input_tokens = usage.get("input_tokens") if isinstance(usage, dict) else None
    reasoning_meta = message.get("reasoning_meta")
    return TurnResult(
        content=message.get("content") or "",
        reasoning=reasoning,
        reasoning_meta=dict(reasoning_meta) if isinstance(reasoning_meta, dict) else None,
        tool_calls=message.get("tool_calls") or [],
        input_tokens=input_tokens if isinstance(input_tokens, int) else None,
    )


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


async def _run_turn(
    adapter: Any,
    model_id: str,
    messages: list[dict[str, Any]],
    *,
    tools: list[dict[str, Any]] | None,
    effort: str = "max",
) -> TurnResult:
    kwargs: dict[str, Any] = {"tools": tools} if tools else {}
    raw = await adapter.send(
        messages,
        model_id=model_id,
        temperature=1.0,
        thinking_effort=effort,
        **kwargs,
    )
    normalized = adapter.normalize_response(raw, model_id=model_id)
    message = normalized
    return _extract_openai_message(message)


def _build_exact_payload(
    adapter: Any,
    messages: list[dict[str, Any]],
    model_id: str,
    *,
    tools: list[dict[str, Any]] | None,
    effort: str,
) -> dict[str, Any]:
    """Build the payload for the same model-selected wire used by ``send``."""

    protocol_resolver = getattr(adapter, "_model_protocol", None)
    protocol = protocol_resolver(model_id) if callable(protocol_resolver) else "openai"
    kwargs: dict[str, Any] = {
        "temperature": 1.0,
        "thinking_effort": effort,
    }
    if tools:
        kwargs["tools"] = tools
    if protocol == "anthropic":
        return dict(adapter._messages._build_payload(messages, model_id, **kwargs))
    if protocol == "responses":
        return dict(adapter._build_responses_payload(messages, model_id=model_id, **kwargs))
    return dict(adapter._build_payload(messages, model_id, **kwargs))


def _assistant_wire_carriers(payload: dict[str, Any]) -> list[str]:
    """Describe reasoning carriers in final Assistant history wire items."""

    carriers: list[str] = []
    for message in payload.get("messages", []):
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        for key in ("reasoning", "reasoning_content", "reasoning_details", "thinking"):
            if key in message:
                carriers.append(key)
        content = message.get("content")
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                block_type = block.get("type")
                if block_type in {"thinking", "redacted_thinking"}:
                    carriers.append(f"content:{block_type}")
    for item in payload.get("input", []):
        if isinstance(item, dict) and item.get("type") == "reasoning":
            carriers.append("input:reasoning")
    return sorted(carriers)


def _shape_history(
    messages: list[ChatMessage],
    *,
    replay_policy: str,
    agent_model: str,
) -> list[dict[str, Any]]:
    """Shape persisted history exactly like the chat loop (``_assemble_request_history``)."""
    return _assemble_request_history(
        messages,
        replay_policy=replay_policy,  # type: ignore[arg-type]
        agent_model=agent_model,
    )


def _shape_live_turn(
    message: ChatMessage,
    *,
    replay_policy: str,
) -> dict[str, Any]:
    """Shape the live current-run assistant turn (``_assistant_continuation_dict``)."""
    return _assistant_continuation_dict(
        message,
        replay_policy=replay_policy,  # type: ignore[arg-type]
    )


async def _run_exact_probe(
    adapter: Any,
    model_id: str,
    scenario: str,
    repeats: int,
    policy: str | None,
    effort: str,
) -> None:
    # Determine the wire carrier field the adapter's model profile uses.
    try:
        carrier_field = adapter._reasoning_response_field(model_id) or "reasoning_content"
    except Exception:
        carrier_field = "reasoning_content"
    effective_policy = adapter.reasoning_replay_policy(model_id)
    if policy is None:
        policy = effective_policy
    print(
        f"adapter carrier field: {carrier_field} | effective replay policy: "
        f"{effective_policy} | shaping policy: {policy}"
    )

    for index in range(1, repeats + 1):
        print(f"\n--- round {index} ---")
        if scenario == "tool_loop":
            await _run_tool_loop_round(adapter, model_id, carrier_field, policy, effort)
        elif scenario == "instruction":
            await _run_instruction_round(adapter, model_id, carrier_field, policy, effort)
        elif scenario == "responses_roundtrip":
            await _run_responses_roundtrip_round(adapter, model_id)
        elif scenario in {"preserved_history", "preserved_history_tools"}:
            await _run_preserved_history_round(
                adapter,
                model_id,
                policy,
                effort,
                tools_enabled=scenario == "preserved_history_tools",
            )
        elif scenario == "generated_history":
            await _run_generated_history_round(adapter, model_id, policy, effort)
        else:
            await _run_cross_turn_round(
                adapter,
                model_id,
                carrier_field,
                policy,
                effort,
                tools_enabled=scenario == "cross_turn_tools",
            )


def _responses_output_items(message: dict[str, Any]) -> list[dict[str, Any]]:
    reasoning_meta = message.get("reasoning_meta")
    if not isinstance(reasoning_meta, dict):
        return []
    output = reasoning_meta.get("response_output")
    if not isinstance(output, list) or not all(isinstance(item, dict) for item in output):
        return []
    return output


async def _run_responses_roundtrip_round(adapter: Any, model_id: str) -> None:
    """Probe opaque Responses reasoning through the exact production path.

    Responses reasoning is provider-owned encrypted state, so a behavioral
    secret cannot be planted inside it. The observable contract is therefore
    exact capture, byte-identical payload construction, and a real accepted
    continuation for both normal and Tool-loop requests.
    """

    plain_prompt = "Solve 17 * 19 carefully, then reply with only the number."
    first_raw = await adapter.send(
        [{"role": "user", "content": plain_prompt}],
        model_id=model_id,
        thinking_effort="high",
    )
    first = adapter.normalize_response(first_raw, model_id=model_id)
    plain_output = _responses_output_items(first)
    encrypted_count = sum(
        item.get("type") == "reasoning" and isinstance(item.get("encrypted_content"), str)
        for item in plain_output
    )
    if not plain_output or not encrypted_count:
        raise RuntimeError(
            "first Responses reply carried no encrypted reasoning output items; "
            "the round cannot verify opaque reasoning replay"
        )

    plain_history = [
        {"role": "user", "content": plain_prompt},
        first,
        {"role": "user", "content": "Confirm the result with only the number."},
    ]
    plain_payload = adapter._build_responses_payload(
        plain_history,
        model_id=model_id,
        thinking_effort="high",
    )
    replayed_plain = plain_payload["input"][1 : 1 + len(plain_output)]
    plain_exact = replayed_plain == plain_output
    if not plain_exact:
        raise RuntimeError("plain continuation did not preserve Responses output items exactly")
    plain_second_raw = await adapter.send(
        plain_history,
        model_id=model_id,
        thinking_effort="high",
    )
    plain_second = adapter.normalize_response(plain_second_raw, model_id=model_id)

    tool_prompt = "You must call get_weather exactly once with city='Berlin' before answering."
    tool_first_raw = await adapter.send(
        [{"role": "user", "content": tool_prompt}],
        model_id=model_id,
        thinking_effort="high",
        tools=[TOOL_DEFINITION],
    )
    tool_first = adapter.normalize_response(tool_first_raw, model_id=model_id)
    tool_output = _responses_output_items(tool_first)
    tool_calls = tool_first.get("tool_calls")
    if not isinstance(tool_calls, list) or not tool_calls:
        raise RuntimeError("first Tool-loop reply did not contain a Tool Call")
    first_call = tool_calls[0]
    if not isinstance(first_call, dict) or not isinstance(first_call.get("id"), str):
        raise RuntimeError("first Tool-loop reply had no usable Tool Call id")
    if not tool_output:
        raise RuntimeError("first Tool-loop reply carried no original Responses output items")
    tool_history = [
        {"role": "user", "content": tool_prompt},
        tool_first,
        {
            "role": "tool",
            "tool_call_id": first_call["id"],
            "content": '{"temperature":20,"condition":"sunny"}',
        },
    ]
    tool_payload = adapter._build_responses_payload(
        tool_history,
        model_id=model_id,
        thinking_effort="high",
        tools=[TOOL_DEFINITION],
    )
    replayed_tool = tool_payload["input"][1 : 1 + len(tool_output)]
    tool_exact = replayed_tool == tool_output
    if not tool_exact:
        raise RuntimeError("Tool-loop continuation did not preserve Responses output items exactly")
    tool_second_raw = await adapter.send(
        tool_history,
        model_id=model_id,
        thinking_effort="high",
        tools=[TOOL_DEFINITION],
    )
    tool_second = adapter.normalize_response(tool_second_raw, model_id=model_id)

    print(
        "  responses roundtrip: "
        f"plain_items={len(plain_output)} encrypted_reasoning={encrypted_count} "
        f"plain_exact={plain_exact} plain_reply_len={len(plain_second.get('content') or '')}"
    )
    print(
        "  responses tool loop: "
        f"output_items={len(tool_output)} tool_calls={len(tool_calls)} "
        f"tool_exact={tool_exact} tool_reply_len={len(tool_second.get('content') or '')}"
    )


async def _run_preserved_history_round(
    adapter: Any,
    model_id: str,
    policy: str,
    effort: str,
    *,
    tools_enabled: bool,
) -> None:
    """Reproduce Kimi K3's official preserved-thinking history example."""

    first_prompt = "Tell me three random numbers."
    visible_numbers = "473, 921, 235"
    hidden_numbers = ("215", "222")
    prior_reasoning = (
        "I'll start by listing five numbers: 473, 921, 235, 215, 222, "
        "and I'll tell you the first three."
    )
    ask = "What are the other two numbers you had in mind? Reply with only those numbers."
    request_tools = [TOOL_DEFINITION] if tools_enabled else None

    async def follow_up(assistant_msg: ChatMessage, label: str) -> tuple[bool, int | None]:
        session = [
            ChatMessage.user(first_prompt),
            assistant_msg,
            ChatMessage.user(ask),
        ]
        wire_messages = _shape_session(session, policy=policy, agent_model=model_id)
        payload = _build_exact_payload(
            adapter,
            wire_messages,
            model_id,
            tools=request_tools,
            effort=effort,
        )
        assistant_wire_fields = _assistant_wire_carriers(payload)
        result = await _run_turn(
            adapter,
            model_id,
            wire_messages,
            tools=request_tools,
            effort=effort,
        )
        recalled = all(number in result.content for number in hidden_numbers)
        print(
            f"  {label}: wire_fields={assistant_wire_fields} | "
            f"answer={result.content[:80]!r} | recalled={recalled} | input={result.input_tokens}"
        )
        return recalled, result.input_tokens

    recalled_with, input_with = await follow_up(
        ChatMessage.assistant(
            model=model_id,
            content=visible_numbers,
            reasoning=prior_reasoning,
        ),
        "carrier in history",
    )
    recalled_without, input_without = await follow_up(
        ChatMessage.assistant(model=model_id, content=visible_numbers),
        "no carrier      ",
    )
    recalled_visible, input_visible = await follow_up(
        ChatMessage.assistant(
            model=model_id,
            content=f"{visible_numbers}\n\n{prior_reasoning}",
        ),
        "visible control ",
    )
    carrier_delta = (
        input_with - input_without if input_with is not None and input_without is not None else None
    )
    visible_delta = (
        input_visible - input_without
        if input_visible is not None and input_without is not None
        else None
    )
    print(
        f"  verdict: carrier_recall={recalled_with} no_carrier_recall={recalled_without} "
        f"visible_recall={recalled_visible} carrier_delta={carrier_delta} "
        f"visible_delta={visible_delta}"
    )


async def _run_generated_history_round(
    adapter: Any,
    model_id: str,
    policy: str,
    effort: str,
) -> None:
    """Test replay with state generated by the model rather than the probe."""

    first_prompt = (
        "Choose two random five-digit numbers. In your hidden reasoning, record them "
        "exactly as PRIVATE_NUMBERS: 12345, 67890, using your chosen values instead of "
        "the example values. Do not show the chosen numbers in this first visible answer, "
        "but reveal them if the user asks in a later turn. Your visible answer must be "
        "exactly READY."
    )
    first = await _run_turn(
        adapter,
        model_id,
        [{"role": "user", "content": first_prompt}],
        tools=None,
        effort=effort,
    )
    match = re.search(
        r"PRIVATE_NUMBERS\s*[:=]\s*(\d{5})\s*[,;/ ]+\s*(\d{5})",
        first.reasoning,
        flags=re.IGNORECASE,
    )
    if match is None:
        raise RuntimeError(
            "the model did not place PRIVATE_NUMBERS in its reasoning; "
            f"reasoning_len={len(first.reasoning)}"
        )
    expected = match.groups()
    meta_json = (
        json.dumps(first.reasoning_meta, sort_keys=True, separators=(",", ":"))
        if first.reasoning_meta
        else ""
    )
    print(
        f"  generated values: {expected[0]}, {expected[1]} | "
        f"reasoning_len={len(first.reasoning)} | "
        f"reasoning_meta_fields={sorted(first.reasoning_meta or {})} | "
        f"reasoning_meta_bytes={len(meta_json.encode('utf-8'))}"
    )
    ask = "What exact two PRIVATE_NUMBERS did you choose? Reply with only those numbers."

    async def follow_up(
        assistant_msg: ChatMessage,
        label: str,
        *,
        force_readable_with_meta: bool = False,
    ) -> tuple[bool, int | None]:
        session = [ChatMessage.user(first_prompt), assistant_msg, ChatMessage.user(ask)]
        wire_messages = _shape_session(session, policy=policy, agent_model=model_id)
        original_formatter = adapter._format_assistant_message

        def format_assistant_with_both(
            message: dict[str, Any],
            *,
            model_id: str | None = None,
        ) -> dict[str, Any]:
            wire = dict(original_formatter(message, model_id=model_id))
            reasoning = message.get("reasoning")
            if isinstance(reasoning, str) and reasoning:
                wire["reasoning_content"] = reasoning
            reasoning_meta = message.get("reasoning_meta")
            if isinstance(reasoning_meta, dict):
                for key in ("encrypted_content", "reasoning_details"):
                    if key in reasoning_meta:
                        wire[key] = reasoning_meta[key]
            return wire

        if force_readable_with_meta:
            adapter._format_assistant_message = format_assistant_with_both
        try:
            payload = _build_exact_payload(
                adapter,
                wire_messages,
                model_id,
                tools=None,
                effort=effort,
            )
            result = await _run_turn(
                adapter,
                model_id,
                wire_messages,
                tools=None,
                effort=effort,
            )
        finally:
            if force_readable_with_meta:
                adapter._format_assistant_message = original_formatter
        assistant_wire_fields = _assistant_wire_carriers(payload)
        recalled = all(number in result.content for number in expected)
        print(
            f"  {label}: wire_fields={assistant_wire_fields} | "
            f"answer={result.content[:80]!r} | recalled={recalled} | input={result.input_tokens}"
        )
        return recalled, result.input_tokens

    readable = ChatMessage.assistant(
        model=model_id,
        content=first.content,
        reasoning=first.reasoning,
    )
    native_meta = ChatMessage.assistant(
        model=model_id,
        content=first.content,
        reasoning_meta=first.reasoning_meta,
    )
    combined = ChatMessage.assistant(
        model=model_id,
        content=first.content,
        reasoning=first.reasoning,
        reasoning_meta=first.reasoning_meta,
    )
    absent = ChatMessage.assistant(model=model_id, content=first.content)
    visible = ChatMessage.assistant(
        model=model_id,
        content="\n\n".join(part for part in (first.content, first.reasoning) if part),
    )
    await follow_up(readable, "real readable carrier")
    await follow_up(native_meta, "real native meta     ")
    await follow_up(
        combined,
        "readable + native meta",
        force_readable_with_meta=True,
    )
    await follow_up(absent, "no carrier           ")
    await follow_up(visible, "visible control       ")


def _shape_session(
    messages: list[ChatMessage],
    *,
    policy: str,
    agent_model: str,
    live_assistant: ChatMessage | None = None,
) -> list[dict[str, Any]]:
    """Shape a session exactly like the chat loop does.

    Persisted session history runs through ``_assemble_request_history``,
    which strips reasoning under ``current_run``/``none``. The live
    current-run assistant turn is the freshly generated tool-call turn: the
    chat loop carries its continuation form (``_assistant_continuation_dict``,
    which keeps reasoning under ``current_run``/``full_history`` and only
    strips it under ``none``) at the assistant's position — before its tool
    results. The history-shaped assistant entry (stripped) is replaced by the
    continuation form, keeping the request order
    [user, assistant, tool, user].
    """
    wire_messages = _assemble_request_history(
        messages,
        replay_policy=policy,  # type: ignore[arg-type]
        agent_model=agent_model,
    )
    if live_assistant is None:
        return wire_messages
    live = _assistant_continuation_dict(
        live_assistant,
        replay_policy=policy,  # type: ignore[arg-type]
    )
    for index, message in enumerate(wire_messages):
        if message.get("role") == "assistant" and message.get("tool_calls"):
            wire_messages[index] = live
            return wire_messages
    return wire_messages


def _wire_carrier_present(
    wire_messages: list[dict[str, Any]],
) -> bool:
    """Report whether the shaped history carries canonical assistant reasoning.

    The shaped messages are canonical (the adapter converts ``reasoning`` to
    the provider wire field in ``_format_assistant_message``), so the check
    looks for the canonical ``reasoning`` field.
    """
    for message in wire_messages:
        if message.get("role") != "assistant":
            continue
        value = message.get("reasoning")
        reasoning_meta = message.get("reasoning_meta")
        if (isinstance(value, str) and value) or (
            isinstance(reasoning_meta, dict) and reasoning_meta
        ):
            return True
    return False


def _build_tool_calls(tool_calls: list[dict[str, Any]]) -> list[Any]:
    from core.chat.messages import ToolCall

    built: list[Any] = []
    for position, call in enumerate(tool_calls):
        if not isinstance(call, dict):
            continue
        function = call.get("function", {})
        if not isinstance(function, dict):
            function = {}
        name = function.get("name") or call.get("name") or ""
        arguments = function.get("arguments", call.get("arguments"))
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                arguments = {}
        if not isinstance(arguments, dict):
            arguments = {}
        built.append(
            ToolCall(
                id=call.get("id") or f"call_{position}",
                name=name,
                arguments=arguments,
            )
        )
    return built


async def _run_cross_turn_round(
    adapter: Any,
    model_id: str,
    carrier_field: str,
    policy: str,
    effort: str,
    *,
    tools_enabled: bool = False,
) -> None:
    secret = _make_secret()
    print(f"  planted secret: {secret}")

    async def run_shaped(assistant_msg: ChatMessage, label: str) -> tuple[bool, int | None]:
        session = [
            ChatMessage.user(TURN1_PLAIN_PROMPT),
            assistant_msg,
            ChatMessage.user(ASK_PROMPT),
        ]
        wire_messages = _shape_session(session, policy=policy, agent_model=model_id)
        carrier_on_wire = _wire_carrier_present(wire_messages)
        result = await _run_turn(
            adapter,
            model_id,
            wire_messages,
            tools=[TOOL_DEFINITION] if tools_enabled else None,
            effort=effort,
        )
        hit = secret in result.content or secret in result.reasoning
        print(
            f"  {label}: carrier_on_wire={carrier_on_wire} | "
            f"answer={result.content[:45]!r} | "
            f"secret_in_answer={secret in result.content} | "
            f"secret_in_reasoning2={secret in result.reasoning} | "
            f"reasoning2_len={len(result.reasoning)} | input={result.input_tokens}"
        )
        return hit, result.input_tokens

    hit_a, input_a = await run_shaped(
        ChatMessage.assistant(model=model_id, content="", reasoning=_secret_reasoning(secret)),
        "carrier in history",
    )
    hit_b, input_b = await run_shaped(
        ChatMessage.assistant(model=model_id, content=""), "no carrier      "
    )
    hit_c, input_c = await run_shaped(
        ChatMessage.assistant(model=model_id, content=f"I recorded the secret {secret}."),
        "visible control ",
    )
    carrier_delta = input_a - input_b if input_a is not None and input_b is not None else None
    visible_delta = input_c - input_b if input_c is not None and input_b is not None else None
    print(f"  transport token deltas: carrier={carrier_delta} visible_control={visible_delta}")
    print(
        "  behavioral recall only (not transport verdict): "
        f"with={hit_a} without={hit_b} control={hit_c}"
    )


async def _run_tool_loop_round(
    adapter: Any,
    model_id: str,
    carrier_field: str,
    policy: str,
    effort: str,
) -> None:
    turn1 = await _run_turn(
        adapter,
        model_id,
        [{"role": "user", "content": TURN1_TOOL_PROMPT}],
        tools=[TOOL_DEFINITION],
        effort=effort,
    )
    if not turn1.tool_calls:
        print(f"  turn1 invalid: no tool call (reasoning_len={len(turn1.reasoning)})")
        print(f"  reasoning: {turn1.reasoning[:200]!r}")
        return
    meta_json = (
        json.dumps(turn1.reasoning_meta, sort_keys=True, separators=(",", ":"))
        if turn1.reasoning_meta
        else ""
    )
    print(
        f"  turn1: tool_calls={len(turn1.tool_calls)} "
        f"reasoning_len={len(turn1.reasoning)} "
        f"reasoning_meta_fields={sorted(turn1.reasoning_meta or {})} "
        f"reasoning_meta_bytes={len(meta_json.encode('utf-8'))}"
    )

    tool_calls = _build_tool_calls(turn1.tool_calls)
    tool_result = ChatMessage.tool(
        tool_call_id=tool_calls[0].id if tool_calls else "call_1",
        name=tool_calls[0].name if tool_calls else "",
        content='{"temperature": 20, "condition": "sunny"}',
    )

    async def run_persisted(assistant_msg: ChatMessage, label: str) -> int | None:
        """Case A: the assistant turn is persisted and shaped as history.

        ``_assemble_request_history`` strips reasoning under
        ``current_run``/``none``.
        """
        session = [
            ChatMessage.user(TURN1_TOOL_PROMPT),
            assistant_msg,
            tool_result,
            ChatMessage.user(ASK_PROMPT),
        ]
        wire_messages = _shape_session(session, policy=policy, agent_model=model_id)
        return await _run_case(assistant_msg, wire_messages, label)

    async def run_live(live_assistant: ChatMessage, label: str) -> int | None:
        """Case B: the same assistant turn is shaped as the live continuation.

        The chat loop replaces the persisted entry with
        ``_assistant_continuation_dict``, which keeps reasoning under
        ``current_run``/``full_history``.
        """
        session = [
            ChatMessage.user(TURN1_TOOL_PROMPT),
            live_assistant,
            tool_result,
            ChatMessage.user(ASK_PROMPT),
        ]
        wire_messages = _shape_session(
            session,
            policy=policy,
            agent_model=model_id,
            live_assistant=live_assistant,
        )
        return await _run_case(live_assistant, wire_messages, label)

    async def _run_case(
        assistant_msg: ChatMessage,
        wire_messages: list[dict[str, Any]],
        label: str,
    ) -> int | None:
        carrier_on_wire = _wire_carrier_present(wire_messages)
        payload = _build_exact_payload(
            adapter,
            wire_messages,
            model_id,
            tools=[TOOL_DEFINITION],
            effort=effort,
        )
        assistant_wire_fields = _assistant_wire_carriers(payload)
        result = await _run_turn(
            adapter,
            model_id,
            wire_messages,
            tools=[TOOL_DEFINITION],
            effort=effort,
        )
        print(
            f"  {label}: carrier_on_wire={carrier_on_wire} | "
            f"wire_fields={assistant_wire_fields} | "
            f"answer_len={len(result.content)} | "
            f"reasoning2_len={len(result.reasoning)} | input={result.input_tokens}"
        )
        return result.input_tokens

    input_a = await run_persisted(
        ChatMessage.assistant(
            model=model_id,
            content=turn1.content,
            reasoning=turn1.reasoning or None,
            reasoning_meta=turn1.reasoning_meta,
            tool_calls=tool_calls,
        ),
        "A persisted w/ carrier",
    )
    input_a2 = await run_persisted(
        ChatMessage.assistant(model=model_id, content=turn1.content, tool_calls=tool_calls),
        "A persisted no carrier",
    )
    input_b = await run_live(
        ChatMessage.assistant(
            model=model_id,
            content=turn1.content,
            reasoning=turn1.reasoning or None,
            reasoning_meta=turn1.reasoning_meta,
            tool_calls=tool_calls,
        ),
        "B live w/ carrier    ",
    )
    input_b2 = await run_live(
        ChatMessage.assistant(model=model_id, content=turn1.content, tool_calls=tool_calls),
        "B live no carrier    ",
    )
    visible_content = "\n\n".join(part for part in (turn1.content, turn1.reasoning) if part)
    input_c = await run_persisted(
        ChatMessage.assistant(
            model=model_id,
            content=visible_content,
            tool_calls=tool_calls,
        ),
        "C visible control   ",
    )
    persisted_carrier_delta = (
        input_a - input_a2 if input_a is not None and input_a2 is not None else None
    )
    live_carrier_delta = (
        input_b - input_b2 if input_b is not None and input_b2 is not None else None
    )
    visible_delta = input_c - input_a2 if input_c is not None and input_a2 is not None else None
    print(
        "  transport token deltas: "
        f"A_persisted={persisted_carrier_delta} "
        f"B_live={live_carrier_delta} visible_control={visible_delta}"
    )


async def _run_instruction_round(
    adapter: Any,
    model_id: str,
    carrier_field: str,
    policy: str,
    effort: str,
) -> None:
    turn1 = await _run_turn(
        adapter,
        model_id,
        [{"role": "user", "content": INSTRUCTION_PROMPT}],
        tools=None,
        effort=effort,
    )
    print(f"  turn1: content={turn1.content[:40]!r} reasoning_len={len(turn1.reasoning)}")

    async def follow_up(assistant_msg: ChatMessage, label: str) -> bool:
        session = [
            ChatMessage.user(INSTRUCTION_PROMPT),
            assistant_msg,
            ChatMessage.user(INSTRUCTION_TRIGGER),
        ]
        wire_messages = _shape_session(session, policy=policy, agent_model=model_id)
        carrier_on_wire = _wire_carrier_present(wire_messages)
        result = await _run_turn(
            adapter,
            model_id,
            wire_messages,
            tools=None,
            effort=effort,
        )
        rule_hit = INSTRUCTION_RESPONSE in result.content
        print(
            f"  {label}: carrier_on_wire={carrier_on_wire} | "
            f"answer={result.content[:45]!r} | rule_hit={rule_hit} "
            f"| reasoning2_len={len(result.reasoning)}"
        )
        return rule_hit

    rule_a = await follow_up(
        ChatMessage.assistant(model=model_id, content="", reasoning=INSTRUCTION_REASONING),
        "carrier in history",
    )
    rule_b = await follow_up(ChatMessage.assistant(model=model_id, content=""), "no carrier      ")
    rule_c = await follow_up(
        ChatMessage.assistant(
            model=model_id,
            content=f'Rule: when the user writes "{INSTRUCTION_TRIGGER}", '
            f'reply with exactly "{INSTRUCTION_RESPONSE}".',
        ),
        "visible control ",
    )
    print(
        "  behavioral recall only (not transport verdict): "
        f"with={rule_a} without={rule_b} control={rule_c}"
    )


def _build_adapter(provider_id: str, model_id: str, api_key: str) -> Any:
    provider_registry = ProviderRegistry.load(RESOURCES_DIR)
    config = provider_registry.get(provider_id)
    connection = config.connections[0]
    model_registry = ModelRegistry.load(RESOURCES_DIR)
    if provider_id == "ollama-cloud":
        return OllamaCloudAdapter(
            config,
            StaticTokenGetter(api_key),
            auth_config=connection.auth,
            model_lookup=lambda mid: model_registry.get(provider_id, mid.split("::", 1)[0]),
        )
    if provider_id == "opencode-go":
        from core.providers.opencode_go import OpenCodeGoAdapter

        return OpenCodeGoAdapter(
            config,
            StaticTokenGetter(api_key),
            auth_config=connection.auth,
            model_lookup=lambda mid: model_registry.get(provider_id, mid.split("::", 1)[0]),
        )
    raise SystemExit(f"provider {provider_id} not supported by the exact probe yet")


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", default="ollama-cloud")
    parser.add_argument("--model", default="deepseek-v4-flash:0731")
    parser.add_argument(
        "--scenario",
        choices=(
            "tool_loop",
            "cross_turn",
            "cross_turn_tools",
            "instruction",
            "responses_roundtrip",
            "preserved_history",
            "preserved_history_tools",
            "generated_history",
        ),
        default="cross_turn",
    )
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument(
        "--policy", choices=("auto", "none", "current_run", "full_history"), default="auto"
    )
    parser.add_argument(
        "--api-key-env",
        help="Credential name in the data-dir .env (defaults by provider).",
    )
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--effort", default="max", help="reasoning_effort / think value.")
    args = parser.parse_args(argv)

    api_key_env = args.api_key_env or API_KEY_ENV_BY_PROVIDER.get(args.provider)
    if api_key_env is None:
        raise SystemExit(
            f"no default API-key environment variable for provider {args.provider!r}; "
            "pass --api-key-env"
        )
    api_key = _load_api_key(api_key_env, args.data_dir)
    print(f"api key length: {len(api_key)}")
    print(
        f"provider: {args.provider} | model: {args.model} | "
        f"scenario: {args.scenario} | policy: {args.policy}"
    )

    adapter = _build_adapter(args.provider, args.model, api_key)
    policy: str | None = None if args.policy == "auto" else args.policy
    asyncio.run(
        _run_exact_probe(
            adapter,
            args.model,
            args.scenario,
            args.repeats,
            policy,
            args.effort,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
