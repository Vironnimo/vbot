"""Google Antigravity's local CLI as a subscription Model provider."""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncGenerator, Iterator, Mapping, Sequence
from contextlib import AsyncExitStack, aclosing
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Literal, override

from core.models.models import Capabilities, Model, ReasoningCapabilities
from core.providers._antigravity_native import AntigravityNative
from core.providers._antigravity_transport import stream_turn
from core.providers._native_agent_wire import (
    Conversation,
    JsonObject,
    NativeAgentAdapter,
    system_prompt,
    visible_message,
)
from core.providers.adapter import ModelLookup, is_request_context_message
from core.providers.errors import CatalogEntrySkipped, ProviderError
from core.providers.providers import AuthConfig, ProviderConfig
from core.providers.tool_schema import render_tool_definitions
from core.providers.wire_profile import Protocol
from core.utils.workers import BoundedWorkerPool

if TYPE_CHECKING:
    from core.debug import ProviderDebugRecorder
    from core.providers.http import ProviderHTTPTransportScope
    from core.providers.token_getter import TokenGetter
    from core.storage import TemporaryFileManager


_REQUEST_WORKERS = BoundedWorkerPool(name="antigravity-request", max_workers=2)
_STEP_BYTES = 48_000
_STEPS_PER_BATCH = 8


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list) and all(
        isinstance(block, Mapping)
        and block.get("type") == "text"
        and isinstance(block.get("text"), str)
        for block in content
    ):
        return "\n\n".join(block["text"] for block in content)
    raise ProviderError(
        "The Antigravity subscription connection accepts text only.", retryable=False
    )


def _records(messages: Sequence[Mapping[str, Any]]) -> Iterator[JsonObject]:
    for message in messages:
        if message.get("role") == "system":
            continue
        record = visible_message(message)
        text = _text(record["content"])
        if media := record.pop("media", None):
            text = "\n\n".join(filter(None, (text, _text(media))))
        record["content"] = text
        if record["role"] == "user" and not is_request_context_message(message):
            record["user_input"] = True
        yield record


def _encode(value: Any) -> str:
    # Preserve a literal @ in JSON without invoking the CLI's @file preprocessor.
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).replace("@", r"\u0040")


def _header(*, has_tools: bool, has_parts: bool = False, tool_results: bool = False) -> str:
    instructions = [
        "Continue this vBot conversation as the assistant. "
        "Read the JSON records in chronological order.",
        "The last message marked user_input is the most recent user input.",
    ]
    if tool_results:
        instructions.append(
            "The tool records give vBot's execution results for the preceding calls."
        )
    if has_parts:
        instructions.append(
            "Numbered parts of a message continue in order. "
            "Join arguments_json parts for the same Tool call to read its arguments."
        )
    instructions.append(
        "Use the vbot MCP server for tool calls."
        if has_tools
        else "Answer directly without calling tools."
    )
    return " ".join(instructions)


def _fields(record: JsonObject, number: int) -> Iterator[tuple[JsonObject, str, str]]:
    """Keep each field attributable to its original message and Tool call across steps."""
    metadata = {key: value for key, value in record.items() if key not in ("content", "tool_calls")}
    metadata["message"] = number
    calls = record.get("tool_calls") or []
    if record["content"] or not calls:
        yield metadata, "content", record["content"]
    for index, call in enumerate(calls, start=1):
        source = {**metadata, "call": index, "tool_call_id": call["id"], "name": call["name"]}
        arguments = json.dumps(call["arguments"], ensure_ascii=False, separators=(",", ":"))
        yield source, "arguments_json", arguments


def _prefix_that_fits(text: str, offset: int, budget: int) -> int:
    """Largest code-point prefix fitting the available bytes after JSON escaping."""
    low = 0
    high = max(0, min(len(text) - offset, budget))
    candidate = high
    while low < high:
        if len(_encode(text[offset : offset + candidate]).encode("utf-8")) - 2 <= budget:
            low = candidate
        else:
            high = candidate - 1
        candidate = (low + high + 1) // 2
    return low


class _ReplaySteps:
    """Pack entire messages first; split a field when a message cannot fit a native step."""

    def __init__(self) -> None:
        self.steps: list[list[str]] = [[]]
        self.remaining = _STEP_BYTES
        self.has_parts = False

    def next_step(self) -> None:
        if self.steps[-1]:
            self.steps.append([])
            self.remaining = _STEP_BYTES

    def append(self, line: str, size: int) -> None:
        self.steps[-1].append(line)
        self.remaining -= size + 1  # Leave room for the next record's newline.

    def add_record(self, record: JsonObject, number: int, line: str, size: int) -> None:
        if size <= self.remaining:
            self.append(line, size)
            return
        self.has_parts = True
        part = 0
        for source, field, text in _fields(record, number):
            offset = 0
            while True:
                part += 1
                frame = {**source, "part": part, field: ""}
                overhead = len(_encode(frame).encode("utf-8"))
                length = _prefix_that_fits(text, offset, self.remaining - overhead)
                if (text and not length) or overhead > self.remaining:
                    self.next_step()
                    length = _prefix_that_fits(text, offset, self.remaining - overhead)
                if (text and not length) or overhead > self.remaining:
                    raise ProviderError(
                        f"Antigravity message {number} has {overhead} UTF-8 bytes of correlation "
                        f"metadata for one part. A replay step holds {_STEP_BYTES} bytes "
                        "including its content.",
                        retryable=False,
                    )
                frame[field] = text[offset : offset + length]
                line = _encode(frame)
                self.append(line, len(line.encode("utf-8")))
                offset += length
                if offset == len(text):
                    break
                self.next_step()


def _prompt(
    records: Sequence[JsonObject], *, has_tools: bool, tool_results: bool = False
) -> tuple[str, tuple[str, ...], tuple[tuple[str, ...], ...]]:
    packed = _ReplaySteps()
    for number, record in enumerate(records, start=1):
        line = _encode(record)
        packed.add_record(record, number, line, len(line.encode("utf-8")))
    header = _header(has_tools=has_tools, has_parts=packed.has_parts, tool_results=tool_results)
    steps = tuple("\n".join(lines) for lines in packed.steps)
    if len(steps) == 1 and len(header.encode("utf-8")) <= packed.remaining:
        return "\n".join([header, *packed.steps[0]]), (), ()
    batches = tuple(steps[i : i + _STEPS_PER_BATCH] for i in range(0, len(steps), _STEPS_PER_BATCH))
    return header, batches[-1], batches[:-1]


def _digest(value: Any) -> bytes:
    encoded = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).digest()


def _mcp_tools(tools: Sequence[Mapping[str, Any]]) -> list[JsonObject]:
    return [
        {
            "name": tool["name"],
            "description": tool["description"],
            "inputSchema": tool["parameters"],
        }
        for tool in render_tool_definitions(tools, profile="omit_strict")
    ]


def _request(
    messages: Sequence[Mapping[str, Any]], tools: Sequence[Mapping[str, Any]], model: str
) -> tuple[str, list[JsonObject], list[JsonObject], bytes, tuple[bytes, ...]]:
    system = system_prompt(messages)
    inventory = _mcp_tools(tools)
    records = list(_records(messages))
    return (
        system,
        inventory,
        records,
        _digest((model, system, inventory)),
        tuple(map(_digest, records)),
    )


class AntigravityAdapter(NativeAgentAdapter):
    """Continue verified native conversations while vBot owns canonical history and Tools."""

    WIRE_PROTOCOLS: ClassVar[tuple[Protocol, ...]] = ("antigravity",)

    def __init__(
        self,
        config: ProviderConfig,
        token_getter: TokenGetter | str | None = None,
        base_url: str | None = None,
        auth_config: AuthConfig | None = None,
        model_lookup: ModelLookup | None = None,
        debug_recorder: ProviderDebugRecorder | None = None,
        *,
        connection_mode: str | None = None,
        http_transport: ProviderHTTPTransportScope | None = None,
        native: AntigravityNative | None = None,
        connection_id: str | None = None,
        temporary_files: TemporaryFileManager | None = None,
    ) -> None:
        super().__init__(model_lookup, debug_recorder)
        self._config = config
        self._connection_id = connection_id or config.connections[0].id
        self._connection_mode = connection_mode
        self._native = native or AntigravityNative(temporary_files=temporary_files)
        self._owns_native = native is None

    @override
    def request_context_kwargs(
        self,
        *,
        agent_id: str,
        session_id: str,
        project_id: str | None = None,
        prompt_cache_affinity_id: str | None = None,
        session_generation_id: str | None = None,
        request_kind: Literal["chat", "compaction", "utility"] = "chat",
    ) -> JsonObject:
        if request_kind != "chat":
            return {}
        return {
            "_antigravity_conversation": Conversation(
                project_id, agent_id, session_id, session_generation_id, self._connection_id
            )
        }

    @override
    async def stream(
        self, messages: list[dict], *, model_id: str, **kwargs: Any
    ) -> AsyncGenerator[dict]:
        self._refuse_unadmitted_model(model_id)
        choice = kwargs.get("tool_choice")
        if choice not in (None, "auto", "none"):
            raise ProviderError(
                "Antigravity supports automatic or disabled Tools.", retryable=False
            )
        temporary_files = self._native.temporary_files
        if temporary_files is None:
            raise ProviderError("Antigravity requires managed temporary storage.", retryable=False)
        key = kwargs.get("_antigravity_conversation")
        if key is not None and not isinstance(key, Conversation):
            raise ProviderError("Invalid Antigravity conversation context.", retryable=False)
        system, tools, records, signature, history = await _REQUEST_WORKERS.run(
            _request, messages, [] if choice == "none" else kwargs.get("tools") or [], model_id
        )

        async def open_turn(executable: Path) -> AsyncGenerator[dict]:
            async with AsyncExitStack() as stack:
                profile = None
                if key is not None:
                    profile = await stack.enter_async_context(
                        self._native.conversations.checkout(key, signature, history)
                    )
                start = len(profile.history) if profile else 0
                # Repeat the call IDs with the real results. The native CLI recorded
                # denials because only vBot is allowed to execute those calls.
                include_calls = start > 0 and bool(records[start - 1].get("tool_calls"))
                prompt, replay_steps, seed_batches = await _REQUEST_WORKERS.run(
                    _prompt,
                    records[start - int(include_calls) :],
                    has_tools=bool(tools),
                    tool_results=include_calls,
                )
                stream = await stack.enter_async_context(
                    aclosing(
                        stream_turn(
                            str(executable),
                            model=model_id,
                            system_prompt=system,
                            prompt=prompt,
                            replay_steps=replay_steps,
                            seed_batches=seed_batches,
                            tools=tools,
                            temporary_files=temporary_files,
                            conversation=profile.native if profile else None,
                        )
                    )
                )
                content = []
                calls = []
                finish = None
                async for delta in stream:
                    if delta["type"] == "finish":
                        finish = delta
                        continue
                    if profile:
                        if delta["type"] == "content_delta":
                            content.append(delta["text"])
                        elif delta["type"] == "tool_call_delta":
                            calls.append(
                                {
                                    "id": delta["id"],
                                    "name": delta["name_delta"],
                                    "arguments": json.loads(delta["arguments_delta"]),
                                }
                            )
                    yield delta
                if finish is None:
                    raise ProviderError(
                        "Antigravity CLI returned an incomplete turn.", retryable=False
                    )
                if profile:
                    assistant = {
                        "role": "assistant",
                        "content": "".join(content),
                        "tool_calls": calls,
                    }
                    profile.history = (*history, await _REQUEST_WORKERS.run(_digest, assistant))
            yield finish

        async with aclosing(self._native.stream(open_turn)) as stream:
            async for delta in stream:
                yield delta

    @override
    async def aclose(self) -> None:
        if self._owns_native:
            await self._native.aclose()

    @override
    def estimate_request_input_tokens(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        model_id: str,
        tools: Sequence[Mapping[str, Any]] | None = None,
    ) -> int:
        from core.utils.tokens import estimate_structured_tokens

        header = _header(has_tools=bool(tools))
        total = estimate_structured_tokens(
            [system_prompt(messages), header, tools or []], model_id=model_id
        )[0]
        for record in _records(messages):
            # Account for all canonical input even when the native conversation is warm.
            # Cache stable records independently and bound individual tokenizer inputs.
            line = _encode(record)
            for offset in range(0, len(line), 8192):
                total += estimate_structured_tokens(
                    line[offset : offset + 8192], model_id=model_id
                )[0]
        return total

    @classmethod
    def normalize_catalog_entry(
        cls, raw: Mapping[str, Any], defaults: Mapping[str, Any] | None = None
    ) -> Model:
        model_id = raw.get("id")
        if not isinstance(model_id, str) or not model_id.strip():
            raise CatalogEntrySkipped("Antigravity CLI model has no id")
        if model_id.startswith("gemini-pro-orchestrator-"):
            raise CatalogEntrySkipped("Antigravity Orchestrator runs its own multi-model workflow")
        return Model(
            model_id=model_id,
            name=str(raw.get("label") or model_id),
            capabilities=Capabilities(
                vision=False,
                tools=True,
                json_mode=False,
                reasoning=ReasoningCapabilities(supported=None),
            ),
            context_window=None,
            max_output_tokens=None,
        )
