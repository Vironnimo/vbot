"""Tests for the minimal non-streaming agentic chat loop."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from core.chat import (
    ChatLoop,
    ChatLoopDependencies,
    ChatMessage,
    ChatSessionManager,
)
from core.projects import AgentResolutionError, ConfigAgent
from core.providers.reasoning import (
    ReasoningReplayPolicy,
)
from core.runs import (
    ChatRunManager,
)
from core.tools import (
    ToolRegistry,
)
from core.tools.file_state import FileReadState

JsonObject = dict[str, Any]


def build_chat_loop(runtime: Any, **kwargs: Any) -> ChatLoop:
    """Construct ChatLoop from a runtime-shaped test double."""
    missing = SimpleNamespace()
    dependencies = ChatLoopDependencies(
        agent_resolver=cast(Any, getattr(runtime, "agent_resolver", missing)),
        projects=cast(Any, getattr(runtime, "projects", missing)),
        providers=cast(Any, getattr(runtime, "providers", missing)),
        models=cast(Any, getattr(runtime, "models", missing)),
        provider_credentials=cast(Any, getattr(runtime, "provider_credentials", missing)),
        sessions=cast(Any, getattr(runtime, "chat_sessions", missing)),
        run_manager=cast(Any, getattr(runtime, "chat_run_manager", missing)),
        tools=cast(Any, getattr(runtime, "tools", missing)),
        process_manager=cast(Any, getattr(runtime, "process_manager", missing)),
        file_read_state=cast(Any, getattr(runtime, "file_read_state", missing)),
        storage=cast(Any, getattr(runtime, "storage", missing)),
        get_extension_registry=lambda: getattr(runtime, "extensions", None),
        get_system_prompts=lambda: runtime.system_prompts,
        get_adapter=lambda provider_id, connection_id: runtime.get_adapter(
            provider_id, connection_id
        ),
        resolve_skills=lambda project_id, agent_id: runtime.skills_for(project_id, agent_id),
        list_project_skills=lambda project_id: runtime.project_own_skills(project_id),
        get_local_context_windows=lambda: runtime.local_context_windows(),
    )
    return ChatLoop(dependencies, **kwargs)


def persisted_roles(messages: list[ChatMessage]) -> list[str]:
    return [message.role for message in messages if message.role != "run_summary"]


def persisted_dict_roles(messages: list[JsonObject]) -> list[str]:
    return [str(message["role"]) for message in messages if message.get("role") != "run_summary"]


@dataclass(frozen=True)
class StubAgent:
    id: str
    model: str
    fallback_model: str = ""
    temperature: float = 0.1
    thinking_effort: str = "high"
    allowed_tools: list[str] | None = None
    allowed_skills: list[str] | None = None
    workspace: Path | None = None
    root_project_id: str | None = None


class StubAgents:
    def __init__(self, agent: StubAgent) -> None:
        self._agent = agent

    def get(self, agent_id: str) -> StubAgent:
        assert agent_id == self._agent.id
        return self._agent


@dataclass(frozen=True)
class StubProject:
    """Minimal project shape the chat loop reads for prompt/cwd context."""

    project_id: str
    cwd: str
    auto_load: list[str]
    display_name: str = ""


class StubProjects:
    """Project-store stub returning ``StubProject`` by id for prompt context."""

    def __init__(self, projects: dict[str, StubProject]) -> None:
        self._projects = dict(projects)

    def get(self, project_id: str) -> StubProject:
        return self._projects[project_id]

    def list(self) -> list[StubProject]:
        return [self._projects[project_id] for project_id in sorted(self._projects)]


class StubAgentResolver:
    """Resolver stub mirroring the runtime seam the chat loop now calls.

    ``project_id=None`` delegates to the identity ``StubAgents`` (byte-for-byte
    today's path). A set ``project_id`` returns the config agent registered for
    ``(project_id, agent_id)`` when one is given, otherwise falls back to the
    identity agent so a project run still resolves something runnable; an
    ``(project_id, agent_id)`` in ``unresolvable`` raises
    :class:`AgentResolutionError`, modelling an off-Team target or a model chain
    that fell through.
    """

    def __init__(
        self,
        agents: StubAgents,
        project_agents: dict[tuple[str, str], StubAgent | ConfigAgent] | None = None,
        unresolvable: set[tuple[str, str]] | None = None,
    ) -> None:
        self._agents = agents
        self._project_agents = dict(project_agents or {})
        self._unresolvable = set(unresolvable or set())
        self.calls: list[tuple[str | None, str]] = []

    def resolve_agent(self, project_id: str | None, agent_id: str) -> StubAgent | ConfigAgent:
        self.calls.append((project_id, agent_id))
        if project_id is None:
            return self._agents.get(agent_id)
        if (project_id, agent_id) in self._unresolvable:
            raise AgentResolutionError(f"agent '{agent_id}' is not on project '{project_id}' team")
        return self._project_agents.get((project_id, agent_id)) or self._agents.get(agent_id)


class StubProviders:
    def __init__(self, provider_ids: set[str], *, base_url: str | None = None) -> None:
        self._provider_ids = provider_ids
        self._base_url = base_url

    def get(self, provider_id: str) -> object:
        if provider_id not in self._provider_ids:
            raise KeyError(provider_id)
        return StubProviderConfig(
            [StubConnection("subscription"), StubConnection("api-key")],
            base_url=self._base_url,
        )


@dataclass(frozen=True)
class StubConnection:
    id: str


@dataclass(frozen=True)
class StubProviderConfig:
    connections: list[StubConnection]
    context_window: int | None = None
    base_url: str | None = None


class StubPrompts:
    def __init__(self, tool_registry: ToolRegistry | None = None) -> None:
        self.agent_for_tools: StubAgent | None = None
        self.tool_registry = tool_registry
        self.app_dir = Path("app")
        self.build_calls: list[tuple[str, str, Any]] = []
        self.render_project_files_calls: list[Any] = []
        self.render_skill_catalog_calls = 0

    def build_system_prompt(
        self,
        agent: StubAgent,
        scope: Any = None,
        *,
        agent_body: str = "",
        project_context: Any = None,
        agent_project_id: str | None = None,
        skill_registry: Any = None,
        skill_catalog: Any = None,
        read_paths: list[Path] | None = None,
        effective_tool_names: Any = None,
        session_tool_grants: Any = (),
    ) -> str:
        del agent_project_id
        self.build_calls.append((agent.id, agent_body, project_context))
        # Echo the body and rendered project files so chat tests can assert what
        # actually reaches the system message, mirroring the real builder's slots
        # (body in the identity slot, project files after it). Thread read_paths
        # through render_project_files so project files are reported as read, like
        # the real builder (SOUL/memory are not modeled by this stub).
        on_read = read_paths.append if read_paths is not None else None
        parts = [
            agent_body,
            f"System for {agent.id}",
            self.render_project_files(project_context, on_read=on_read),
        ]
        return "\n".join(part for part in parts if part)

    def render_skill_catalog(self, agent: StubAgent, skill_registry: Any = None) -> Any:
        from core.prompts import PinnedSkillCatalog

        # Reflect the live registry's skill count so tests can detect a re-render
        # (a fresh snapshot) versus a reused session-pinned one.
        self.render_skill_catalog_calls += 1
        skills = (
            skill_registry.list_all()
            if skill_registry is not None and hasattr(skill_registry, "list_all")
            else []
        )
        return PinnedSkillCatalog(catalog_text=f"catalog:{len(skills)}")

    def render_visiting_project_skills(self, project_name: str, skills: Any) -> str:
        if not skills:
            return ""
        lines = [
            f"Skills from project '{project_name}' — read a skill's SKILL.md "
            "with the `read` tool to use it:"
        ]
        lines.extend(f"- {skill.name}: {skill.description} ({skill.path})" for skill in skills)
        return "\n".join(lines)

    def render_project_files(self, project_context: Any, *, on_read: Any = None) -> str:
        self.render_project_files_calls.append(project_context)
        if project_context is None:
            return ""
        # Wrap each existing project file exactly like the real renderer so chat
        # tests see the same <file>-framed content — the auto_load list in order
        # (AGENTS.md is just its seeded first entry, no longer special-cased).
        blocks: list[str] = []
        for name in project_context.auto_load:
            file_path = Path(project_context.cwd) / name
            if file_path.exists():
                blocks.append(f'<file name="{name}">\n{file_path.read_text()}\n</file>')
                if on_read is not None:
                    on_read(file_path.resolve())
        return "\n".join(blocks)

    def provider_tool_definitions(
        self,
        agent: StubAgent,
        *,
        skill_registry: Any = None,
        skill_catalog: Any = None,
        session_tool_grants: Any = (),
    ) -> list[JsonObject]:
        self.agent_for_tools = agent
        allowed = agent.allowed_tools
        definitions = (
            self.tool_registry.provider_definitions(
                allowed,
                session_grants=session_tool_grants,
            )
            if self.tool_registry is not None
            else []
        )
        if allowed is None or "*" in allowed or "get_weather" in allowed:
            weather = {
                "name": "get_weather",
                "description": "Get weather.",
                "parameters": {"type": "object"},
            }
            definitions = [weather, *definitions]
        return list({str(definition["name"]): definition for definition in definitions}.values())


@dataclass(frozen=True)
class StubSkill:
    name: str
    description: str
    path: Path


class StubSkills:
    def __init__(self, skills: list[StubSkill]) -> None:
        self._skills = {skill.name: skill for skill in skills}

    def list_all(self) -> list[StubSkill]:
        return sorted(self._skills.values(), key=lambda skill: skill.name)

    def is_allowed(self, name: str, allowed_skills: list[str] | None) -> bool:
        if name not in self._skills:
            return False
        if allowed_skills is None or "*" in allowed_skills:
            return True
        return name in allowed_skills

    def availability_for(
        self,
        name: str,
        allowed_skills: list[str] | None = None,
    ) -> Any:
        del allowed_skills
        if name in self._skills:
            return SimpleNamespace(state="available", missing=())
        return SimpleNamespace(state="invalid", missing=(f"skill '{name}' is not loadable",))

    def filter_allowed(self, allowed_skills: list[str]) -> list[StubSkill]:
        if "*" in allowed_skills:
            return self.list_all()
        return [self._skills[name] for name in allowed_skills if name in self._skills]


class StubAdapter:
    def __init__(self, responses: list[Any], *, stream_responses: list[Any] | None = None) -> None:
        self._responses = responses
        self._stream_responses = stream_responses or []
        self.requests: list[JsonObject] = []
        self.stream_requests: list[JsonObject] = []

    async def send(self, messages: list[JsonObject], *, model_id: str, **kwargs: Any) -> JsonObject:
        self.requests.append(
            {"messages": deepcopy(messages), "model_id": model_id, "kwargs": deepcopy(kwargs)}
        )
        if not self._responses:
            raise AssertionError("unexpected adapter request")
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return cast(JsonObject, response)

    def normalize_response(
        self, response: JsonObject, *, model_id: str | None = None
    ) -> JsonObject:
        return response

    async def stream(
        self,
        messages: list[JsonObject],
        *,
        model_id: str,
        **kwargs: Any,
    ) -> Any:
        self.stream_requests.append(
            {"messages": deepcopy(messages), "model_id": model_id, "kwargs": deepcopy(kwargs)}
        )
        if not self._stream_responses:
            raise AssertionError("unexpected adapter stream request")
        response = self._stream_responses.pop(0)
        if isinstance(response, Exception):
            raise response
        for delta in response:
            if isinstance(delta, Exception):
                raise delta
            yield delta


class ClosingStubAdapter(StubAdapter):
    def __init__(self, responses: list[JsonObject]) -> None:
        super().__init__(responses)
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


class BlockingStubAdapter(StubAdapter):
    def __init__(self) -> None:
        super().__init__([])
        self.request_started = asyncio.Event()
        self.release = asyncio.Event()

    async def send(self, messages: list[JsonObject], *, model_id: str, **kwargs: Any) -> JsonObject:
        self.requests.append(
            {"messages": deepcopy(messages), "model_id": model_id, "kwargs": deepcopy(kwargs)}
        )
        self.request_started.set()
        await self.release.wait()
        return {"content": "Late", "tool_calls": None}


class BlockingStreamingStubAdapter(ClosingStubAdapter):
    def __init__(self) -> None:
        super().__init__([])
        self.stream_started = asyncio.Event()
        self.release = asyncio.Event()

    async def stream(
        self,
        messages: list[JsonObject],
        *,
        model_id: str,
        **kwargs: Any,
    ) -> Any:
        self.stream_requests.append(
            {"messages": deepcopy(messages), "model_id": model_id, "kwargs": deepcopy(kwargs)}
        )
        yield {"type": "content_delta", "text": "before"}
        self.stream_started.set()
        await self.release.wait()
        yield {"type": "content_delta", "text": "late"}


class BlockingReasoningStreamingStubAdapter(ClosingStubAdapter):
    """Streams only reasoning, then blocks — the zero-visible-output cancel case."""

    def __init__(self) -> None:
        super().__init__([])
        self.stream_started = asyncio.Event()
        self.release = asyncio.Event()

    async def stream(
        self,
        messages: list[JsonObject],
        *,
        model_id: str,
        **kwargs: Any,
    ) -> Any:
        self.stream_requests.append(
            {"messages": deepcopy(messages), "model_id": model_id, "kwargs": deepcopy(kwargs)}
        )
        yield {"type": "reasoning_delta", "text": "Thinking hard."}
        self.stream_started.set()
        await self.release.wait()
        yield {"type": "content_delta", "text": "late"}


class TenToolsThenBlockingReasoningAdapter(ClosingStubAdapter):
    """Completes ten tools, then exposes readable work until cancellation."""

    def __init__(self) -> None:
        super().__init__([])
        self.second_step_started = asyncio.Event()

    async def stream(
        self,
        messages: list[JsonObject],
        *,
        model_id: str,
        **kwargs: Any,
    ) -> Any:
        self.stream_requests.append(
            {"messages": deepcopy(messages), "model_id": model_id, "kwargs": deepcopy(kwargs)}
        )
        if len(self.stream_requests) == 1:
            yield {"type": "reasoning_delta", "text": "Plan the batch. "}
            yield {"type": "reasoning_delta", "text": "Inspect every result."}
            for index in range(10):
                yield {
                    "type": "tool_call_delta",
                    "id": f"call-{index}",
                    "name_delta": "get_weather",
                    "arguments_delta": '{"city":"Berlin"}',
                }
            yield {"type": "finish", "reason": "tool_calls"}
            return
        if len(self.stream_requests) == 2:
            yield {"type": "reasoning_delta", "text": "Review the completed batch. "}
            yield {"type": "reasoning_delta", "text": "Prepare the final answer."}
            self.second_step_started.set()
            await asyncio.Event().wait()
        raise AssertionError("unexpected adapter stream request")


class StalledStreamingStubAdapter(StubAdapter):
    async def stream(
        self,
        messages: list[JsonObject],
        *,
        model_id: str,
        **kwargs: Any,
    ) -> Any:
        self.stream_requests.append(
            {"messages": deepcopy(messages), "model_id": model_id, "kwargs": deepcopy(kwargs)}
        )
        yield {"type": "content_delta", "text": "partial"}
        await asyncio.sleep(1)
        yield {"type": "content_delta", "text": "late"}


class SlowStreamingStubAdapter(StubAdapter):
    """Streams visible content, pauses, then completes — to probe the stall guard.

    With a short chunk timeout the pause trips a chunk stall for remote providers;
    for local providers (where the guard is disabled) the same pause completes.
    """

    def __init__(self, *, delay: float) -> None:
        super().__init__([])
        self._delay = delay

    async def stream(
        self,
        messages: list[JsonObject],
        *,
        model_id: str,
        **kwargs: Any,
    ) -> Any:
        self.stream_requests.append(
            {"messages": deepcopy(messages), "model_id": model_id, "kwargs": deepcopy(kwargs)}
        )
        yield {"type": "content_delta", "text": "partial"}
        await asyncio.sleep(self._delay)
        yield {"type": "content_delta", "text": " done"}
        yield {"type": "finish", "reason": "stop"}


class MidStreamCancelledStubAdapter(StubAdapter):
    async def stream(
        self,
        messages: list[JsonObject],
        *,
        model_id: str,
        **kwargs: Any,
    ) -> Any:
        self.stream_requests.append(
            {"messages": deepcopy(messages), "model_id": model_id, "kwargs": deepcopy(kwargs)}
        )
        yield {"type": "reasoning_delta", "text": "Need network."}
        raise asyncio.CancelledError


class PolicyStubAdapter(StubAdapter):
    """Stub adapter declaring an explicit reasoning replay policy."""

    def __init__(self, responses: list[Any], *, policy: ReasoningReplayPolicy) -> None:
        super().__init__(responses)
        self._policy = policy
        self.policy_queries: list[str] = []

    def reasoning_replay_policy(self, model_id: str) -> ReasoningReplayPolicy:
        self.policy_queries.append(model_id)
        return self._policy


class StubProcessManager:
    def __init__(self) -> None:
        self.cancelled_scopes: list[str] = []

    def cancel_scope(self, run_id: str) -> None:
        self.cancelled_scopes.append(run_id)


class StubRuntime:
    def __init__(
        self,
        *,
        data_dir: Path,
        agent: StubAgent,
        adapter: StubAdapter,
        adapters_by_connection: dict[str, StubAdapter] | None = None,
        raise_on_connection: dict[str, Exception] | None = None,
        provider_ids: set[str] | None = None,
        provider_base_url: str | None = None,
        tools: ToolRegistry | None = None,
        storage: Any | None = None,
        models: Any | None = None,
        project_agents: dict[tuple[str, str], StubAgent | ConfigAgent] | None = None,
        unresolvable_agents: set[tuple[str, str]] | None = None,
        projects: Any | None = None,
    ) -> None:
        self.agents = StubAgents(agent)
        self.agent_resolver = StubAgentResolver(self.agents, project_agents, unresolvable_agents)
        self.projects = projects if projects is not None else StubProjects({})
        self.chat_sessions = ChatSessionManager(data_dir)
        # Real guard instance so tests can assert auto-injected prompt files are
        # stamped as read-before-write for the session.
        self.file_read_state = FileReadState()
        self.tools = tools or ToolRegistry()
        self.system_prompts = StubPrompts(self.tools)
        self.chat_runs = ChatRunManager()
        self.chat_run_manager = self.chat_runs
        self.process_manager = StubProcessManager()
        self.extensions: Any = None
        self.providers = StubProviders(
            provider_ids or {agent.model.split("/", 1)[0]},
            base_url=provider_base_url,
        )
        self.provider_credentials = StubProviderCredentials(
            {f"{agent.model.split('/', 1)[0]}:api-key"}
        )
        self.skills: Any = StubSkills([])
        self.storage = (
            storage
            if storage is not None
            else StubStorage(
                {"auto": False, "threshold": 0.8, "tail_tokens": 15_000, "summary_model": None},
                data_dir=data_dir,
            )
        )
        self.models = models if models is not None else StubModels({})
        self.adapter = adapter
        self.adapters_by_connection = dict(adapters_by_connection or {})
        self.raise_on_connection = dict(raise_on_connection or {})
        self.adapter_provider_id: str | None = None
        self.adapter_connection_id: str | None = None
        # Records every ``skills_for`` resolution so tests can assert the effective
        # (project_id, agent_id) a run resolves skills against — e.g. that a rooted
        # identity run resolves against its home project, not ``None``.
        self.skills_for_calls: list[tuple[str | None, str | None]] = []
        # Project-own skills the visiting reminder lists; tests set this per project.
        self.project_own_skills_result: list[Any] = []

    def get_adapter(self, provider_id: str, connection_id: str) -> StubAdapter:
        self.adapter_provider_id = provider_id
        self.adapter_connection_id = connection_id
        if connection_id in self.raise_on_connection:
            raise self.raise_on_connection[connection_id]
        if connection_id in self.adapters_by_connection:
            return self.adapters_by_connection[connection_id]
        return self.adapter

    def skills_for(self, project_id: str | None = None, agent_id: str | None = None) -> Any:
        # The stub holds one skill registry; project/agent scoping is exercised by
        # the runtime tests. Read ``self.skills`` dynamically so tests that reassign
        # it after construction still take effect. The call is recorded so tests can
        # assert the effective project/agent a run resolves skills against.
        self.skills_for_calls.append((project_id, agent_id))
        return self.skills

    def project_skill_names(self, _project_id: str | None = None) -> frozenset[str]:
        return frozenset()

    def project_own_skills(self, _project_id: str) -> list[Any]:
        # Visiting-reminder source: tests set ``project_own_skills_result`` to skill
        # objects (name/description/path); default empty means no skills section.
        return self.project_own_skills_result

    def local_context_windows(self) -> JsonObject:
        # Mirrors Runtime.local_context_windows: the live user-configured
        # local-model window map, read from storage at call time.
        return cast(JsonObject, self.storage.load_local_models_settings()["context_windows"])


class StubProviderCredentials:
    def __init__(self, usable_connection_ids: set[str]) -> None:
        self._usable_connection_ids = usable_connection_ids

    def has_credentials(self, _provider_id: str, connection_id: str | None = None) -> bool:
        return connection_id in self._usable_connection_ids

    def is_usable(self, provider_id: str, connection_id: str | None = None) -> bool:
        return self.has_credentials(provider_id, connection_id)


@dataclass(frozen=True)
class StubModelEntry:
    context_window: int | None
    connections: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


class StubModels:
    def __init__(self, entries: dict[tuple[str, str], int | None]) -> None:
        self._entries = {
            (provider_id, model_id): StubModelEntry(context_window=context_window)
            for (provider_id, model_id), context_window in entries.items()
        }

    def get(self, provider_id: str, model_id: str) -> StubModelEntry:
        key = (provider_id, model_id)
        if key not in self._entries:
            raise KeyError(key)
        return self._entries[key]


class StubStorage:
    def __init__(self, compaction_settings: JsonObject, *, data_dir: Path | None = None) -> None:
        if "trigger" not in compaction_settings:
            self._compaction_settings = {
                "enabled": compaction_settings.get("auto", True),
                "trigger": {
                    "type": "context_ratio",
                    "threshold": compaction_settings.get("threshold", 0.8),
                },
                "strategy": {
                    "type": "summary_tail",
                    "tail_tokens": compaction_settings.get("tail_tokens", 15_000),
                    "summary_model": compaction_settings.get("summary_model"),
                },
            }
        else:
            self._compaction_settings = dict(compaction_settings)
        self.data_dir = data_dir or Path("data")

    def load_compaction_settings(self) -> JsonObject:
        return dict(self._compaction_settings)

    def load_local_models_settings(self) -> JsonObject:
        return {"context_windows": {}}


class StubCompactionService:
    def __init__(
        self,
        *,
        should_auto: bool,
        estimated_tokens: int = 0,
        checkpoint: ChatMessage | None = None,
        compact_error: Exception | None = None,
    ) -> None:
        self._should_auto = should_auto
        self._estimated_tokens = estimated_tokens
        self._checkpoint = checkpoint
        self._compact_error = compact_error
        self.should_auto_calls: list[tuple[int, int, float]] = []
        self.estimate_calls: list[list[JsonObject]] = []
        self.compact_calls: list[JsonObject] = []

    def should_auto_compact(
        self,
        input_tokens: int,
        context_window: int,
        threshold: float,
        **kwargs: Any,
    ) -> bool:
        self.should_auto_calls.append((input_tokens, context_window, threshold))
        return self._should_auto

    def estimate_messages_tokens(self, messages: list[JsonObject]) -> int:
        self.estimate_calls.append([dict(message) for message in messages])
        return self._estimated_tokens

    async def compact(
        self,
        messages: list[ChatMessage],
        *,
        agent: Any,
        summary_adapter: Any,
        summary_model_id: str,
        storage: Any,
        settings: Any,
        instruction: str | None = None,
        **kwargs: Any,
    ) -> ChatMessage:
        self.compact_calls.append(
            {
                "message_roles": persisted_roles(messages),
                "agent_id": getattr(agent, "id", None),
                "summary_adapter": summary_adapter,
                "summary_model_id": summary_model_id,
                "storage": storage,
                "summary_model": getattr(settings, "summary_model", None),
                "instruction": instruction,
            }
        )
        if self._compact_error is not None:
            raise self._compact_error
        if self._checkpoint is None:
            raise AssertionError("StubCompactionService requires checkpoint for successful compact")
        return self._checkpoint


def _write_test_skill(tmp_path: Path, name: str) -> Path:
    skill_dir = tmp_path / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text(
        f"""---
name: {name}
description: Test skill.
---

# {name}

Use this skill content.
""",
        encoding="utf-8",
    )
    return skill_file


class RecordingReflection:
    def __init__(self, *, raise_on_notify: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self._raise_on_notify = raise_on_notify

    def notify_run_end(self, run: Any, agent: Any, *, internal: bool, outcome: str) -> None:
        if self._raise_on_notify:
            raise RuntimeError("reflection exploded")
        self.calls.append(
            {
                "agent_id": run.agent_id,
                "session_id": run.session_id,
                "agent": agent,
                "internal": internal,
                "outcome": outcome,
            }
        )
