"""Dependency-ordered Runtime shutdown shared by ``stop()``, ``aclose()`` and failed starts.

Every step runs even when an earlier one fails: a failed step is logged with
its name and traceback while logging is still open, and shutdown continues.
Service references are cleared and logging is closed in every case. Only then
is a single failure re-raised unchanged, or several as one ``ExceptionGroup``.
Cancellation and other ``BaseException``s are not collected: they end the
remaining steps and propagate once references are cleared and logging closed.
A failed start runs the same synchronous steps for whatever it had built, but
only logs their failures: the startup error is the one its caller re-raises.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING

from core.debug import drain_debug_traces
from core.utils.logging import get_logger

if TYPE_CHECKING:
    from core.runtime.runtime import Runtime

_LOGGER = get_logger("core")


@dataclass(frozen=True, slots=True)
class _Step:
    """One named shutdown step; an operation is ``None`` where that variant skips it."""

    name: str
    stop: Callable[[], object] | None
    aclose: Callable[[], object] | None


def run_shutdown(runtime: Runtime) -> None:
    """Run every synchronous shutdown step, release the Runtime, then report failures."""
    runtime._log_shutdown()
    _raise_failures(_run_synchronous_steps(runtime))


def clean_up_failed_startup(runtime: Runtime) -> None:
    """Release what a failed start built through the synchronous shutdown steps.

    Step failures are logged with their step but not raised, so the caller
    re-raises the startup error unchanged.
    """
    _run_synchronous_steps(runtime)


def _run_synchronous_steps(runtime: Runtime) -> list[tuple[str, Exception]]:
    # Synchronous shutdown cannot drain admitted Extension work, so it withdraws
    # readiness before its first step.
    runtime._started = False
    failures: list[tuple[str, Exception]] = []
    try:
        for step in _steps(runtime):
            if step.stop is None:
                continue
            try:
                step.stop()
            except Exception as error:
                _record_failure(failures, step.name, error)
    finally:
        _release(runtime, failures)
    return failures


async def run_shutdown_async(runtime: Runtime) -> None:
    """Run every asynchronous shutdown step, release the Runtime, then report failures."""
    runtime._log_shutdown()
    failures: list[tuple[str, Exception]] = []
    try:
        for step in _steps(runtime):
            if step.aclose is None:
                continue
            try:
                result = step.aclose()
                if inspect.isawaitable(result):
                    await result
            except Exception as error:
                _record_failure(failures, step.name, error)
    finally:
        _release(runtime, failures)
    _raise_failures(failures)


def _steps(runtime: Runtime) -> Iterator[_Step]:
    """Yield every shutdown step in dependency order, each just before it runs.

    Extensions shut down while the services their handlers call are live.
    Producers stop before the work they feed drains, and active Runs drain
    before the Decision, Speech and Provider services they call close. Tracked
    processes and terminals end before the temporary files they may use are
    swept, and the databases close last. Draining admitted Extension work, Runs,
    Sub-Agent activity files and handed-off Debug traces needs the Event Loop,
    so those steps have no synchronous operation.
    """
    extensions = runtime._extensions
    if runtime._extension_runtime is not None:
        # An admitted reload still needs the live Runtime refresh callbacks:
        # drain it and close Extension admission before withdrawing readiness.
        yield _Step(
            "extensions",
            None if extensions is None else extensions.fire_shutdown_blocking,
            runtime._extension_runtime.aclose,
        )
    elif extensions is not None:
        yield _Step("extensions", extensions.fire_shutdown_blocking, extensions.fire_shutdown)
    yield _Step(
        "extension_databases",
        runtime._close_extension_databases,
        runtime._close_extension_databases,
    )
    yield _Step("readiness", None, partial(_withdraw_readiness, runtime))

    if (channels := runtime._channel_service) is not None:
        yield _Step("channels", channels.stop, channels.aclose)
    if (cron := runtime._cron_service) is not None:
        yield _Step("cron", cron.stop, cron.aclose)
    if (calendar := runtime._calendar_service) is not None:
        yield _Step("calendar_actions", calendar.actions.stop, calendar.actions.aclose)
    if (bootstrap := runtime._bootstrap_service) is not None:
        yield _Step("bootstrap", bootstrap.stop, bootstrap.aclose)
    if (archive := runtime._archive) is not None:
        # A purge in progress ends before its next Session; the next start continues it.
        yield _Step("archive_retention", archive.stop, archive.aclose)
    if (triggers := runtime._trigger_service) is not None:
        yield _Step("triggers", None, triggers.aclose)
    if (reflection := runtime._reflection_service) is not None:
        yield _Step("reflection", None, reflection.aclose)
    if (librarian := runtime._librarian_service) is not None:
        # A pass in progress stops; its consolidation Run ends with the Runs below.
        yield _Step("librarian", librarian.stop, librarian.aclose)
    if (titles := runtime._session_title_service) is not None:
        yield _Step("session_titles", None, titles.aclose)
    if (runs := runtime._chat_run_manager) is not None:
        yield _Step("chat_runs", None, runs.aclose)
    if (subagents := runtime._subagent_coordinator) is not None:
        # Every Run has ended; each Sub-Agent activity file records its outcome.
        yield _Step("subagent_activity", None, subagents.drain_activity)

    if (decisions := runtime._decisions) is not None:
        yield _Step("decisions", decisions.close, decisions.aclose)
    if (speech := runtime._speech) is not None:
        yield _Step("speech", speech.close, speech.aclose)
    if (provider_usage := runtime._provider_usage) is not None:
        yield _Step("provider_usage", provider_usage.close, provider_usage.aclose)
    # Every Provider call has ended; persist the Debug traces they handed off.
    yield _Step("debug_traces", None, drain_debug_traces)
    if (provider_runtime := runtime._provider_runtime) is not None:
        # Every Provider call has ended; write the wire facts they taught.
        close_observations = provider_runtime.close_wire_observations
        yield _Step(
            "wire_observations",
            close_observations,
            partial(asyncio.to_thread, close_observations),
        )
    if (performance := runtime._performance) is not None:
        yield _Step("performance", performance.stop, performance.aclose)
    if (processes := runtime._process_manager) is not None:
        yield _Step("processes", processes.stop, processes.aclose)
    if (terminals := runtime._terminal_manager) is not None:
        yield _Step("terminals", terminals.stop, terminals.aclose)
    if (keep_awake := runtime._keep_awake) is not None:
        yield _Step("keep_awake", keep_awake.close, keep_awake.close)
    if (storage := runtime._storage) is not None:
        temporary_files = storage.temporary_files
        yield _Step("temporary_files", temporary_files.stop, temporary_files.aclose)

    if channels is not None:
        yield _Step("channel_state", channels.close, channels.close)
    if (recall := runtime._recall) is not None:
        yield _Step("recall", recall.close, recall.aclose)
    if (embeddings := runtime._embeddings) is not None:
        # After Recall: its indexer may still be waiting for an embedding.
        yield _Step("embeddings", embeddings.close, embeddings.aclose)
    if (statistics := runtime._statistics_index) is not None:
        # A running Statistics read holds the index lock; aclose waits on its pool.
        yield _Step("statistics_index", statistics.close, statistics.aclose)
    if (usage := runtime._usage_recorder) is not None:
        yield _Step("model_usage", usage.close, usage.aclose)
    if (sessions := runtime._chat_sessions) is not None:
        yield _Step("sessions", sessions.close, sessions.close)


def _withdraw_readiness(runtime: Runtime) -> None:
    runtime._started = False


def _record_failure(failures: list[tuple[str, Exception]], step: str, error: Exception) -> None:
    _LOGGER.error("Runtime shutdown step failed (step=%s)", step, exc_info=error)
    failures.append((step, error))


def _release(runtime: Runtime, failures: list[tuple[str, Exception]]) -> None:
    """Clear service references and close logging, also after cancellation."""
    runtime._clear_service_references()
    try:
        runtime._close_log_manager()
    except Exception as error:
        _record_failure(failures, "logging", error)


def _raise_failures(failures: Sequence[tuple[str, Exception]]) -> None:
    if len(failures) == 1:
        raise failures[0][1]
    if failures:
        steps = ",".join(step for step, _ in failures)
        raise ExceptionGroup(
            f"Runtime shutdown failed (steps={steps})", [error for _, error in failures]
        )
