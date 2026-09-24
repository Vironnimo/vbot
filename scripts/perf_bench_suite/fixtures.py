"""Deterministic synthetic fixtures shared by the Python benchmark groups.

Histories model realistic Agent Sessions: each Run starts with a user message,
takes two to five Model steps that call Tools (``bash``, ``read``,
``search_files``) and ends with a Markdown answer and its Run summary. Message
sizes follow per-role weights with jitter and are scaled so the whole history
reaches its character budget (four characters per token). The last Run is left
running, as it is while Chat builds the next request.

The OpenAI-compatible Adapter is real; only its HTTP client is swapped for an
``httpx.MockTransport`` so streams are fed from memory and nothing leaves the
process.
"""

from __future__ import annotations

import json
import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from core.chat.messages import ChatMessage, ToolCall
from core.database import write_bootstrap_marker
from core.models.models import Capabilities, Model, ReasoningCapabilities
from core.providers.openai_compatible import OpenAICompatibleAdapter
from core.providers.providers import AuthConfig, ConnectionConfig, ProviderConfig
from core.sessions import ChatSession, ChatSessionManager
from core.tools import tool_success
from scripts.perf_bench_suite.runner import BenchContext

CHARS_PER_TOKEN = 4
AGENT_ID = "perf-agent"
PROVIDER_ID = "perf-bench"
MODEL_ID = "perf-model"
AGENT_MODEL = f"{PROVIDER_ID}/{MODEL_ID}"
BASE_URL = "https://perf-bench.invalid/v1"
CONTEXT_WINDOW = 262_144
MAX_OUTPUT_TOKENS = 32_768
TOOL_DEFINITION_COUNT = 28
SYSTEM_PROMPT_CHARS = 12_000
_HISTORY_START = datetime(2026, 1, 5, 9, 0, tzinfo=UTC)

_WORD_TEXT = (
    "agent session request stream provider model tool result history token budget "
    "project workspace file path config value render markdown chunk buffer queue "
    "latency sample timing event loop worker thread writer reader index cache summary "
    "message answer context window output input delta frame payload the a of to and "
    "in for with on is are was be this that it from by as at update check build parse "
    "encode decode apply merge filter select return value list"
)
_WORDS = tuple(_WORD_TEXT.split())
_CODE_LINES = (
    "def handle(request, *, timeout=30):",
    "    payload = json.loads(request.body)",
    "    for item in payload.get('items', []):",
    "        result.append(transform(item))",
    "    return {'ok': True, 'count': len(result)}",
    "class SessionIndex:",
    "    def __init__(self, path: Path) -> None:",
    "        self._entries: dict[str, int] = {}",
    "if __name__ == '__main__':",
    "    raise SystemExit(main())",
    "const rows = items.filter((row) => row.visible);",
    "export function renderRow(row) { return `<li>${row.label}</li>`; }",
)


@dataclass(frozen=True)
class HistoryShape:
    """A synthetic history size: message count and approximate token budget."""

    label: str
    messages: int
    tokens: int

    @property
    def chars(self) -> int:
        return self.tokens * CHARS_PER_TOKEN


HISTORY_SHAPES = (
    HistoryShape("100msg", 100, 50_000),
    HistoryShape("1000msg", 1000, 200_000),
)


class TextFactory:
    """Deterministic filler text, Markdown and code of a requested length."""

    def __init__(self, seed: int) -> None:
        self._rng = random.Random(seed)

    def words(self, chars: int) -> str:
        parts: list[str] = []
        size = 0
        while size < chars:
            word = self._rng.choice(_WORDS)
            parts.append(word)
            size += len(word) + 1
        return " ".join(parts)[: max(chars, 1)]

    def code(self, chars: int) -> str:
        lines: list[str] = []
        size = 0
        while size < chars:
            line = self._rng.choice(_CODE_LINES)
            lines.append(line)
            size += len(line) + 1
        return "\n".join(lines)[: max(chars, 1)]

    def markdown(self, chars: int) -> str:
        """Headings, paragraphs, bullet lists, inline code and fenced code blocks."""
        blocks: list[str] = []
        size = 0
        while size < chars:
            kind = self._rng.random()
            if kind < 0.12:
                block = f"## {self.words(self._rng.randint(12, 40)).capitalize()}"
            elif kind < 0.3:
                block = "\n".join(
                    f"- `{self._rng.choice(_WORDS)}` {self.words(self._rng.randint(20, 70))}"
                    for _ in range(self._rng.randint(2, 5))
                )
            elif kind < 0.42:
                block = f"```python\n{self.code(self._rng.randint(80, 320))}\n```"
            else:
                block = self.words(self._rng.randint(120, 480)).capitalize() + "."
            blocks.append(block)
            size += len(block) + 2
        return "\n\n".join(blocks)[: max(chars, 1)]

    def randint(self, low: int, high: int) -> int:
        return self._rng.randint(low, high)

    def uniform(self, low: float, high: float) -> float:
        return self._rng.uniform(low, high)

    def choice(self, options: Sequence[str]) -> str:
        return self._rng.choice(options)


# --- histories --------------------------------------------------------------

_ROLE_WEIGHTS = {"user": 2.0, "assistant_tools": 0.4, "tool": 3.0, "final": 3.0}
_TOOL_NAMES = ("bash", "read", "search_files")


@dataclass
class _Planned:
    kind: str
    weight: float
    tool_calls: int = 0


@dataclass
class RunSegment:
    """One Run of a synthetic history, in persistence order."""

    run_id: str
    steps: list[list[ChatMessage]] = field(default_factory=list)
    summary: ChatMessage | None = None


@dataclass(frozen=True)
class SyntheticHistory:
    shape: HistoryShape
    runs: tuple[RunSegment, ...]

    @property
    def message_count(self) -> int:
        return sum(
            sum(len(step) for step in run.steps) + (run.summary is not None) for run in self.runs
        )


def build_history(shape: HistoryShape, *, seed: int = 7) -> SyntheticHistory:
    """Plan Runs until the message budget is used, then fill text to the char budget.

    Every Run except the last ends with a ``run_summary``; the last Run stays
    running.
    """
    text = TextFactory(seed)
    plans = _plan_runs(shape.messages, text)
    total_weight = sum(item.weight for plan in plans for item in plan)
    chars_per_weight = shape.chars / total_weight
    clock = _Clock(_HISTORY_START)
    runs: list[RunSegment] = []
    for index, plan in enumerate(plans):
        finished = index < len(plans) - 1
        runs.append(_materialize_run(index, plan, chars_per_weight, text, clock, finished))
    return SyntheticHistory(shape=shape, runs=tuple(runs))


def _plan_runs(message_budget: int, text: TextFactory) -> list[list[_Planned]]:
    plans: list[list[_Planned]] = []
    used = 0
    while used < message_budget:
        plan = [_Planned("user", _jittered("user", text))]
        for _ in range(text.randint(2, 5)):
            calls = text.randint(1, 2)
            plan.append(_Planned("assistant_tools", _jittered("assistant_tools", text), calls))
            plan.extend(_Planned("tool", _jittered("tool", text)) for _ in range(calls))
        plan.append(_Planned("final", _jittered("final", text)))
        plan.append(_Planned("summary", 0.0))
        remaining = message_budget - used
        if len(plan) > remaining:
            plan = _trim_plan(plan, remaining)
        plans.append(plan)
        used += len(plan)
    return plans


def _trim_plan(plan: list[_Planned], budget: int) -> list[_Planned]:
    """Cut a Run to ``budget`` messages at a step boundary, keeping Tool pairs whole."""
    trimmed: list[_Planned] = []
    index = 0
    while index < len(plan):
        item = plan[index]
        group = [item, *plan[index + 1 : index + 1 + item.tool_calls]]
        if len(trimmed) + len(group) > budget:
            break
        trimmed.extend(group)
        index += len(group)
    return trimmed or plan[:1]


def _jittered(kind: str, text: TextFactory) -> float:
    return _ROLE_WEIGHTS[kind] * text.uniform(0.5, 1.5)


class _Clock:
    def __init__(self, start: datetime) -> None:
        self._now = start

    def tick(self, seconds: float) -> datetime:
        self._now += timedelta(seconds=seconds)
        return self._now


def _materialize_run(
    index: int,
    plan: list[_Planned],
    chars_per_weight: float,
    text: TextFactory,
    clock: _Clock,
    finished: bool,
) -> RunSegment:
    run = RunSegment(run_id=f"run-{index:05d}")
    started = clock.tick(30)
    position = 0
    iterations = 0
    while position < len(plan):
        item = plan[position]
        chars = max(1, round(item.weight * chars_per_weight))
        if item.kind == "user":
            run.steps.append([ChatMessage.user(text.words(chars), timestamp=started)])
            position += 1
        elif item.kind == "assistant_tools":
            results = plan[position + 1 : position + 1 + item.tool_calls]
            run.steps.append(
                _tool_step(index, iterations, chars, results, chars_per_weight, text, clock)
            )
            iterations += 1
            position += 1 + item.tool_calls
        elif item.kind == "final":
            answer = ChatMessage.assistant(
                model=AGENT_MODEL,
                content=text.markdown(chars),
                usage={"input_tokens": 1000 + 40 * iterations, "output_tokens": chars // 4},
                timestamp=clock.tick(8),
            )
            run.steps.append([answer])
            iterations += 1
            position += 1
        else:  # summary
            position += 1
            if finished:
                completed = clock.tick(0.2)
                run.summary = ChatMessage.run_summary(
                    run_id=run.run_id,
                    status="completed",
                    timing={
                        "started_at": started.isoformat(),
                        "completed_at": completed.isoformat(),
                        "duration_ms": int((completed - started).total_seconds() * 1000),
                    },
                    iteration_count=max(iterations, 1),
                    timestamp=completed,
                )
    return run


def _tool_step(
    run_index: int,
    step_index: int,
    chars: int,
    results: Sequence[_Planned],
    chars_per_weight: float,
    text: TextFactory,
    clock: _Clock,
) -> list[ChatMessage]:
    calls: list[ToolCall] = []
    for call_index in range(len(results)):
        name = text.choice(_TOOL_NAMES)
        calls.append(
            ToolCall(
                id=f"call-{run_index:05d}-{step_index:02d}-{call_index}",
                name=name,
                arguments=_tool_arguments(name, text),
            )
        )
    assistant = ChatMessage.assistant(
        model=AGENT_MODEL,
        content=text.words(chars),
        usage={"input_tokens": 1000 + 40 * step_index, "output_tokens": chars // 4 + 30},
        tool_calls=calls,
        timestamp=clock.tick(5),
    )
    step = [assistant]
    for call, planned in zip(calls, results, strict=True):
        result_chars = max(1, round(planned.weight * chars_per_weight))
        started = clock.tick(0.1)
        completed = clock.tick(0.4)
        step.append(
            ChatMessage.tool(
                tool_call_id=call.id,
                name=call.name,
                content=json.dumps(
                    tool_success(_tool_data(call.name, result_chars, text)),
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                timing={
                    "started_at": started.isoformat(),
                    "completed_at": completed.isoformat(),
                    "duration_ms": 400,
                },
                timestamp=completed,
            )
        )
    return step


def _tool_arguments(name: str, text: TextFactory) -> dict[str, Any]:
    path = f"src/{text.choice(_WORDS)}/{text.choice(_WORDS)}.py"
    if name == "bash":
        return {"command": f"python -m pytest tests/{text.choice(_WORDS)} -q", "timeout": 120}
    if name == "read":
        return {"path": path, "offset": text.randint(1, 400), "limit": 200}
    return {"pattern": text.choice(_WORDS), "path": "src", "glob": "*.py"}


def _tool_data(name: str, chars: int, text: TextFactory) -> dict[str, Any]:
    if name == "bash":
        return {"exit_code": 0, "output": text.code(chars)}
    if name == "read":
        return {"content": text.code(chars)}
    lines = []
    size = 0
    while size < chars:
        line = f"src/{text.choice(_WORDS)}.py:{text.randint(1, 900)}: {text.words(60)}"
        lines.append(line)
        size += len(line) + 1
    return {"matches": "\n".join(lines)[:chars]}


# --- session store ----------------------------------------------------------


def open_session_store(data_dir: Path) -> ChatSessionManager:
    """Create a fresh, empty Session store the way the application bootstraps one."""
    data_dir.mkdir(parents=True, exist_ok=True)
    write_bootstrap_marker(data_dir)
    return ChatSessionManager(data_dir)


@dataclass(frozen=True)
class LoadedHistory:
    """A persisted synthetic history and its ``load_active()`` result."""

    shape: HistoryShape
    session: ChatSession
    messages: list[ChatMessage]

    @property
    def content_chars(self) -> int:
        return sum(
            len(message.content) for message in self.messages if isinstance(message.content, str)
        )


def loaded_history(context: BenchContext, shape: HistoryShape) -> LoadedHistory:
    """Persist ``shape``'s history once per suite run and load it back."""

    def build() -> LoadedHistory:
        session = history_store(context).create(AGENT_ID)
        persist_history(session, build_history(shape))
        return LoadedHistory(shape=shape, session=session, messages=session.load_active())

    return context.fixture(f"history:{shape.label}", build)


def history_store(context: BenchContext) -> ChatSessionManager:
    """The one Session store per suite run that holds every synthetic history."""
    return context.fixture("history-store", lambda: session_store(context, "history-store"))


def session_store(context: BenchContext, name: str) -> ChatSessionManager:
    """Open a fresh Session store under the suite's work directory; closed at the end."""
    manager = open_session_store(context.work_dir / name)
    context.add_cleanup(manager.close)
    return manager


def persist_history(session: ChatSession, history: SyntheticHistory) -> None:
    """Write a history through the Session writer API, one Model step per batch.

    A step's Tool results name their owning assistant message through the
    writer's ``assistant_message_id``, as Chat's Tool dispatch does.
    """
    for run in history.runs:
        writer = session.start_run(run.run_id)
        for step in run.steps:
            if step[0].role == "assistant" and step[0].tool_calls:
                writer.assistant_message_id = step[0].id
            writer.append_many(step)
        if run.summary is not None:
            writer.append(run.summary)


# --- Provider ---------------------------------------------------------------

PERF_PROVIDER_CONFIG = ProviderConfig(
    id=PROVIDER_ID,
    name="Perf Bench",
    adapter="openai_compatible",
    base_url=BASE_URL,
    connections=[
        ConnectionConfig(
            id="api-key",
            type="api_key",
            label="API Key",
            auth=AuthConfig(
                header="Authorization",
                prefix="Bearer ",
                credential_key="PERF_BENCH_API_KEY",
            ),
        )
    ],
    defaults={"max_tokens": 8192},
)

PERF_MODEL = Model(
    model_id=MODEL_ID,
    name="Perf Model",
    capabilities=Capabilities(
        vision=False,
        tools=True,
        json_mode=True,
        reasoning=ReasoningCapabilities(supported=False),
    ),
    context_window=CONTEXT_WINDOW,
    max_output_tokens=MAX_OUTPUT_TOKENS,
)


def _model_lookup(model_id: str) -> Model | None:
    return PERF_MODEL if model_id == MODEL_ID else None


class MemoryByteStream(httpx.AsyncByteStream):
    """An async response body that yields prepared byte chunks."""

    def __init__(self, chunks: Sequence[bytes]) -> None:
        self._chunks = tuple(chunks)

    async def __aiter__(self):  # type: ignore[override]
        for chunk in self._chunks:
            yield chunk


async def offline_adapter(
    response_chunks: Callable[[], Sequence[bytes]] | None = None,
) -> OpenAICompatibleAdapter:
    """Build the real OpenAI-compatible Adapter with an in-memory HTTP transport.

    Every request is answered with ``response_chunks()`` as an SSE body. The
    Adapter's own client is closed and replaced through its private
    ``_client`` attribute because the Adapter has no transport injection point.
    """
    adapter = OpenAICompatibleAdapter(
        PERF_PROVIDER_CONFIG,
        "perf-bench-key",
        model_lookup=_model_lookup,
    )
    chunks = response_chunks or (lambda: (b"data: [DONE]\n\n",))

    def handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=MemoryByteStream(chunks()),
        )

    original = adapter._client
    if not isinstance(original, httpx.AsyncClient):
        raise TypeError("OpenAICompatibleAdapter no longer keeps an httpx.AsyncClient in _client")
    await original.aclose()
    adapter._client = httpx.AsyncClient(base_url=BASE_URL, transport=httpx.MockTransport(handle))
    return adapter


def shared_adapter(context: BenchContext) -> OpenAICompatibleAdapter:
    """One offline Adapter per suite run for request-building benchmarks."""

    def build() -> OpenAICompatibleAdapter:
        adapter = context.run(offline_adapter())
        context.add_cleanup(adapter.aclose)
        return adapter

    return context.fixture("adapter", build)


# --- Provider SSE bodies ----------------------------------------------------

STREAM_CHUNKS = 2000
_STREAM_CREATED = 1_767_600_000


def _sse_event(payload: dict[str, Any]) -> bytes:
    return f"data: {json.dumps(payload, separators=(',', ':'))}\n\n".encode()


def _completion_chunk(delta: dict[str, Any], finish_reason: str | None = None) -> dict[str, Any]:
    return {
        "id": "chatcmpl-perf",
        "object": "chat.completion.chunk",
        "created": _STREAM_CREATED,
        "model": MODEL_ID,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }


def _stream_tail(finish_reason: str, completion_tokens: int) -> list[bytes]:
    usage = {
        "prompt_tokens": 50_000,
        "completion_tokens": completion_tokens,
        "total_tokens": 50_000 + completion_tokens,
    }
    return [
        _sse_event(_completion_chunk({}, finish_reason)),
        _sse_event(
            {
                "id": "chatcmpl-perf",
                "object": "chat.completion.chunk",
                "created": _STREAM_CREATED,
                "model": MODEL_ID,
                "choices": [],
                "usage": usage,
            }
        ),
        b"data: [DONE]\n\n",
    ]


def content_stream_events(chunks: int = STREAM_CHUNKS, *, seed: int = 17) -> tuple[bytes, ...]:
    """An OpenAI chat-completions SSE body with ``chunks`` short content deltas.

    Each SSE event is its own network chunk, the worst case for per-chunk cost.
    """
    text = TextFactory(seed)
    events = [_sse_event(_completion_chunk({"role": "assistant", "content": ""}))]
    events.extend(
        _sse_event(_completion_chunk({"content": text.words(text.randint(3, 9)) + " "}))
        for _ in range(chunks)
    )
    events.extend(_stream_tail("stop", chunks))
    return tuple(events)


def tool_call_stream_events(chunks: int = STREAM_CHUNKS, *, seed: int = 19) -> tuple[bytes, ...]:
    """An SSE body streaming one Tool Call whose JSON arguments arrive in ``chunks`` pieces."""
    text = TextFactory(seed)
    arguments = json.dumps({"path": "notes/plan.md", "content": text.markdown(chunks * 12)})
    bounds = [round(index * len(arguments) / chunks) for index in range(chunks + 1)]
    fragments = [arguments[start:end] for start, end in zip(bounds, bounds[1:], strict=False)]
    first = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "index": 0,
                "id": "call_perf_write",
                "type": "function",
                "function": {"name": "write", "arguments": ""},
            }
        ],
    }
    events = [_sse_event(_completion_chunk(first))]
    events.extend(
        _sse_event(
            _completion_chunk({"tool_calls": [{"index": 0, "function": {"arguments": fragment}}]})
        )
        for fragment in fragments
    )
    events.extend(_stream_tail("tool_calls", len(fragments)))
    return tuple(events)


# --- request inputs ---------------------------------------------------------


def system_prompt(chars: int = SYSTEM_PROMPT_CHARS, *, seed: int = 11) -> str:
    """A synthetic System Prompt of about ``chars`` characters."""
    return TextFactory(seed).markdown(chars)


def tool_definitions(count: int = TOOL_DEFINITION_COUNT, *, seed: int = 13) -> list[dict[str, Any]]:
    """Synthetic provider Tool definitions (name, description, JSON Schema parameters)."""
    text = TextFactory(seed)
    definitions: list[dict[str, Any]] = []
    for index in range(count):
        properties: dict[str, Any] = {}
        for prop_index in range(text.randint(2, 6)):
            properties[f"{text.choice(_WORDS)}_{prop_index}"] = {
                "type": text.choice(("string", "integer", "boolean")),
                "description": text.words(text.randint(60, 180)),
            }
        definitions.append(
            {
                "name": f"tool_{index:02d}_{text.choice(_WORDS)}",
                "description": text.words(text.randint(300, 900)),
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": sorted(properties)[:1],
                    "additionalProperties": False,
                },
            }
        )
    return definitions
