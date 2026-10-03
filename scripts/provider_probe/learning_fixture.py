"""Disposable vBot fixture that renders and dispatches learning evaluations like production.

Each worker owns one Runtime on a temporary data directory and reuses it for
its attempts: the fixture Identity Agent (``main``), the global Skill home and
its Skill history and archive are restored to their state after startup before
every attempt. The System
Prompt comes from production prompt assembly with the attempt's pinned Memory
and Skill catalog, the Model is offered the Agent's full effective Tool
definitions, and Tool calls run through the production Tool executor; a scope's
Tool restriction and its denial answer apply only at dispatch, as in a
Reflection review or a Librarian pass. Calls carry the production Run kind (a
review kind, ``librarian``, or ``user`` for ``/learn`` and the source Session's
own calls), so background guards such as ``skill_manage``'s protection of
user-created and pinned Skills apply as in production.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast

from scripts.provider_probe.learning_texts import (
    AppliedTexts,
    TextPack,
    apply_tool_texts,
    brief_text,
    current_texts,
    merge_text_pack,
)

EVAL_AGENT_ID = "main"
# ``/learn`` runs with every Tool in production; the harness narrows dispatch to
# the learning Tools so an evaluation never runs commands or reaches the network.
LEARN_DISPATCH_TOOLS: tuple[str, ...] = ("memory", "skill", "skill_manage")
TOOL_ROUTE_NOTE = (
    "Tool definitions are the Agent's provider definitions with the production bash "
    "projection at nesting depth 0 and without analyze_image, which the route drops "
    "while no image-understanding Model task is configured."
)
_TOOL_CONTENT_SEPARATORS = (",", ":")
# The Run kind each seeded Skill origin was written under, as production records it.
_SEED_RUN_KINDS: dict[str, str | None] = {
    "agent": None,
    "reflection": "skill_reflection",
    "librarian": "librarian",
}


@dataclass
class PreparedAttempt:
    """The seeded state and the first request of one attempt."""

    system_prompt: str
    definitions: list[dict[str, Any]]
    messages: list[dict[str, Any]]
    restriction: tuple[str, ...]
    # Production's answer to a call outside a review's scope; ``None`` for /learn.
    denial_resolver: Callable[[str], str | None] | None
    session_id: str
    run_id: str
    # The Agent's configured sampling; ``None`` leaves the Provider default.
    agent_temperature: float | None
    agent_top_p: float | None
    activated: dict[str, str]
    # The production Run kind value of the attempt's Tool calls.
    run_kind: str = "user"


def _activation_hook(activated: dict[str, str]) -> Callable[[str, str], bool]:
    """Track Skill activations per attempt like a Session does (same content: not new)."""

    def activate(name: str, content: str) -> bool:
        if activated.get(name) == content:
            return False
        activated[name] = content
        return True

    return activate


def tool_message_content(result: Mapping[str, Any]) -> str:
    """Serialize a Tool result the way Chat stores it in the Session."""
    return json.dumps(result, ensure_ascii=False, separators=_TOOL_CONTENT_SEPARATORS)


class EvalWorker:
    """One disposable data directory and Runtime that runs attempts one at a time."""

    def __init__(
        self,
        *,
        agent_model: str | None,
        pack: TextPack | None,
        thinking_effort: str | None = None,
    ) -> None:
        self._agent_model = agent_model
        self._thinking_effort = thinking_effort
        self._pack = pack
        self._temporary: TemporaryDirectory[str] | None = None
        self._runtime: Any = None
        self._baseline_files: dict[Path, bytes] = {}
        self._baseline_dirs: set[Path] = set()
        self.applied: AppliedTexts | None = None

    @property
    def runtime(self) -> Any:
        if self._runtime is None:
            raise RuntimeError("EvalWorker is not started")
        return self._runtime

    def start(self) -> None:
        """Start the Runtime, configure the fixture Agent and apply the text pack."""
        from core.runtime.runtime import Runtime
        from core.utils.config import Config
        from scripts.provider_probe.common import _start_probe_runtime

        self._temporary = TemporaryDirectory(
            prefix="vbot-learning-eval-", ignore_cleanup_errors=True
        )
        root = Path(self._temporary.name)
        env = root / ".env"
        env.write_text("LOG_LEVEL=WARNING\n", encoding="utf-8")
        self._runtime = Runtime(Config(data_dir=root / "data", env_path=env))
        _start_probe_runtime(self._runtime)
        runtime = self._runtime
        # The System Prompt names the Agent's Model and thinking effort.
        changes = {
            key: value
            for key, value in (
                ("model", self._agent_model),
                ("thinking_effort", self._thinking_effort),
            )
            if value
        }
        if changes:
            runtime.agents.update(EVAL_AGENT_ID, **changes)
        agent = self._agent()
        current = current_texts(
            self._route_definitions(agent),
            runtime.system_prompts.list_blocks(),
            runtime.storage.read_prompt_fragment,
        )
        self.applied = merge_text_pack(current, self._pack)
        for text_id in self.applied.changed:
            if text_id.startswith("block:"):
                block_id = text_id.removeprefix("block:")
                runtime.system_prompts.update_block(block_id, self.applied.texts.blocks[block_id])
        self._baseline_files, self._baseline_dirs = self._fixture_files()

    async def aclose(self) -> None:
        runtime, self._runtime = self._runtime, None
        try:
            if runtime is not None:
                await runtime.aclose()
        finally:
            if self._temporary is not None:
                self._temporary.cleanup()
                self._temporary = None

    def _agent(self) -> Any:
        return self.runtime.agent_resolver.resolve_agent(None, EVAL_AGENT_ID)

    def _route_definitions(self, agent: Any) -> list[dict[str, Any]]:
        from core.tools.bash import project_bash_tool_definitions
        from core.tools.image import ANALYZE_IMAGE_TOOL_NAME

        definitions = self.runtime.system_prompts.provider_tool_definitions(agent)
        definitions = project_bash_tool_definitions(definitions, nesting_depth=0)
        return [
            dict(definition)
            for definition in definitions
            if definition.get("name") != ANALYZE_IMAGE_TOOL_NAME
        ]

    def _fixture_roots(self) -> list[Path]:
        runtime = self.runtime
        data_dir = Path(runtime.storage.data_dir)
        # The Agent directory holds its private Skill history and archive; the
        # global home keeps both next to it.
        return [
            data_dir / "agents" / EVAL_AGENT_ID,
            runtime.global_skills_dir,
            data_dir / "skill-archive",
        ]

    def _fixture_single_files(self) -> list[Path]:
        return [Path(self.runtime.storage.data_dir) / "skill-history.jsonl"]

    def _fixture_files(self) -> tuple[dict[Path, bytes], set[Path]]:
        files: dict[Path, bytes] = {}
        dirs: set[Path] = set()
        for path in self._fixture_single_files():
            if path.is_file():
                files[path] = path.read_bytes()
        for root in self._fixture_roots():
            if not root.is_dir():
                continue
            dirs.add(root)
            for path in root.rglob("*"):
                if path.is_dir():
                    dirs.add(path)
                elif path.is_file():
                    files[path] = path.read_bytes()
        return files, dirs

    def _reset(self) -> None:
        """Restore the fixture Agent and global Skill state to their post-startup state."""
        for path in self._fixture_single_files():
            if path.is_file() and path not in self._baseline_files:
                path.unlink()
        for root in self._fixture_roots():
            if not root.is_dir():
                continue
            # Deepest first, so a directory is empty once its new entries are gone.
            for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
                if path.is_file() and path not in self._baseline_files:
                    path.unlink()
                elif path.is_dir() and path not in self._baseline_dirs:
                    path.rmdir()
            if root not in self._baseline_dirs:
                root.rmdir()
        for path, data in self._baseline_files.items():
            if not path.is_file() or path.read_bytes() != data:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
        self._invalidate_skills()

    def _invalidate_skills(self) -> None:
        runtime = self.runtime
        runtime.invalidate_agent_skills(None)
        runtime.invalidate_project_skills(None)
        runtime.reload_skills()

    def own_skill_file_exists(self, name: str, file_path: str) -> bool:
        if not name or not file_path:
            return False
        try:
            home: Path = self.runtime.agent_skills_dir(EVAL_AGENT_ID)
            return (home / name / file_path).is_file()
        except OSError, ValueError:
            return False

    def state(self) -> dict[str, Any]:
        """Return the observable learning state: Memory entries and Skill files."""
        runtime = self.runtime
        workspace = Path(self._agent().workspace)
        memory = {
            scope: [entry.content for entry in runtime.memory.list_entries(workspace, scope)]
            for scope in ("user", "agent")
        }
        files: dict[str, str] = {}
        homes = (
            ("own", runtime.agent_skills_dir(EVAL_AGENT_ID)),
            ("global", runtime.global_skills_dir),
        )
        for prefix, home in homes:
            if not home.is_dir():
                continue
            for path in sorted(home.rglob("*")):
                relative = path.relative_to(home)
                # Skill packages hold no dot directories; keep bookkeeping out of scoring.
                if path.is_file() and not any(part.startswith(".") for part in relative.parts):
                    files[f"{prefix}/{relative.as_posix()}"] = path.read_text(
                        encoding="utf-8", errors="replace"
                    )
        return {"memory": memory, "files": files}

    def _seed_skills(self, case: Mapping[str, Any]) -> None:
        """Create the case's Skills with their origin as writer, then pin the pinned ones."""
        from core.skills import HUMAN_WRITER, SkillWriter
        from core.skills.authoring import SkillActor

        runtime = self.runtime
        for skill in case.get("skills", []):
            readonly = bool(skill.get("readonly"))
            origin = "human" if readonly else str(skill.get("origin", "agent"))
            if origin != "human" and origin not in _SEED_RUN_KINDS:
                raise ValueError(f"Unsupported Skill origin in case {case['id']}: {origin}")
            home = (
                runtime.global_skills_dir if readonly else runtime.agent_skills_dir(EVAL_AGENT_ID)
            )
            writer = HUMAN_WRITER
            if origin != "human":
                actor = cast(SkillActor, origin)
                writer = SkillWriter(actor=actor, run_kind=_SEED_RUN_KINDS[origin])
            runtime.skill_authoring.create(home, skill["name"], skill["content"], writer=writer)
            if skill.get("pinned"):
                runtime.skill_authoring.set_pinned(home, skill["name"], True, writer=HUMAN_WRITER)

    def librarian_candidates(self) -> tuple[Any, ...]:
        """Return the Skills a Librarian pass may change, as production lists them.

        The fixture has no Skill use, so every candidate reads as never used.
        """
        from core.automation.librarian import librarian_candidates

        runtime = self.runtime
        return librarian_candidates(
            runtime.skill_authoring,
            runtime.agent_skills_dir(EVAL_AGENT_ID),
            usage={},
        )

    async def prepare(
        self, case: Mapping[str, Any], scope: str, *, attempt_id: str
    ) -> PreparedAttempt:
        """Seed one attempt's state and build its first request like production."""
        from core.agents import LIBRARIAN_TOOLS
        from core.automation.reflection import (
            REFLECTION_RUN_KINDS,
            REFLECTION_TOOL_RESTRICTIONS,
            _review_tool_denial_resolver,
        )
        from core.chat._request_history import _prepare_request_messages
        from core.chat.messages import ChatMessage
        from core.chat.messages import ToolCall as CanonicalToolCall
        from core.projects.resolver import runtime_agent_body
        from core.providers.reasoning import DEFAULT_REASONING_REPLAY_POLICY
        from core.runs import RunKind

        if self.applied is None:
            raise RuntimeError("EvalWorker is not started")
        runtime = self.runtime
        self._reset()
        agent = self._agent()
        workspace = Path(agent.workspace)
        # A Session pins Memory when it starts; a stale pin predates the stored entries.
        pinned_memory = (
            runtime.system_prompts.render_memory_files(agent)
            if case.get("stale_memory_prompt")
            else None
        )
        for memory_scope in ("user", "agent"):
            for entry in case.get("memory", {}).get(memory_scope, []):
                runtime.memory.add_entry(workspace, memory_scope, entry)
        self._seed_skills(case)
        self._invalidate_skills()
        agent = self._agent()
        if pinned_memory is None:
            pinned_memory = runtime.system_prompts.render_memory_files(agent)
        registry = runtime.skills_for(None, EVAL_AGENT_ID)
        definitions = apply_tool_texts(self._route_definitions(agent), self.applied.texts)
        system_prompt = runtime.system_prompts.build_system_prompt(
            agent,
            agent_body=runtime_agent_body(agent),
            soul_context=runtime.system_prompts.render_soul(agent),
            memory_files_context=pinned_memory,
            skill_registry=registry,
            skill_catalog=runtime.system_prompts.render_skill_catalog(agent, registry),
            effective_tool_definitions=definitions,
        )
        candidates: tuple[Any, ...] = ()
        if scope == "learn":
            restriction, denial_resolver = LEARN_DISPATCH_TOOLS, None
            run_kind = "user"
        elif scope == "librarian":
            restriction, denial_resolver = LIBRARIAN_TOOLS, None
            run_kind = RunKind.LIBRARIAN.value
            candidates = self.librarian_candidates()
        else:
            restriction = tuple(REFLECTION_TOOL_RESTRICTIONS[scope])  # type: ignore[index]
            denial_resolver = _review_tool_denial_resolver(scope)  # type: ignore[arg-type]
            run_kind = REFLECTION_RUN_KINDS[scope].value  # type: ignore[index]
        prepared = PreparedAttempt(
            system_prompt=system_prompt,
            definitions=definitions,
            messages=[],
            restriction=restriction,
            denial_resolver=denial_resolver,
            session_id=f"eval-{attempt_id}",
            run_id=f"eval-{attempt_id}-run",
            agent_temperature=getattr(agent, "temperature", None),
            agent_top_p=getattr(agent, "top_p", None),
            activated={},
            run_kind=run_kind,
        )
        model = str(agent.model or "")
        session: list[Any] = []
        calls: dict[str, Mapping[str, Any]] = {}
        for item in case["history"]:
            role = item["role"]
            if role == "user":
                session.append(ChatMessage.user(item["content"]))
            elif role == "assistant":
                tool_calls = list(item.get("tool_calls") or [])
                calls.update({call["id"]: call for call in tool_calls})
                session.append(
                    ChatMessage.assistant(
                        model=model,
                        content=item.get("content"),
                        tool_calls=[
                            CanonicalToolCall(
                                id=call["id"],
                                name=call["name"],
                                arguments=dict(call.get("arguments") or {}),
                            )
                            for call in tool_calls
                        ]
                        or None,
                    )
                )
            elif role == "tool":
                call = calls[item["tool_call_id"]]
                if item.get("dispatch"):
                    # The original Session's own call: every Tool the Agent allows.
                    result = (
                        await self.dispatch(
                            [call], prepared, restriction=None, notes=[], source_call=True
                        )
                    )[0]
                else:
                    result = item["result"]
                session.append(
                    ChatMessage.tool(
                        tool_call_id=call["id"],
                        name=call["name"],
                        content=tool_message_content(result),
                    )
                )
            else:
                raise ValueError(f"Unsupported history role in case {case['id']}: {role}")
        # The review, /learn or Librarian instruction persists as a plain note,
        # rendered as a System Reminder at the end of the request. A Librarian
        # pass starts a new Session, so its case history is empty.
        session.append(
            ChatMessage.note(brief_text(scope, case, self.applied.texts, candidates=candidates))
        )
        prepared.messages = _prepare_request_messages(
            system_prompt=system_prompt,
            agent_model=model,
            session_messages=session,
            replay_policy=DEFAULT_REASONING_REPLAY_POLICY,
            reasoning_scope_model=model,
        ).messages
        return prepared

    async def dispatch(
        self,
        calls: Sequence[Mapping[str, Any]],
        prepared: PreparedAttempt,
        *,
        restriction: Sequence[str] | None,
        denial_resolver: Callable[[str], str | None] | None = None,
        notes: list[str],
        iteration: int = 0,
        source_call: bool = False,
    ) -> list[dict[str, Any]]:
        """Run Tool calls through the production executor and dispatch allowlist.

        ``restriction`` narrows dispatch like a Run's Tool restriction; ``None``
        dispatches with the Agent's full allowlist. A call ``denial_resolver``
        answers fails with that message without running, as Chat answers it
        before dispatch. Tool notes go to ``notes``. Calls carry the attempt's
        Run kind; ``source_call`` marks the source Session's own (user) call.
        """
        from core.chat.tool_dispatch import _dispatch_allowed_tools
        from core.runs import RunKind
        from core.tools import ToolCall, ToolExecutionConfig, ToolExecutor, tool_failure
        from core.tools.availability import agent_tool_settings

        denied: dict[int, dict[str, Any]] = {}
        for index, call in enumerate(calls):
            message = denial_resolver(str(call["name"])) if denial_resolver else None
            if message is not None:
                denied[index] = tool_failure("tool_not_allowed", message)
        runnable = [call for index, call in enumerate(calls) if index not in denied]
        if not runnable:
            return [denied[index] for index in range(len(calls))]
        runtime = self.runtime
        agent = self._agent()
        workspace = Path(agent.workspace)
        config = ToolExecutionConfig(
            agent_id=EVAL_AGENT_ID,
            session_id=prepared.session_id,
            run_id=prepared.run_id,
            run_kind=RunKind.USER if source_call else RunKind(prepared.run_kind),
            workspace=workspace,
            vbot_root=Path(runtime.system_prompts.vbot_root),
            data_root=Path(runtime.storage.data_dir),
            iteration_number=iteration,
            cwd=workspace,
            allowed_tools=_dispatch_allowed_tools(
                agent, runtime.tools, None if restriction is None else list(restriction)
            ),
            allowed_skills=getattr(agent, "allowed_skills", ["*"]),
            tool_settings=agent_tool_settings(getattr(agent, "tools", {})),
            note_hook=notes.append,
            skill_activation_hook=_activation_hook(prepared.activated),
            input_contracts=runtime.tools.contracts_for_provider_definitions(prepared.definitions),
        )
        executed = iter(
            await ToolExecutor(runtime.tools).execute_many(
                [
                    ToolCall(
                        id=str(call["id"]),
                        name=str(call["name"]),
                        arguments=call.get("arguments"),
                    )
                    for call in runnable
                ],
                config,
            )
        )
        return [denied[index] if index in denied else next(executed) for index in range(len(calls))]
