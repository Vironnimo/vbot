"""Google Antigravity's local CLI as a subscription Model provider."""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator, Iterator, Mapping, Sequence
from contextlib import aclosing
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, override

from core.models.models import Capabilities, Model, ReasoningCapabilities
from core.providers._antigravity_native import AntigravityNative
from core.providers._antigravity_transport import stream_turn
from core.providers._native_agent_wire import (
    JsonObject,
    NativeAgentAdapter,
    system_prompt,
    visible_message,
)
from core.providers.adapter import ModelLookup, is_request_context_message
from core.providers.errors import CatalogEntrySkipped, ProviderError, ProviderReplayLimitError
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
_MAX_REPLAY_STEPS = 8


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


def _header(*, has_tools: bool, has_parts: bool = False) -> str:
    instructions = [
        "Continue this vBot conversation as the assistant. "
        "Read the JSON records in chronological order.",
        "The last message marked user_input is the most recent user input.",
    ]
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

    def add_record(
        self, record: JsonObject, number: int, line: str, size: int, *, dense: bool
    ) -> None:
        if size > self.remaining and not dense:
            self.next_step()
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
                    raise ProviderReplayLimitError(
                        f"Antigravity message {number} has {overhead} UTF-8 bytes of correlation "
                        f"metadata for one part. A replay step holds {_STEP_BYTES} bytes "
                        "including its content."
                    )
                frame[field] = text[offset : offset + length]
                line = _encode(frame)
                self.append(line, len(line.encode("utf-8")))
                offset += length
                if offset == len(text):
                    break
                self.next_step()


def _prompt(
    messages: Sequence[Mapping[str, Any]], *, has_tools: bool
) -> tuple[str, tuple[str, ...]]:
    entries = []
    total = 0
    limit = _STEP_BYTES * _MAX_REPLAY_STEPS
    for record in _records(messages):
        line = _encode(record)
        size = len(line.encode("utf-8"))
        total += size
        if total > limit:
            raise ProviderReplayLimitError(
                f"The Antigravity history already contains {total} UTF-8 bytes before framing. "
                f"The route can carry {_MAX_REPLAY_STEPS} steps of {_STEP_BYTES} bytes. "
                "Compact older history or reduce the size of a single message."
            )
        entries.append((record, line, size))

    # Keep complete records together unless the resulting gaps would exhaust the
    # replay slots. The second pass uses those gaps for correlated fragments.
    for dense in (False, True):
        packed = _ReplaySteps()
        for number, (record, line, size) in enumerate(entries, start=1):
            packed.add_record(record, number, line, size, dense=dense)
            if len(packed.steps) > _MAX_REPLAY_STEPS:
                break
        else:
            header = _header(has_tools=has_tools, has_parts=packed.has_parts)
            steps = tuple("\n".join(lines) for lines in packed.steps)
            if len(steps) == 1 and len(header.encode("utf-8")) <= packed.remaining:
                return "\n".join([header, *packed.steps[0]]), ()
            return header, steps
    raise ProviderReplayLimitError(
        f"The {total}-byte Antigravity history exceeds {_MAX_REPLAY_STEPS} steps of {_STEP_BYTES} "
        "UTF-8 bytes after framing its parts. Compact older history or reduce a single message."
    )


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
    messages: Sequence[Mapping[str, Any]], tools: Sequence[Mapping[str, Any]]
) -> tuple[str, str, tuple[str, ...], list[JsonObject]]:
    prompt, replay_steps = _prompt(messages, has_tools=bool(tools))
    return system_prompt(messages), prompt, replay_steps, _mcp_tools(tools)


class AntigravityAdapter(NativeAgentAdapter):
    """Run one stateless native turn per Model step; vBot owns the history and Tools."""

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
        temporary_files: TemporaryFileManager | None = None,
    ) -> None:
        super().__init__(model_lookup, debug_recorder)
        self._config = config
        self._connection_mode = connection_mode
        self._native = native or AntigravityNative(temporary_files=temporary_files)
        self._owns_native = native is None

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
        system, prompt, replay_steps, tools = await _REQUEST_WORKERS.run(
            _request, messages, [] if choice == "none" else kwargs.get("tools") or []
        )

        def open_turn(executable: Path) -> AsyncGenerator[dict]:
            return stream_turn(
                str(executable),
                model=model_id,
                system_prompt=system,
                prompt=prompt,
                replay_steps=replay_steps,
                tools=tools,
                temporary_files=temporary_files,
            )

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
            # Count before applying replay limits so Chat can project Compaction.
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
