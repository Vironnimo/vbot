"""Background self-improvement reviews over forked sessions.

The reflection service owns two halves of one capability:

1. The shared review orchestration used by the ``/reflect`` command and the
   background trigger: fork the session (same agent, so the fork stays
   prompt-cache-warm and keeps the pinned skill catalog), run the reflection
   brief as an internal run inside the fork with only the memory/skill tools
   dispatchable, and return the fork id plus the run's closing summary. The
   source session is never touched.
2. The cadence policy behind the background trigger: per-session counters in
   the session metadata sidecar (user turns since the last memory review,
   Iterations since the last skill review), incremented at the end of every
   completed visible run and every user-cancelled visible run that completed a
   Model step. When a threshold is reached, one review run fires in the
   background. A successful review consumes the due counts it covered; a failed
   review leaves them due for the next run.

The chat loop notifies this service at run end through the small
``ReflectionNotifier`` protocol it owns; everything with I/O happens in a
background task so run teardown is never delayed.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from core.chat.content_blocks import ContentBlock, TextBlock
from core.runs import RunKind
from core.sessions import SESSION_FORK_ALWAYS_STRIP_META_KEYS
from core.subagents.subagents import SUBAGENT_SESSION_METADATA_FLAG
from core.tools.availability import MEMORY_TOOL_NAME, memory_tool_enabled
from core.tools.skill import SKILL_LIST_TOOL_NAME
from core.utils.logging import get_logger

if TYPE_CHECKING:
    from core.chat import ReplySurface
    from core.runs import Run
    from core.runtime.interfaces import RuntimeServices

ReflectionScope = Literal["memory", "skill", "combined"]

REFLECT_FRAGMENT_NAMES: dict[ReflectionScope, str] = {
    "memory": "reflect-memory.md",
    "skill": "reflect-skill.md",
    "combined": "reflect.md",
}
# The restriction is the Reflection Run's dispatch boundary. ``skill_list`` is
# already part of every Session whose Agent can use ``skill``, so Reflection
# never changes the provider-visible Tool set at the fork boundary.
REFLECTION_TOOL_RESTRICTION = ("memory", "skill", SKILL_LIST_TOOL_NAME, "skill_manage")
MEMORY_REFLECTION_TOOL_RESTRICTION = (MEMORY_TOOL_NAME,)
SKILL_REFLECTION_TOOL_RESTRICTION = ("skill", SKILL_LIST_TOOL_NAME, "skill_manage")
REFLECTION_TOOL_RESTRICTIONS: dict[ReflectionScope, tuple[str, ...]] = {
    "memory": MEMORY_REFLECTION_TOOL_RESTRICTION,
    "skill": SKILL_REFLECTION_TOOL_RESTRICTION,
    "combined": REFLECTION_TOOL_RESTRICTION,
}
# Session-sidecar key holding the cadence counters. Kept out of forks via the
# always-strip policy in ``core/sessions`` so a fork restarts at zero.
REFLECTION_COUNTERS_META_KEY = "reflection_counters"
TURNS_SINCE_MEMORY_REVIEW_KEY = "turns_since_memory_review"
ITERATIONS_SINCE_SKILL_REVIEW_KEY = "iterations_since_skill_review"
# A manual reset advances this generation so a concurrent background review
# cannot consume activity recorded after that reset.
COUNTER_GENERATION_KEY = "generation"
# Display title stamped onto review forks so they are recognizable in the
# session list (a fork would otherwise inherit the source session's title).
REFLECTION_FORK_TITLE = "Reflection"
_SUMMARY_LOG_LIMIT = 200

_LOGGER = get_logger("automation.reflection")


@dataclass(frozen=True)
class ReflectionResult:
    """Outcome of one review: the fork it ran in and the run's closing summary."""

    session_id: str
    summary: str


class ReflectionUnavailableError(RuntimeError):
    """Raised when a review is requested for an Agent without active Memory."""


class ReflectionService:
    """Fork-based session reviews that save durable memory/skill updates."""

    def __init__(self, runtime: RuntimeServices) -> None:
        self._runtime = runtime
        self._background_tasks: set[asyncio.Task[None]] = set()
        self._agents_in_review: set[str] = set()
        self._closed = False

    # -- background trigger ----------------------------------------------------

    def notify_run_end(self, run: Run, agent: Any, *, internal: bool, outcome: str) -> None:
        """Account a finished run and maybe fire a background review.

        Called by the chat loop at the end of every run. Completed visible Runs
        and user-cancelled visible Runs with at least one completed Model step
        count. Internal runs (handoff, learn, the review run itself) and config
        agents are gated out here; sub-agent sessions are gated in the task once
        session metadata is loaded. The review runs in a fork, so the session
        this run belongs to stays free.
        """
        if (
            self._closed
            or internal
            or not agent.workspace
            or not memory_tool_enabled(agent.memory_prompt_mode)
        ):
            return
        memory_tool_called = MEMORY_TOOL_NAME in run.tool_call_names
        user_cancelled_after_model_step = (
            outcome == "cancelled" and run.cancel_reason == "user" and run.iteration_count > 0
        )
        count_run = outcome == "success" or user_cancelled_after_model_step
        if not count_run and not memory_tool_called:
            return
        task = asyncio.create_task(
            self._account_run_end(
                agent_id=run.agent_id,
                session_id=run.session_id,
                project_id=run.project_id,
                iteration_count=run.iteration_count,
                memory_tool_called=memory_tool_called,
                count_run=count_run,
            )
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
        memory_tool_called: bool,
        count_run: bool,
    ) -> None:
        settings = self._runtime.storage.load_reflection_settings()
        if not settings["enabled"] and not memory_tool_called:
            return
        sessions = self._runtime.chat_sessions
        metadata = sessions.get_metadata(agent_id, session_id, project_id)
        if metadata.get(SUBAGENT_SESSION_METADATA_FLAG):
            return

        raw_counters = metadata.get(REFLECTION_COUNTERS_META_KEY)
        counters = raw_counters if isinstance(raw_counters, dict) else {}
        turns = (
            0
            if memory_tool_called
            else _non_negative_int(counters.get(TURNS_SINCE_MEMORY_REVIEW_KEY))
            + (1 if count_run else 0)
        )
        iterations = _non_negative_int(counters.get(ITERATIONS_SINCE_SKILL_REVIEW_KEY)) + (
            max(iteration_count, 0) if count_run else 0
        )
        counter_generation = _non_negative_int(counters.get(COUNTER_GENERATION_KEY))
        memory_due = turns >= settings["memory_turn_interval"]
        skill_due = iterations >= settings["skill_model_step_interval"]
        # One review at a time per agent: a due session while a review is already
        # running keeps its counters and re-checks on its next run end.
        should_review = (memory_due or skill_due) and agent_id not in self._agents_in_review
        metadata[REFLECTION_COUNTERS_META_KEY] = {
            TURNS_SINCE_MEMORY_REVIEW_KEY: turns,
            ITERATIONS_SINCE_SKILL_REVIEW_KEY: iterations,
            COUNTER_GENERATION_KEY: counter_generation,
        }
        sessions.set_metadata(agent_id, session_id, metadata, project_id)
        if not settings["enabled"] or not count_run or not should_review:
            return

        due = "+".join(
            name for name, is_due in (("memory", memory_due), ("skill", skill_due)) if is_due
        )
        self._agents_in_review.add(agent_id)
        try:
            _LOGGER.info(
                "Reflection review triggered (agent=%s session=%s due=%s)",
                agent_id,
                session_id,
                due,
            )
            result = await self.run_review(
                agent_id,
                session_id,
                project_id=project_id,
                review_scope=_review_scope(memory_due, skill_due),
            )
            self._consume_reviewed_counters(
                agent_id,
                session_id,
                project_id=project_id,
                counter_generation=counter_generation,
                reviewed_turns=turns if memory_due else 0,
                reviewed_iterations=iterations if skill_due else 0,
            )
            _LOGGER.info(
                "Reflection review completed (agent=%s fork=%s): %s",
                agent_id,
                result.session_id,
                _log_excerpt(result.summary) or "no summary",
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
        metadata = sessions.get_metadata(agent_id, session_id, project_id)
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
        sessions.set_metadata(agent_id, session_id, metadata, project_id)

    # -- shared review orchestration --------------------------------------------

    async def run_review(
        self,
        agent_id: str,
        session_id: str,
        *,
        project_id: str | None = None,
        review_scope: ReflectionScope = "combined",
        extra_instruction: str | None = None,
        on_fork_created: Callable[[str], None] | None = None,
        reply_surface: ReplySurface | None = None,
    ) -> ReflectionResult:
        """Fork the session and run the reflection brief inside the fork.

        The fork stays on the same agent (prompt-cache-warm, pinned catalog
        kept) and is titled so it is recognizable in the session list.
        ``review_scope`` selects the memory-only, skill-only, or combined brief
        and dispatch boundary. ``extra_instruction`` is appended to that brief;
        ``on_fork_created`` fires with the fork id before the review run
        starts, so an accessor can surface the fork while the review runs.
        """
        agent = self._runtime.agent_resolver.resolve_agent(project_id, agent_id)
        if not agent.workspace or not memory_tool_enabled(agent.memory_prompt_mode):
            raise ReflectionUnavailableError(
                "Reflection requires an identity Agent with the memory Tool active"
            )
        sessions = self._runtime.chat_sessions
        source_title = str(
            sessions.get_metadata(agent_id, session_id, project_id).get("title") or ""
        ).strip()
        fork = await sessions.fork(
            agent_id,
            session_id,
            source_project_id=project_id,
            target_project_id=project_id,
            strip_meta_keys=SESSION_FORK_ALWAYS_STRIP_META_KEYS,
        )
        title = (
            f"{REFLECTION_FORK_TITLE}: {source_title}" if source_title else REFLECTION_FORK_TITLE
        )
        sessions.set_title(agent_id, fork.id, title, project_id)
        sessions.record_run_kind(agent_id, fork.id, RunKind.REFLECTION, project_id)
        if on_fork_created is not None:
            on_fork_created(fork.id)
        # The fork is fresh and never busy — start directly, no queueing needed.
        # Streaming loop so an accessor watching the fork sees the live timeline.
        review_run = await self._runtime.streaming_chat_loop.start_run(
            agent_id,
            self._build_instruction(review_scope, extra_instruction),
            session_id=fork.id,
            internal=True,
            reply_surface=reply_surface,
            project_id=project_id,
            tool_restriction=REFLECTION_TOOL_RESTRICTIONS[review_scope],
            run_kind=RunKind.REFLECTION,
            contributes_to_agent_activity=False,
        )
        final_message = await review_run.wait()
        return ReflectionResult(session_id=fork.id, summary=_final_text(final_message.content))

    def reset_counters(self, agent_id: str, session_id: str, project_id: str | None = None) -> None:
        """Zero both cadence counters (a manual ``/reflect`` reviewed everything)."""
        sessions = self._runtime.chat_sessions
        metadata = sessions.get_metadata(agent_id, session_id, project_id)
        raw_counters = metadata.get(REFLECTION_COUNTERS_META_KEY)
        counters = raw_counters if isinstance(raw_counters, dict) else {}
        metadata[REFLECTION_COUNTERS_META_KEY] = {
            TURNS_SINCE_MEMORY_REVIEW_KEY: 0,
            ITERATIONS_SINCE_SKILL_REVIEW_KEY: 0,
            COUNTER_GENERATION_KEY: _non_negative_int(counters.get(COUNTER_GENERATION_KEY)) + 1,
        }
        sessions.set_metadata(agent_id, session_id, metadata, project_id)

    def _build_instruction(
        self, review_scope: ReflectionScope, extra_instruction: str | None
    ) -> str:
        base = self._runtime.storage.read_prompt_fragment(
            REFLECT_FRAGMENT_NAMES[review_scope]
        ).strip()
        extra = (extra_instruction or "").strip()
        if not extra:
            return base
        return f"{base}\n\n{extra}"


def _review_scope(memory_due: bool, skill_due: bool) -> ReflectionScope:
    """Select the complete work brief for the dimensions due now."""
    if memory_due and skill_due:
        return "combined"
    if memory_due:
        return "memory"
    return "skill"


def _final_text(content: str | list[ContentBlock] | None) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return "\n".join(block.text for block in content if isinstance(block, TextBlock)).strip()
    return ""


def _log_excerpt(summary: str) -> str:
    collapsed = " ".join(summary.split())
    if len(collapsed) <= _SUMMARY_LOG_LIMIT:
        return collapsed
    return collapsed[: _SUMMARY_LOG_LIMIT - 1] + "…"


def _non_negative_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return max(value, 0)
