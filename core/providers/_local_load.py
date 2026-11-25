"""Bounded retries for an explicit accelerator allocation failure while loading a Model."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from core.providers._chat_completions_constants import OUTPUT_LIMIT_PARAMETER_NAMES
from core.providers.adapter import (
    ProviderAdapter,
    estimate_wire_request_input_tokens,
    resolve_request_input_budget,
)
from core.providers.errors import ProviderError
from core.providers.providers import RequestInput
from core.providers.wire_profiles import WireBinding
from core.utils.tokens import estimate_structured_tokens


def load_oom(error: ProviderError, *, load_endpoint: bool) -> bool:
    if error.status_code not in (400, 422, 500, 503):
        return False
    detail = str(error).lower()
    memory = re.search(
        r"out of (?:device |gpu |video )?memory|insufficient (?:gpu |video )?memory|"
        r"not enough (?:gpu |video )?memory|failed to allocate|unable to allocate",
        detail,
    )
    accelerator = re.search(
        r"\b(?:cuda|cudamalloc|vram|gpu|metal|vulkan|hip|device memory)\b", detail
    )
    loading = re.search(
        r"(?:load(?:ing)? (?:the )?model|model load|initializ(?:e|ing) (?:the )?(?:model|runner)|"
        r"(?:allocate|allocating|initialize) (?:the )?kv cache)",
        detail,
    )
    return bool(memory and accelerator and (load_endpoint or loading))


def request_floor(
    adapter: ProviderAdapter,
    model_id: str,
    messages: list[dict[str, Any]],
    kwargs: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> int | None:
    """Reserve the input and the output the configured window would actually serve."""
    wire_messages = payload.get("messages")
    if not isinstance(wire_messages, list):
        return None
    # A text estimate cannot predict the server's preprocessing of images,
    # audio, files or opaque reasoning history. Check native and canonical shapes.
    for message in (*messages, *wire_messages):
        if not isinstance(message, Mapping) or any(
            message.get(key) for key in ("images", "audio", "attachments", "reasoning_meta")
        ):
            return None
        for key in ("content", "content_blocks", "tool_result_content"):
            parts = message.get(key)
            if parts is None or isinstance(parts, str):
                continue
            if not isinstance(parts, list) or any(
                not isinstance(part, Mapping)
                or part.get("type") not in ("text", "input_text", "output_text")
                or not isinstance(part.get("text"), str)
                for part in parts
            ):
                return None

    native_options = payload.get("options")
    if isinstance(native_options, Mapping):
        limits = [native_options["num_predict"]] if "num_predict" in native_options else []
    else:
        limits = [payload[key] for key in OUTPUT_LIMIT_PARAMETER_NAMES if key in payload]
    if limits:
        output = 0
        for limit in limits:
            if type(limit) is not int or limit <= 0:
                return None
            output = max(output, limit)
    else:
        ceiling = adapter._model_max_output_tokens(model_id)
        if ceiling is None:
            return None
        output = ceiling
    thinking = adapter.minimum_output_tokens(
        model_id, kwargs.get("thinking_effort"), explicit_limit=output
    )

    if any(payload.get(key) for key in ("input", "images", "audio", "template")) or any(
        key in payload and not isinstance(payload[key], str) for key in ("system", "instructions")
    ):
        return None
    input_fields = ("messages", "tools", "response_format", "format", "instructions", "system")
    rendered = {
        key: estimate_structured_tokens(payload[key], model_id=model_id)[0]
        for key in input_fields
        if key in payload
    }
    budget = resolve_request_input_budget(
        model_id,
        lambda: estimate_wire_request_input_tokens(
            adapter, messages, model_id=model_id, tools=kwargs.get("tools")
        ),
    )
    # Chat's scoped projection covers canonical messages and Tools. A Wire
    # body override or output schema can add input on top of that measurement.
    rules = adapter.wire_profile(model_id).request
    additions = sum(
        cost
        for field, cost in rendered.items()
        if field not in ("messages", "tools")
        or field in rules.extra_body
        or (field == "tools" and not kwargs.get("tools") and field in rules.body_defaults)
    )
    input_bound = budget.upper_bound
    if additions:
        input_bound += RequestInput.estimate(additions).upper_bound
    if additions or budget.estimated and budget.estimated_tokens == budget.tokens:
        input_bound = max(input_bound, RequestInput.estimate(sum(rendered.values())).upper_bound)
    return max(2048, input_bound + max(output, thinking))


def learned_window(wire: WireBinding, model_id: str, configured: int | None) -> int | None:
    return wire.route_fact(model_id, "load_window", setting=configured) if configured else None
