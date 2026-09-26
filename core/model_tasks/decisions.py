"""Decision execution and the experiment lifecycle, independent of Agents."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from time import monotonic
from typing import Any, TypeVar

import httpx

from core.database import Database
from core.model_tasks.decision_actions import run_command, validate_control
from core.model_tasks.decision_providers import ProviderDecisionClient
from core.model_tasks.decision_store import DecisionStore
from core.model_tasks.decision_types import DecisionError, text, validate_input
from core.model_tasks.model_tasks import TaskModelService, parse_task_model_target_id
from core.model_tasks.task_execution import TaskUsage, TaskUsageContext
from core.providers.errors import NetworkError, ProviderError, ProviderOutcomeUnknownError
from core.providers.task_client import TaskClientRuntime
from core.usage import UsageRecorder
from core.utils.logging import get_logger
from core.utils.tls import shared_ssl_context

_LOGGER = get_logger("decisions")
_Result = TypeVar("_Result")


class DecisionService:
    """One executor for Tools, experiments, and future internal consumers."""

    def __init__(
        self,
        model_tasks: TaskModelService,
        runtime: TaskClientRuntime,
        store_path: Path,
        *,
        usage_recorder: UsageRecorder | None = None,
    ) -> None:
        self._model_tasks = model_tasks
        self._runtime = runtime
        self._usage_recorder = usage_recorder
        self._store = DecisionStore(store_path)
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._starts: set[asyncio.Task[dict[str, Any]]] = set()
        self._admission = asyncio.Lock()
        self._closed = False

    @property
    def database(self) -> Database:
        """The canonical ``decisions.db`` handle, for data snapshots and health."""
        return self._store.database

    async def _run(
        self, function: Callable[..., _Result], *arguments: Any, **keywords: Any
    ) -> _Result:
        """Run blocking store work on the ``decisions.db`` worker pool.

        After close it raises :class:`~core.database.DatabaseUnavailableError`.
        """
        return await self._store.database.run_async(function, *arguments, **keywords)

    def available(self) -> bool:
        if self._closed or not self._model_tasks.binding_is_usable("decision"):
            return False
        ref = parse_task_model_target_id(self._model_tasks.binding_for("decision").target)
        return ref.provider_id == "openrouter"

    def _target(self) -> str:
        if not self.available():
            raise DecisionError(
                "Configure an available Decision model in Settings > Specialized Models.",
                code="not_configured",
            )
        return self._model_tasks.binding_for("decision").target

    async def evaluate(
        self, state: Any, questions: Any, *, usage_context: TaskUsageContext | None = None
    ) -> dict[str, Any]:
        state, questions = validate_input(state, questions)
        return await self._evaluate(self._target(), state, questions, usage_context=usage_context)

    async def _evaluate(
        self,
        target: str,
        state: Any,
        questions: list[dict[str, Any]],
        *,
        http_client: httpx.AsyncClient | None = None,
        usage_context: TaskUsageContext | None = None,
    ) -> dict[str, Any]:
        started = monotonic()
        ref = parse_task_model_target_id(target)
        try:
            client = ProviderDecisionClient.from_runtime(
                self._runtime,
                ref,
                usage_observer=TaskUsage(
                    self._usage_recorder, "decision", ref, context=usage_context
                ),
            )
            result = await client.evaluate(state, questions, http_client=http_client)
        except ProviderOutcomeUnknownError as exc:
            raise DecisionError(
                (
                    "The Provider may have processed this evaluation, but no "
                    "usable result arrived. A new evaluation may incur "
                    "another charge."
                ),
                code="outcome_unknown",
            ) from exc
        except (ProviderError, NetworkError) as exc:
            raise DecisionError(str(exc), code="provider_error") from exc
        return {**result, "target": target, "duration_ms": round((monotonic() - started) * 1000)}

    async def list_experiments(self) -> dict[str, Any]:
        return {
            "experiments": await self._run(self._store.list),
            "available": self.available(),
        }

    async def get_experiment(self, identifier: str) -> dict[str, Any]:
        return await self._run(self._store.get, identifier)

    async def save_experiment(
        self, draft: Any, identifier: str | None = None, revision: int | None = None
    ) -> dict[str, Any]:
        return await self._run(self._store.save, draft, identifier, revision)

    async def delete_experiment(self, identifier: str, revision: int) -> None:
        await self._run(self._store.delete, identifier, revision)

    async def history(self, identifier: str, before: int | None = None) -> dict[str, Any]:
        return await self._run(self._store.history, identifier, before)

    async def evaluation(self, identifier: str) -> dict[str, Any]:
        return await self._run(self._store.evaluation, identifier)

    async def start(
        self, identifier: str, revision: int, request_id: str, mode: str = "evaluate"
    ) -> dict[str, Any]:
        text(request_id, "Request id", maximum=128)
        if not isinstance(mode, str) or mode not in {"evaluate", "control"}:
            raise DecisionError("Mode must be evaluate or control.")
        if self._closed:
            raise DecisionError("Decision service is stopping.", code="unavailable")
        # Request cancellation/disconnection must not orphan a persisted evaluation.
        operation = asyncio.create_task(self._start(identifier, revision, request_id, mode))
        self._starts.add(operation)
        operation.add_done_callback(self._start_done)
        return await asyncio.shield(operation)

    def _start_done(self, task: asyncio.Task[dict[str, Any]]) -> None:
        self._starts.discard(task)
        if not task.cancelled():
            task.exception()  # Retrieved even if the requesting accessor disconnected.

    async def _start(
        self, identifier: str, revision: int, request_id: str, mode: str
    ) -> dict[str, Any]:
        async with self._admission:
            previous = await self._run(self._store.request, request_id)
            if previous:
                if (
                    previous["experiment_id"] != identifier
                    or previous["snapshot"]["revision"] != revision
                    or previous["snapshot"]["mode"] != mode
                ):
                    raise DecisionError(
                        "Request id already belongs to another evaluation.", code="conflict"
                    )
                return previous
            target = self._target()
            experiment = await self.get_experiment(identifier)
            draft = experiment["draft"]
            if mode == "control":
                draft["control"] = validate_control(draft.get("control"))
            else:
                draft["state"], draft["questions"] = validate_input(
                    draft["state"], draft["questions"]
                )
            snapshot = {**draft, "target": target, "revision": revision, "mode": mode}
            record = await self._run(self._store.begin, identifier, revision, request_id, snapshot)
            task = asyncio.create_task(self._execute(record), name=f"decision:{record['id']}")
            self._tasks[record["id"]] = task
            task.add_done_callback(lambda finished: self._execution_done(record["id"], finished))
            return record

    def _execution_done(self, identifier: str, task: asyncio.Task[None]) -> None:
        self._tasks.pop(identifier, None)
        if not task.cancelled() and task.exception() is not None:
            _LOGGER.error(
                "Decision result could not be persisted (id=%s)",
                identifier,
                exc_info=task.exception(),
            )

    async def _execute(self, record: dict[str, Any]) -> None:
        snapshot = record["snapshot"]
        result = error = None
        status = "completed"
        try:
            if snapshot["mode"] == "control":
                result = await self._control(record)
            else:
                result = await self._evaluate(
                    snapshot["target"], snapshot["state"], snapshot["questions"]
                )
        except asyncio.CancelledError:
            status = "cancelled"
        except DecisionError as exc:
            status, error = "failed", {"code": exc.code, "message": str(exc)}
        except Exception:
            _LOGGER.exception("Decision evaluation failed (id=%s)", record["id"])
            status, error = (
                "failed",
                {
                    "code": "internal_error",
                    "message": (
                        "Evaluation failed unexpectedly. Try again after checking the server logs."
                    ),
                },
            )
        if not self._closed:
            await self._run(self._store.finish, record["id"], status, result=result, error=error)

    async def _control(self, record: dict[str, Any]) -> dict[str, Any]:
        setup = record["snapshot"]["control"]
        question = {
            "id": "action",
            "type": "choice",
            "instructions": setup["instructions"],
            "criteria": {key: value["description"] for key, value in setup["actions"].items()},
        }
        progress: dict[str, Any] = {
            "mode": "control",
            "steps_completed": 0,
            "steps": [],
            "phase": "observing",
        }
        async with httpx.AsyncClient(verify=shared_ssl_context()) as http_client:
            while not setup["max_steps"] or progress["steps_completed"] < setup["max_steps"]:
                progress["phase"] = "observing"
                await self._run(self._store.progress, record["id"], progress)
                started = monotonic()
                raw = await run_command(setup["observe"], setup["timeout_seconds"])
                try:
                    observation = json.loads(raw)
                except (ValueError, RecursionError) as exc:
                    raise DecisionError(
                        'Observation must return JSON: {"state": ..., "done": false}.',
                        code="invalid_observation",
                    ) from exc
                if (
                    not isinstance(observation, dict)
                    or set(observation) != {"state", "done"}
                    or not isinstance(observation["done"], bool)
                ):
                    raise DecisionError(
                        "Observation must contain state and a boolean done field only.",
                        code="invalid_observation",
                    )
                if observation["done"]:
                    progress.update(phase="completed", stop_reason="application_done")
                    return progress
                state, questions = validate_input(observation["state"], [question])
                progress["phase"] = "deciding"
                await self._run(self._store.progress, record["id"], progress)
                decision = await self._evaluate(
                    record["snapshot"]["target"], state, questions, http_client=http_client
                )
                action = decision["answers"]["action"]["choice"]
                step = {
                    "number": progress["steps_completed"] + 1,
                    "state": state,
                    "decision": decision,
                    "action": action,
                    "status": "executing",
                }
                progress["steps"] = [*progress["steps"][-49:], step]
                progress["phase"] = "acting"
                # Persist intent before issuing an action. Interrupted effects are never replayed.
                await self._run(self._store.progress, record["id"], progress)
                command = setup["actions"][action]["command"]
                step["output"] = (
                    await run_command(command, setup["timeout_seconds"]) if command else ""
                )
                step.update(status="completed", duration_ms=round((monotonic() - started) * 1000))
                progress["steps_completed"] += 1
                progress["phase"] = "waiting"
                await self._run(self._store.progress, record["id"], progress)
                await asyncio.sleep(setup["interval_ms"] / 1000)
            progress.update(phase="completed", stop_reason="step_limit")
            return progress

    async def cancel(self, identifier: str) -> dict[str, Any]:
        record = await self.evaluation(identifier)
        task = self._tasks.get(identifier)
        if task is not None and not task.done():
            if not task.cancelling():
                task.cancel()
            await asyncio.gather(asyncio.shield(task), return_exceptions=True)
            # A task cancelled before its first instruction cannot run cleanup.
            await self._run(self._store.finish, identifier, "cancelled")
        return await self.evaluation(record["id"])

    def close(self) -> None:
        self._closed = True
        try:
            for operation in tuple(self._starts):
                operation.cancel()
            for identifier, task in tuple(self._tasks.items()):
                if not task.cancelling():
                    task.cancel()
                if not self._store.database.is_closed():
                    self._store.finish(identifier, "interrupted")
        finally:
            self._store.close()

    async def aclose(self) -> None:
        self._closed = True
        try:
            await asyncio.gather(*tuple(self._starts), return_exceptions=True)
            async with self._admission:
                tasks = tuple(self._tasks.items())
                for _, task in tasks:
                    if not task.cancelling():
                        task.cancel()
                await asyncio.gather(*(task for _, task in tasks), return_exceptions=True)
                for identifier, _ in tasks:
                    await self._run(self._store.finish, identifier, "interrupted")
        finally:
            self._store.close()
