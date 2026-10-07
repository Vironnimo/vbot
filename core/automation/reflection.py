"""Background self-improvement reviews over forked sessions.

The reflection service owns two halves of one capability:

1. The shared review orchestration used by the ``/reflect`` command and the
   background trigger: fork the session (same agent, so the fork stays
   prompt-cache-warm and keeps the pinned skill catalog), run the scope's brief
   as an internal run inside the fork with only that scope's Tools dispatchable
   and a small Tool-iteration limit, and return the fork id plus the run's
   closing summary. The source session is never touched.
2. The cadence policy behind the background trigger: per-session counters in
   the session metadata sidecar (user turns since the last memory review,
   Iterations since the last skill review), incremented at the end of every
   completed visible run and every user-cancelled visible run that completed a
   Model step. When a threshold is reached, one review run fires in the
   background. A successful review consumes the due counts it covered; a failed
   review leaves them due for the next run.

Both halves follow the Agent's effective Tool access: the memory dimension needs
a callable ``memory``, the skill dimension callable ``skill`` and
``skill_manage``. An unavailable dimension is neither counted nor reviewed, and
a requested scope narrows to the dimensions that remain. The built-in Librarian
has no dimension: its Sessions maintain other Agents' Skills and are never
reviewed.

The chat loop notifies this service at run end through the small
``ReflectionNotifier`` protocol it owns; everything with I/O happens in a
background task so run teardown is never delayed, and its blocking Settings
reads, brief reads and metadata writes run on reflection workers off the Event
Loop.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from core.agents import is_librarian
from core.chat.content_blocks import ContentBlock, TextBlock
from core.prompts.briefs import ReflectionScope, reflection_brief
from core.runs import RunKind
from core.sessions import SUBAGENT_SESSION_META_KEY, SessionAddress
from core.tools.availability import MEMORY_TOOL_NAME, SKILL_MANAGE_TOOL_NAME, resolve_tool_access
from core.tools.model_names import model_tool_name
from core.utils.logging import get_logger
from core.utils.workers import BoundedWorkerPool

if TYPE_CHECKING:
    from core.chat import ReplySurface
    from core.runs import Run
    from core.runtime.interfaces import RuntimeServices

# Run-end accounting waits for the Session writer, so it never runs on the loop.
_REFLECTION_WORKERS = BoundedWorkerPool(name="reflection", max_workers=2)

# The restriction is the Reflection Run's dispatch boundary. Listing and loading
# Skills share one stable ``skill`` definition, so Reflection never changes the
# provider-visible Tool set at the fork boundary. A review dimension is available
# only when the effective Agent can call every Tool of its restriction.
REFLECTION_TOOL_RESTRICTION = ("memory", "skill", "skill_manage")
MEMORY_REFLECTION_TOOL_RESTRICTION = (MEMORY_TOOL_NAME,)
SKILL_REFLECTION_TOOL_RESTRICTION = ("skill", SKILL_MANAGE_TOOL_NAME)
REFLECTION_TOOL_RESTRICTIONS: dict[ReflectionScope, tuple[str, ...]] = {
    "memory": MEMORY_REFLECTION_TOOL_RESTRICTION,
    "skill": SKILL_REFLECTION_TOOL_RESTRICTION,
    "combined": REFLECTION_TOOL_RESTRICTION,
}
# Tool result for a review's call outside its scope. ``tool`` is the called Tool
# and ``tools`` the scope's Tools, both as the Model names them.
REVIEW_TOOL_DENIAL_MESSAGE = (
    "Nothing was run: {tool} is not available in this review. "
    "This review can call only {tools}. Do not retry this call."
)
# Session-sidecar key holding the cadence counters. Kept out of forks via the
# always-strip policy in ``core/sessions`` so a fork restarts at zero.
REFLECTION_COUNTERS_META_KEY = "reflection_counters"
TURNS_SINCE_MEMORY_REVIEW_KEY = "turns_since_memory_review"
ITERATIONS_SINCE_SKILL_REVIEW_KEY = "iterations_since_skill_review"
# A manual reset advances this generation so a concurrent background review
# cannot consume activity recorded after that reset.
COUNTER_GENERATION_KEY = "generation"
# Scope-specific Run origins so accessors can filter memory and skill reviews
# independently; the combined scope (manual ``/reflect``) keeps the generic
# reflection kind.
REFLECTION_RUN_KINDS: dict[ReflectionScope, RunKind] = {
    "memory": RunKind.MEMORY_REFLECTION,
    "skill": RunKind.SKILL_REFLECTION,
    "combined": RunKind.REFLECTION,
}
_LOGGER = get_logger("automation.reflection")


@dataclass(frozen=True)
class ReflectionResult:
    """Outcome of one review: the fork it ran in, the run's closing summary, and
    the scope actually reviewed (the requested scope narrowed to callable Tools)."""

    session_id: str
    summary: str
    scope: ReflectionScope


@dataclass(frozen=True)
class _AdvancedCounters:
    """One Session's counters after a Run end, and which reviews are now due."""

    reviews_enabled: bool
    turns: int
    iterations: int
    generation: int
    memory_due: bool
    skill_due: bool


class ReflectionUnavailableError(RuntimeError):
    """Raised before forking when the Agent can review no requested dimension."""


class ReflectionService:
    """Fork-based session reviews that save durable memory/skill updates."""

    def __init__(
        self,
        runtime: RuntimeServices,
        *,
        librarian_running: Callable[[str], bool] = lambda _agent_id: False,
    ) -> None:
        """Review Sessions of ``runtime``'s Agents.

        ``librarian_running(agent_id)`` says whether a Librarian pass curates the
        Agent's Skills; a background review then leaves the Skills to the pass.
        """
        self._runtime = runtime
        self._librarian_running = librarian_running
        self._background_tasks: set[asyncio.Task[None]] = set()
        self._agents_in_review: set[str] = set()
        self._closed = False

    def reviewing(self, agent_id: str) -> bool:
        """Whether a background review of the Agent is starting or running."""
        return agent_id in self._agents_in_review

    # -- background trigger ----------------------------------------------------

    def notify_run_end(self, run: Run, agent: Any, *, internal: bool, outcome: str) -> None:
        """Account a finished run and maybe fire a background review.

        Called by the chat loop at the end of every run with the Run's effective
        Agent. Completed visible Runs and user-cancelled visible Runs with at
        least one completed Model step count. Internal runs (handoff, learn, the
        review run itself), config agents, and Agents that can call neither
        review dimension's Tools are gated out here; sub-agent sessions are gated
        in the task once session metadata is loaded. Only dimensions the Agent
        can review are counted or reset. The review runs in a fork, so the
        session this run belongs to stays free.
        """
        if self._closed or internal or not agent.workspace:
            return
        memory_available, skill_available = self._callable_dimensions(agent)
        if not memory_available and not skill_available:
            return
        # A write through the dimension's own Tool resets that dimension.
        memory_tool_called = memory_available and MEMORY_TOOL_NAME in run.tool_call_names
        skill_manage_called = skill_available and SKILL_MANAGE_TOOL_NAME in run.tool_call_names
        user_cancelled_after_model_step = (
            outcome == "cancelled" and run.cancel_reason == "user" and run.iteration_count > 0
        )
        count_run = outcome == "success" or user_cancelled_after_model_step
        if not count_run and not memory_tool_called and not skill_manage_called:
            return
        task = asyncio.create_task(
            self._account_run_end(
                agent_id=run.agent_id,
                session_id=run.session_id,
                project_id=run.project_id,
                iteration_count=run.iteration_count,
                memory_available=memory_available,
                skill_available=skill_available,
                memory_tool_called=memory_tool_called,
                skill_manage_called=skill_manage_called,
                count_run=count_run,
            ),
            name=f"reflection-accounting:{run.id}",
        )
        self._background_tasks.add(task)
        task.add_done_callback(self._on_background_task_done)

    def _on_background_task_done(self, task: asyncio.Task[None]) -> None:
        self._background_tasks.discard(task)
        if task.cancelled():
            return
        exception = task.exception()
        if exception is not None:
            _LOGGER.warning("Background reflection task failed: %s", exception, exc_info=exception)

    async def aclose(self) -> None:
        """Cancel and drain every automatic reflection orchestration task."""
        self._closed = True
        tasks = tuple(self._background_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._background_tasks.clear()
        self._agents_in_review.clear()

    async def _account_run_end(
        self,
        *,
        agent_id: str,
        session_id: str,
        project_id: str | None,
        iteration_count: int,
        memory_available: bool,
        skill_available: bool,
        memory_tool_called: bool,
        skill_manage_called: bool,
        count_run: bool,
    ) -> None:
        counters = await _REFLECTION_WORKERS.run(
            self._advance_counters,
            SessionAddress(project_id=project_id, agent_id=agent_id, session_id=session_id),
            iteration_count=iteration_count,
            memory_available=memory_available,
            skill_available=skill_available,
            memory_tool_called=memory_tool_called,
            skill_manage_called=skill_manage_called,
            count_run=count_run,
        )
        if counters is None:
            return
        # One review at a time per agent: a due Session while a review is already
        # running keeps its counters and re-checks on its next Run end. So do the
        # Skill counts while a Librarian pass curates the Agent's Skills: both
        # would change the same Skills, each from what it read earlier.
        skill_due = counters.skill_due and not self._librarian_running(agent_id)
        scope = _scope_of(memory=counters.memory_due, skill=skill_due)
        if (
            not counters.reviews_enabled
            or not count_run
            or scope is None
            or agent_id in self._agents_in_review
        ):
            return

        self._agents_in_review.add(agent_id)
        try:
            _LOGGER.debug(
                "Reflection review triggered (agent=%s session=%s scope=%s)",
                agent_id,
                session_id,
                scope,
            )
            result = await self.run_review(
                agent_id,
                session_id,
                project_id=project_id,
                review_scope=scope,
            )
            # The review may have narrowed to the Tools callable when it started;
            # only the dimensions it reviewed consume their counts.
            reviewed_memory, reviewed_skill = _scope_dimensions(result.scope)
            reviewed_turns = counters.turns if reviewed_memory else 0
            reviewed_iterations = counters.iterations if reviewed_skill else 0
            await _REFLECTION_WORKERS.run(
                self._consume_reviewed_counters,
                agent_id,
                session_id,
                project_id=project_id,
                counter_generation=counters.generation,
                reviewed_turns=reviewed_turns,
                reviewed_iterations=reviewed_iterations,
            )
            # The review's closing summary is Model output and never enters the log.
            _LOGGER.info(
                "Reflection review completed (agent=%s session=%s fork=%s scope=%s "
                "turns=%d iterations=%d)",
                agent_id,
                session_id,
                result.session_id,
                result.scope,
                reviewed_turns,
                reviewed_iterations,
            )
        except ReflectionUnavailableError as error:
            # Tool access changed after the Run ended; the counts stay due.
            _LOGGER.debug(
                "Reflection review skipped (agent=%s session=%s): %s",
                agent_id,
                session_id,
                error,
            )
        except Exception:
            _LOGGER.warning(
                "Reflection review failed (agent=%s session=%s)",
                agent_id,
                session_id,
                exc_info=True,
            )
        finally:
            self._agents_in_review.discard(agent_id)

    def _advance_counters(
        self,
        address: SessionAddress,
        *,
        iteration_count: int,
        memory_available: bool,
        skill_available: bool,
        memory_tool_called: bool,
        skill_manage_called: bool,
        count_run: bool,
    ) -> _AdvancedCounters | None:
        """Count one finished Run; ``None`` when the Session keeps no counters.

        An unavailable dimension keeps its counter unchanged and is never due.
        """
        settings = self._runtime.storage.load_reflection_settings()
        if not settings["enabled"] and not memory_tool_called and not skill_manage_called:
            return None
        state: dict[str, Any] = {"skip": False}

        def update(metadata: dict[str, Any]) -> None:
            if metadata.get(SUBAGENT_SESSION_META_KEY):
                state["skip"] = True
                return
            raw_counters = metadata.get(REFLECTION_COUNTERS_META_KEY)
            counters = raw_counters if isinstance(raw_counters, dict) else {}
            turns = (
                0
                if memory_tool_called
                else _non_negative_int(counters.get(TURNS_SINCE_MEMORY_REVIEW_KEY))
                + (1 if count_run and memory_available else 0)
            )
            iterations = (
                0
                if skill_manage_called
                else _non_negative_int(counters.get(ITERATIONS_SINCE_SKILL_REVIEW_KEY))
                + (max(iteration_count, 0) if count_run and skill_available else 0)
            )
            counter_generation = _non_negative_int(counters.get(COUNTER_GENERATION_KEY))
            if memory_tool_called or skill_manage_called:
                # A reset changes the baseline just like manual /reflect. An
                # older in-flight review must not consume later activity.
                counter_generation += 1
            memory_due = memory_available and turns >= settings["memory_turn_interval"]
            skill_due = skill_available and iterations >= settings["skill_model_step_interval"]
            state.update(
                turns=turns,
                iterations=iterations,
                counter_generation=counter_generation,
                memory_due=memory_due,
                skill_due=skill_due,
            )
            metadata[REFLECTION_COUNTERS_META_KEY] = {
                TURNS_SINCE_MEMORY_REVIEW_KEY: turns,
                ITERATIONS_SINCE_SKILL_REVIEW_KEY: iterations,
                COUNTER_GENERATION_KEY: counter_generation,
            }

        self._runtime.chat_sessions.mutate_metadata(address, update)
        if state["skip"]:
            return None
        return _AdvancedCounters(
            reviews_enabled=bool(settings["enabled"]),
            turns=int(state["turns"]),
            iterations=int(state["iterations"]),
            generation=int(state["counter_generation"]),
            memory_due=bool(state["memory_due"]),
            skill_due=bool(state["skill_due"]),
        )

    def _consume_reviewed_counters(
        self,
        agent_id: str,
        session_id: str,
        *,
        project_id: str | None,
        counter_generation: int,
        reviewed_turns: int,
        reviewed_iterations: int,
    ) -> None:
        """Consume only counts covered by a successful background review."""
        sessions = self._runtime.chat_sessions
        address = SessionAddress(project_id=project_id, agent_id=agent_id, session_id=session_id)

        def update(metadata: dict[str, Any]) -> None:
            raw_counters = metadata.get(REFLECTION_COUNTERS_META_KEY)
            counters = raw_counters if isinstance(raw_counters, dict) else {}
            current_generation = _non_negative_int(counters.get(COUNTER_GENERATION_KEY))
            if current_generation != counter_generation:
                return
            current_turns = _non_negative_int(counters.get(TURNS_SINCE_MEMORY_REVIEW_KEY))
            current_iterations = _non_negative_int(counters.get(ITERATIONS_SINCE_SKILL_REVIEW_KEY))
            metadata[REFLECTION_COUNTERS_META_KEY] = {
                TURNS_SINCE_MEMORY_REVIEW_KEY: max(current_turns - reviewed_turns, 0),
                ITERATIONS_SINCE_SKILL_REVIEW_KEY: max(current_iterations - reviewed_iterations, 0),
                COUNTER_GENERATION_KEY: current_generation,
            }

        sessions.mutate_metadata(address, update)

    # -- shared review orchestration --------------------------------------------

    async def run_review(
        self,
        agent_id: str,
        session_id: str,
        *,
        project_id: str | None = None,
        review_scope: ReflectionScope = "combined",
        focus: str | None = None,
        on_fork_created: Callable[[str], None] | None = None,
        reply_surface: ReplySurface | None = None,
    ) -> ReflectionResult:
        """Fork the session and run the reflection brief inside the fork.

        The fork stays on the same agent (prompt-cache-warm, pinned catalog
        kept) and is titled with the agent's display name so it is
        recognizable in the session list.
        ``review_scope`` requests the memory-only, skill-only, or combined
        review; it narrows to the dimensions the effective Agent can call, which
        select the brief, the dispatch boundary, and the Run kind. The result
        names the scope actually reviewed. ``ReflectionUnavailableError`` is
        raised before forking when no requested dimension remains. ``focus`` is
        the user's focus, appended to the brief; ``on_fork_created`` fires with
        the fork id before the review run starts, so an accessor can surface the
        fork while the review runs.
        """
        agent = await self._runtime.agent_resolver.resolve_agent_async(project_id, agent_id)
        if not agent.workspace:
            raise ReflectionUnavailableError("Reflection requires an identity Agent")
        scope = self.available_review_scope(agent, review_scope)
        if scope is None:
            raise ReflectionUnavailableError(
                f"Reflection scope {review_scope} needs Tools this Agent cannot call"
            )
        instruction = await _REFLECTION_WORKERS.run(
            reflection_brief, self._runtime.storage, scope, focus=focus
        )
        sessions = self._runtime.chat_sessions
        source_address = SessionAddress(
            project_id=project_id, agent_id=agent_id, session_id=session_id
        )
        source_title = str(
            await sessions.metadata_value_async(source_address, "title") or ""
        ).strip()
        run_kind = REFLECTION_RUN_KINDS[scope]
        # The fork is titled with the agent's display name so review forks stay
        # distinguishable in a session list that spans agents; the run-kind
        # marker on the row already says "reflection" (a fork would otherwise
        # inherit the source session's title). Both commit with the fork.
        fork = await sessions.fork(
            source_address,
            target_project_id=project_id,
            title=f"{agent.name}: {source_title}" if source_title else agent.name,
            run_kind=run_kind,
        )
        if on_fork_created is not None:
            on_fork_created(fork.id)
        # The fork is fresh and never busy — start directly, no queueing needed.
        # Streaming loop so an accessor watching the fork sees the live timeline.
        review_run = await self._runtime.chat_loop.start_run(
            agent_id,
            instruction,
            session_id=fork.id,
            internal=True,
            reply_surface=reply_surface,
            project_id=project_id,
            tool_restriction=REFLECTION_TOOL_RESTRICTIONS[scope],
            tool_denial_resolver=_review_tool_denial_resolver(scope),
            run_kind=run_kind,
            contributes_to_agent_activity=False,
            source_session_id=session_id,
        )
        final_message = await review_run.wait()
        return ReflectionResult(
            session_id=fork.id, summary=_final_text(final_message.content), scope=scope
        )

    def available_review_scope(
        self, agent: Any, requested: ReflectionScope = "combined"
    ) -> ReflectionScope | None:
        """Narrow ``requested`` to the dimensions ``agent`` can review.

        ``agent`` is the effective Agent, so Project ceilings and Tool Access
        Policy denials apply. Returns ``None`` when no requested dimension is
        callable. Resolves policy in memory, so it is safe on the Event Loop.
        """
        memory_available, skill_available = self._callable_dimensions(agent)
        wants_memory, wants_skill = _scope_dimensions(requested)
        return _scope_of(
            memory=wants_memory and memory_available, skill=wants_skill and skill_available
        )

    def _callable_dimensions(self, agent: Any) -> tuple[bool, bool]:
        """Whether ``agent`` can call every Tool of the memory and skill dimensions."""
        return callable_review_dimensions(agent, self._runtime.tools.list_tools())

    def reset_counters(
        self,
        agent_id: str,
        session_id: str,
        project_id: str | None = None,
        review_scope: ReflectionScope = "combined",
    ) -> None:
        """Zero the counters of the dimensions a manual ``/reflect`` reviewed."""
        sessions = self._runtime.chat_sessions
        address = SessionAddress(project_id=project_id, agent_id=agent_id, session_id=session_id)
        reviewed_memory, reviewed_skill = _scope_dimensions(review_scope)

        def update(metadata: dict[str, Any]) -> None:
            raw_counters = metadata.get(REFLECTION_COUNTERS_META_KEY)
            counters = raw_counters if isinstance(raw_counters, dict) else {}
            turns = _non_negative_int(counters.get(TURNS_SINCE_MEMORY_REVIEW_KEY))
            iterations = _non_negative_int(counters.get(ITERATIONS_SINCE_SKILL_REVIEW_KEY))
            metadata[REFLECTION_COUNTERS_META_KEY] = {
                TURNS_SINCE_MEMORY_REVIEW_KEY: 0 if reviewed_memory else turns,
                ITERATIONS_SINCE_SKILL_REVIEW_KEY: 0 if reviewed_skill else iterations,
                COUNTER_GENERATION_KEY: _non_negative_int(counters.get(COUNTER_GENERATION_KEY)) + 1,
            }

        sessions.mutate_metadata(address, update)


def callable_review_dimensions(agent: Any, tools: Sequence[Any]) -> tuple[bool, bool]:
    """Whether ``agent`` can call every Tool of the memory and of the skill dimension.

    ``agent`` is the effective Agent, so Project ceilings and Tool Access Policy
    denials apply; ``tools`` are the registered Tools. The built-in Librarian has
    neither. Resolves policy in memory, so it is safe on the Event Loop.
    """
    if is_librarian(agent):
        return False, False
    callable_tools = set(
        resolve_tool_access(
            agent.tool_access,
            tools,
            agent.memory_prompt_mode,
            workspace=agent.workspace or "",
        ).allowed_tools
    )
    return (
        callable_tools.issuperset(MEMORY_REFLECTION_TOOL_RESTRICTION),
        callable_tools.issuperset(SKILL_REFLECTION_TOOL_RESTRICTION),
    )


def tool_denial_resolver(allowed: Sequence[str], message: str) -> Callable[[str], str | None]:
    """Deny every Tool outside ``allowed`` with ``message`` naming the allowed Tools.

    ``message`` has the fields ``tool`` (the called Tool) and ``tools`` (the
    allowed ones), both as the Model names them.
    """
    tools = _tool_list(allowed)

    def resolve(tool_name: str) -> str | None:
        if tool_name in allowed:
            return None
        return message.format(tool=f"`{model_tool_name(tool_name)}`", tools=tools)

    return resolve


def _scope_of(*, memory: bool, skill: bool) -> ReflectionScope | None:
    """Return the review scope covering exactly the given dimensions."""
    if memory and skill:
        return "combined"
    if memory:
        return "memory"
    if skill:
        return "skill"
    return None


def _scope_dimensions(scope: ReflectionScope) -> tuple[bool, bool]:
    """Return whether ``scope`` reviews the memory and the skill dimension."""
    return scope != "skill", scope != "memory"


def _review_tool_denial_resolver(scope: ReflectionScope) -> Callable[[str], str | None]:
    """Deny every Tool outside the scope with a result naming the scope's Tools."""
    return tool_denial_resolver(REFLECTION_TOOL_RESTRICTIONS[scope], REVIEW_TOOL_DENIAL_MESSAGE)


def _tool_list(names: Sequence[str]) -> str:
    """Join backticked Model Tool names: one, ``a and b``, or ``a, b, and c``."""
    quoted = [f"`{model_tool_name(name)}`" for name in names]
    if len(quoted) <= 2:
        return " and ".join(quoted)
    return f"{', '.join(quoted[:-1])}, and {quoted[-1]}"


def _final_text(content: str | list[ContentBlock] | None) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return "\n".join(block.text for block in content if isinstance(block, TextBlock)).strip()
    return ""


def _non_negative_int(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        return 0
    return max(value, 0)
