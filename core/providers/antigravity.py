"""Google Antigravity's local CLI as a subscription Model provider."""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator, Mapping, Sequence
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
from core.providers.adapter import ModelLookup
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
_TRANSCRIPT_HEADER = (
    "Continue this vBot conversation as the assistant. "
    "Each JSON line is one message in chronological order."
)


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


def _prompt(messages: Sequence[Mapping[str, Any]], *, has_tools: bool) -> str:
    conversation = [message for message in messages if message.get("role") != "system"]
    records = []
    for message in conversation:
        record = visible_message(message)
        text = _text(record["content"])
        if media := record.pop("media", None):
            text = "\n\n".join(filter(None, (text, _text(media))))
        record["content"] = text
        # Preserve a literal @ in JSON without invoking the CLI's @file preprocessor.
        encoded = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        records.append(encoded.replace("@", r"\u0040"))
    tool_scope = (
        "Use the vbot MCP server for tool calls."
        if has_tools
        else "Answer directly without calling tools."
    )
    return "\n".join([f"{_TRANSCRIPT_HEADER} {tool_scope}", *records])


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
) -> tuple[str, str, list[JsonObject]]:
    return system_prompt(messages), _prompt(messages, has_tools=bool(tools)), _mcp_tools(tools)


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
        system, prompt, tools = await _REQUEST_WORKERS.run(
            _request, messages, [] if choice == "none" else kwargs.get("tools") or []
        )

        def open_turn(executable: Path) -> AsyncGenerator[dict]:
            return stream_turn(
                str(executable),
                model=model_id,
                system_prompt=system,
                prompt=prompt,
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

        return estimate_structured_tokens(
            [system_prompt(messages), _prompt(messages, has_tools=bool(tools)), tools or []],
            model_id=model_id,
        )[0]

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
