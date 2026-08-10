"""Immediate local and optional Model-generated Session titles."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from core.chat.content_blocks import (
    ContentBlock,
    FileBlock,
    FileMentionBlock,
    MediaBlock,
    TextBlock,
)
from core.chat.model_resolution import (
    _first_usable_connection_id,
    _model_connection_allowlist,
    parse_model_with_connection,
)
from core.debug import DebugContext
from core.sessions.sessions import (
    SESSION_AUTO_TITLE_INITIALIZED_KEY,
    SESSION_TITLE_KEY,
)
from core.utils.logging import get_logger
from core.utils.workers import BoundedWorkerPool

if TYPE_CHECKING:
    from core.runtime.interfaces import RuntimeServices

_LOGGER = get_logger("sessions.titles")

SESSION_TITLE_WORKER_LIMIT = 2
_SESSION_TITLE_WORKERS = BoundedWorkerPool(
    name="session-title",
    max_workers=SESSION_TITLE_WORKER_LIMIT,
)

LOCAL_TITLE_MAX_CHARACTERS = 40
GENERATED_TITLE_MAX_CHARACTERS = 60
TITLE_INPUT_HEAD_BYTES = 3 * 1024
TITLE_INPUT_TAIL_BYTES = 2 * 1024
TITLE_ATTACHMENT_METADATA_MAX_BYTES = 1024
TITLE_OMISSION_MARKER = "\n\n[large middle section omitted]\n\n"
_SUBAGENT_SESSION_METADATA_FLAG = "is_subagent_session"
_HIDDEN_REASONING_BLOCK_PATTERN = re.compile(
    r"<(think|thinking|analysis|reasoning)>[\s\S]*?(?:</\1>|$)\s*",
    re.IGNORECASE,
)
_META_TITLE_PATTERNS = (
    re.compile(
        r"^(?:the\s+)?user\s+(?:is\s+)?(?:asking|asks|wants|requested|requests|needs)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"^(?:this|the)\s+(?:chat|session|conversation|request)\s+"
        r"(?:is|asks|concerns|involves|focuses)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"^(?:der|die)\s+(?:user|nutzer(?:in)?)\s+"
        r"(?:fragt|bittet|möchte|will|fordert)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"^was\s+(?:macht|möchte|will)\s+(?:der|die)\s+(?:user|nutzer(?:in)?)\b",
        re.IGNORECASE,
    ),
    re.compile(r"^(?:in\s+)?dieser\s+(?:session|unterhaltung)\b", re.IGNORECASE),
)

TITLE_SYSTEM_PROMPT = (
    "Your sole job is to create a title for a chat Session based on its first user message. "
    "The soft cap is 40 characters; exceed it only when clarity requires it. The absolute "
    "maximum is 60 characters. Your entire response must be only the title in plain text on "
    "a single line, with no quotes, no leading 'Title:', and no Markdown. Good title: Login "
    "failure investigation. Bad title: The user is asking me to investigate login failures."
)


@dataclass(frozen=True)
class _TitleGenerationRequest:
    model: str
    title_input: str


class SessionTitleService:
    """Set a local title immediately and optionally improve it in the background."""

    def __init__(self, runtime: RuntimeServices) -> None:
        self._runtime = runtime
        self._background_tasks: set[asyncio.Task[None]] = set()
        self._closed = False

    def notify_user_message(
        self,
        *,
        agent_id: str,
        session_id: str,
        project_id: str | None,
        agent: Any,
        content: str | list[ContentBlock],
        run_id: str,
    ) -> None:
        """Handle the first visible user message without delaying its Run."""
        if self._closed:
            return
        task = asyncio.create_task(
            self._initialize_title_async(
                agent_id=agent_id,
                session_id=session_id,
                project_id=project_id,
                agent=agent,
                content=content,
                run_id=run_id,
            )
        )
        self._background_tasks.add(task)
        task.add_done_callback(self._on_background_task_done)

    async def _initialize_title_async(
        self,
        *,
        agent_id: str,
        session_id: str,
        project_id: str | None,
        agent: Any,
        content: str | list[ContentBlock],
        run_id: str,
    ) -> None:
        try:
            generation = await _SESSION_TITLE_WORKERS.run(
                self._prepare_title,
                agent_id=agent_id,
                session_id=session_id,
                project_id=project_id,
                agent=agent,
                content=content,
            )
            if generation is not None:
                await self._generate_title(
                    agent_id=agent_id,
                    session_id=session_id,
                    project_id=project_id,
                    model=generation.model,
                    title_input=generation.title_input,
                    run_id=run_id,
                )
        except Exception:
            _LOGGER.warning(
                "Session title initialization failed (agent=%s session=%s)",
                agent_id,
                session_id,
                exc_info=True,
            )

    def _prepare_title(
        self,
        *,
        agent_id: str,
        session_id: str,
        project_id: str | None,
        agent: Any,
        content: str | list[ContentBlock],
    ) -> _TitleGenerationRequest | None:
        sessions = self._runtime.chat_sessions
        metadata = sessions.get_metadata(agent_id, session_id, project_id)
        if metadata.get(SESSION_AUTO_TITLE_INITIALIZED_KEY) is True:
            return None
        if metadata.get(_SUBAGENT_SESSION_METADATA_FLAG) is True:
            return None
        has_manual_title = isinstance(metadata.get(SESSION_TITLE_KEY), str) and bool(
            metadata[SESSION_TITLE_KEY].strip()
        )

        session = sessions.get(agent_id, session_id, project_id)
        user_message_count = 0
        for message in session.load():
            if message.role == "user":
                user_message_count += 1
                if user_message_count > 1:
                    break
        if user_message_count != 1:
            sessions.mark_auto_title_initialized(agent_id, session_id, project_id)
            return None

        text, attachment_lines = _title_source_parts(content)
        local_title = _local_title(text, attachment_lines)
        sessions.set_auto_title(agent_id, session_id, local_title, project_id)

        if has_manual_title:
            return None

        settings = self._runtime.storage.load_session_title_settings()
        if not settings["enabled"]:
            return None
        title_input = _title_input(text, attachment_lines)
        if not title_input:
            return None

        configured_model = settings["model"]
        model = configured_model or str(agent.model)
        return _TitleGenerationRequest(model=model, title_input=title_input)

    def _on_background_task_done(self, task: asyncio.Task[None]) -> None:
        self._background_tasks.discard(task)
        if task.cancelled():
            return
        exception = task.exception()
        if exception is not None:
            _LOGGER.warning("Background Session title task failed", exc_info=exception)

    async def aclose(self) -> None:
        """Cancel and drain every in-flight generated-title request."""
        self._closed = True
        tasks = tuple(self._background_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._background_tasks.clear()

    async def _generate_title(
        self,
        *,
        agent_id: str,
        session_id: str,
        project_id: str | None,
        model: str,
        title_input: str,
        run_id: str,
    ) -> None:
        adapter: Any | None = None
        try:
            provider_id, model_id, connection_id = _resolve_model_target(self._runtime, model)
            adapter = self._runtime.get_adapter(provider_id, connection_id)
            if hasattr(adapter, "set_debug_context"):
                adapter.set_debug_context(
                    DebugContext(
                        run_id=f"title-{run_id}",
                        agent_id=agent_id,
                        session_id=session_id,
                        provider_id=provider_id,
                        connection_id=connection_id,
                        model_id=model_id,
                        streaming=False,
                        iteration_number=0,
                    )
                )
            response = await adapter.send(
                [
                    {"role": "system", "content": TITLE_SYSTEM_PROMPT},
                    {"role": "user", "content": title_input},
                ],
                model_id=model_id,
                temperature=0.0,
                thinking_effort="none",
            )
            title = await _SESSION_TITLE_WORKERS.run(
                _normalize_generated_title,
                adapter,
                response,
                model_id,
            )
            await _SESSION_TITLE_WORKERS.run(
                self._runtime.chat_sessions.set_auto_title,
                agent_id,
                session_id,
                title,
                project_id,
            )
            _LOGGER.info(
                "Automatic Session title generated (agent=%s session=%s model=%s)",
                agent_id,
                session_id,
                model,
            )
        except Exception:
            # The immediate local title is the final fallback. An explicitly
            # configured Title Model never cascades into another billable call.
            _LOGGER.warning(
                "Automatic Session title generation failed (agent=%s session=%s model=%s)",
                agent_id,
                session_id,
                model,
                exc_info=True,
            )
        finally:
            if adapter is not None:
                try:
                    await adapter.aclose()
                except Exception:
                    _LOGGER.warning("Failed to close Session title adapter", exc_info=True)


def _normalize_generated_title(
    adapter: Any,
    response: dict[str, Any],
    model_id: str,
) -> str:
    normalized = adapter.normalize_response(response, model_id=model_id)
    return _generated_title(normalized)


def _resolve_model_target(runtime: RuntimeServices, model: str) -> tuple[str, str, str]:
    provider_id, model_id, connection_suffix = parse_model_with_connection(model)
    if connection_suffix:
        return provider_id, model_id, f"{provider_id}:{connection_suffix}"
    connection_id = _first_usable_connection_id(
        runtime,
        provider_id,
        _model_connection_allowlist(runtime, provider_id, model_id),
    )
    return provider_id, model_id, connection_id


def _title_source_parts(content: str | list[ContentBlock]) -> tuple[str, list[str]]:
    if isinstance(content, str):
        return content, []

    text_parts: list[str] = []
    attachments: list[str] = []
    for block in content:
        if isinstance(block, TextBlock):
            text_parts.append(block.text)
        elif isinstance(block, (MediaBlock, FileBlock)):
            attachments.append(f"- {block.filename} ({block.media_type})")
        elif isinstance(block, FileMentionBlock):
            filename = _mentioned_filename(block.path)
            size = f", {block.size_bytes} bytes" if block.size_bytes is not None else ""
            attachments.append(f"- {filename} (mentioned file{size})")
    return "\n".join(text_parts), attachments


def _local_title(text: str, attachment_lines: list[str]) -> str:
    collapsed = " ".join(text.split())
    if not collapsed and attachment_lines:
        collapsed = attachment_lines[0].removeprefix("- ")
    if len(collapsed) <= LOCAL_TITLE_MAX_CHARACTERS:
        return collapsed
    return collapsed[: LOCAL_TITLE_MAX_CHARACTERS - 1].rstrip() + "…"


def _title_input(text: str, attachment_lines: list[str]) -> str:
    projected_text = _bounded_text_projection(text)
    bounded_attachments = _utf8_prefix(
        "\n".join(attachment_lines), TITLE_ATTACHMENT_METADATA_MAX_BYTES
    )
    sections: list[str] = []
    if projected_text.strip():
        sections.append(f"First user message:\n{projected_text}")
    if bounded_attachments.strip():
        sections.append(f"Attachments:\n{bounded_attachments}")
    return "\n\n".join(sections)


def _bounded_text_projection(text: str) -> str:
    encoded = text.encode("utf-8")
    total_limit = TITLE_INPUT_HEAD_BYTES + TITLE_INPUT_TAIL_BYTES
    if len(encoded) <= total_limit:
        return text
    head = encoded[:TITLE_INPUT_HEAD_BYTES].decode("utf-8", errors="ignore")
    tail = encoded[-TITLE_INPUT_TAIL_BYTES:].decode("utf-8", errors="ignore")
    return f"{head}{TITLE_OMISSION_MARKER}{tail}"


def _utf8_prefix(value: str, max_bytes: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def _generated_title(response: dict[str, Any]) -> str:
    content = response.get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        chunks: list[str] = []
        for item in content:
            if isinstance(item, str):
                chunks.append(item)
                continue
            if not isinstance(item, dict):
                continue
            block_type = item.get("type")
            if block_type not in (None, "text", "output_text"):
                continue
            block_text = item.get("text")
            if isinstance(block_text, str):
                chunks.append(block_text)
        text = "\n".join(chunk for chunk in chunks if isinstance(chunk, str))
    else:
        raise ValueError("Session title response did not include text content")

    text = _HIDDEN_REASONING_BLOCK_PATTERN.sub("", text)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) != 1:
        raise ValueError("Session title response was not exactly one text line")
    line = lines[0]
    line = re.sub(r"^(?:title|titel)\s*:\s*", "", line, flags=re.IGNORECASE)
    line = line.strip(" \t\"'`*_#")
    line = " ".join(line.split()).rstrip(".!?:;").strip(" \t\"'`*_#")
    if not line:
        raise ValueError("Session title response was empty")
    if any(pattern.search(line) for pattern in _META_TITLE_PATTERNS):
        raise ValueError("Session title response described the naming task instead of the topic")
    if len(line) > GENERATED_TITLE_MAX_CHARACTERS:
        raise ValueError(
            f"Session title response exceeded {GENERATED_TITLE_MAX_CHARACTERS} characters"
        )
    return line


def _mentioned_filename(path: str) -> str:
    normalized = path.replace("\\", "/").rstrip("/")
    return normalized.rsplit("/", 1)[-1] or path
