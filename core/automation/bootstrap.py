"""Persistent startup-triggered Agent Runs."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast
from uuid import uuid4

from core.config_validation import (
    JsonConfigValidationError,
    JsonDiagnostic,
    JsonValidationReport,
    add_error,
    error_diagnostic,
    load_validated_json_file,
    validate_allowed_string,
    validate_json_file,
    validate_non_empty_string,
    validate_optional_allowed_string,
    validate_optional_string,
    warn_unknown_keys,
)
from core.runs import RunKind, RunStatus
from core.settings import is_valid_agent_id, is_valid_project_id
from core.utils.atomic import atomic_write_text
from core.utils.errors import VBotError
from core.utils.logging import get_logger

if TYPE_CHECKING:
    from core.automation.automation import TriggerService
    from core.projects import AgentResolver
    from core.sessions import ChatSessionManager

BootstrapMode = Literal["once", "always"]
BootstrapStatus = Literal["active", "paused", "completed", "failed"]
BootstrapOutcome = Literal["success", "failed", "cancelled"]

BOOTSTRAP_MODES = frozenset(("once", "always"))
BOOTSTRAP_STATUSES = frozenset(("active", "paused", "completed", "failed"))
BOOTSTRAP_OUTCOMES = frozenset(("success", "failed", "cancelled"))
TERMINAL_BOOTSTRAP_STATUSES = frozenset(("completed",))
MAX_STORED_BOOTSTRAP_JOBS = 512
MAX_ACTIVE_BOOTSTRAP_JOBS = 64
MAX_CONCURRENT_BOOTSTRAP_RUNS = 4
_LAST_ERROR_MAX_CHARS = 500
_MUTABLE_FIELDS = frozenset(("agent_id", "project_id", "name", "prompt", "mode", "session_id"))
_JOB_FIELDS = _MUTABLE_FIELDS | {
    "id",
    "status",
    "created_at",
    "armed_after_startup_id",
    "last_started_startup_id",
    "last_started_at",
    "last_completed_at",
    "last_run_id",
    "last_session_id",
    "last_outcome",
    "last_error",
}

_LOGGER = get_logger("automation.bootstrap")


class BootstrapServiceError(VBotError):
    """Base class for expected Bootstrap service failures."""


class BootstrapJobNotFoundError(BootstrapServiceError):
    """Raised when a Bootstrap id is unknown."""


class BootstrapJobValidationError(BootstrapServiceError):
    """Raised when a Bootstrap definition is invalid."""


class BootstrapStorageError(BootstrapServiceError):
    """Raised when Bootstrap persistence is unreadable or unwritable."""


@dataclass(slots=True)
class BootstrapJob:
    """One persisted startup-triggered Run definition and its health state."""

    id: str
    agent_id: str
    project_id: str | None
    name: str
    prompt: str
    mode: BootstrapMode
    session_id: str | None
    status: BootstrapStatus
    created_at: str
    armed_after_startup_id: str
    last_started_startup_id: str | None = None
    last_started_at: str | None = None
    last_completed_at: str | None = None
    last_run_id: str | None = None
    last_session_id: str | None = None
    last_outcome: BootstrapOutcome | None = None
    last_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "agent_id": self.agent_id,
            "project_id": self.project_id,
            "name": self.name,
            "prompt": self.prompt,
            "mode": self.mode,
            "session_id": self.session_id,
            "status": self.status,
            "created_at": self.created_at,
            "armed_after_startup_id": self.armed_after_startup_id,
            "last_started_startup_id": self.last_started_startup_id,
            "last_started_at": self.last_started_at,
            "last_completed_at": self.last_completed_at,
            "last_run_id": self.last_run_id,
            "last_session_id": self.last_session_id,
            "last_outcome": self.last_outcome,
            "last_error": self.last_error,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> BootstrapJob:
        return cls(
            id=str(payload["id"]),
            agent_id=str(payload["agent_id"]),
            project_id=payload.get("project_id"),
            name=str(payload["name"]),
            prompt=str(payload["prompt"]),
            mode=cast("BootstrapMode", payload["mode"]),
            session_id=payload.get("session_id"),
            status=cast("BootstrapStatus", payload.get("status") or "active"),
            created_at=str(payload["created_at"]),
            armed_after_startup_id=str(payload["armed_after_startup_id"]),
            last_started_startup_id=payload.get("last_started_startup_id"),
            last_started_at=payload.get("last_started_at"),
            last_completed_at=payload.get("last_completed_at"),
            last_run_id=payload.get("last_run_id"),
            last_session_id=payload.get("last_session_id"),
            last_outcome=payload.get("last_outcome"),
            last_error=payload.get("last_error"),
        )


def validate_bootstrap_jobs_file(jobs_path: str | Path) -> JsonValidationReport:
    """Validate persisted ``bootstrap/jobs.json`` without consuming it."""
    return validate_json_file(jobs_path, validate_bootstrap_jobs_data, missing_ok=True)


def validate_bootstrap_jobs_data(data: Any) -> list[JsonDiagnostic]:
    """Validate a decoded Bootstrap job array."""
    if not isinstance(data, list):
        return [error_diagnostic("$", f"Expected a JSON array, got {type(data).__name__}")]
    diagnostics: list[JsonDiagnostic] = []
    for index, item in enumerate(data):
        _validate_job_data(diagnostics, index, item)
    return diagnostics


def _validate_job_data(diagnostics: list[JsonDiagnostic], index: int, item: Any) -> None:
    path = f"$[{index}]"
    if not isinstance(item, dict):
        add_error(diagnostics, path, "Expected a JSON object")
        return
    warn_unknown_keys(diagnostics, path, item, _JOB_FIELDS, "Bootstrap job field")
    for field in ("id", "agent_id", "name", "prompt", "created_at", "armed_after_startup_id"):
        validate_non_empty_string(diagnostics, f"{path}.{field}", item.get(field), required=True)
    agent_id = item.get("agent_id")
    if isinstance(agent_id, str) and agent_id and not is_valid_agent_id(agent_id):
        add_error(diagnostics, f"{path}.agent_id", "must be a valid Agent id")
    project_id = item.get("project_id")
    if isinstance(project_id, str) and project_id and not is_valid_project_id(project_id):
        add_error(diagnostics, f"{path}.project_id", "must be a valid Project id")
    validate_allowed_string(diagnostics, f"{path}.mode", item.get("mode"), BOOTSTRAP_MODES)
    validate_optional_allowed_string(
        diagnostics, f"{path}.status", item.get("status"), BOOTSTRAP_STATUSES
    )
    validate_optional_allowed_string(
        diagnostics, f"{path}.last_outcome", item.get("last_outcome"), BOOTSTRAP_OUTCOMES
    )
    for field in (
        "project_id",
        "session_id",
        "last_started_startup_id",
        "last_started_at",
        "last_completed_at",
        "last_run_id",
        "last_session_id",
        "last_error",
    ):
        validate_optional_string(diagnostics, f"{path}.{field}", item.get(field))


def _load_payload(path: Path) -> list[Any]:
    try:
        return cast(
            "list[Any]",
            load_validated_json_file(
                path,
                lambda data: (
                    []
                    if isinstance(data, list)
                    else [
                        error_diagnostic("$", f"Expected a JSON array, got {type(data).__name__}")
                    ]
                ),
                missing_ok=True,
                missing_default=[],
            ),
        )
    except JsonConfigValidationError as error:
        raise BootstrapStorageError(str(error)) from error


class BootstrapService:
    """Manage Bootstrap CRUD and start eligible jobs once the server is ready."""

    def __init__(
        self,
        trigger_service: TriggerService,
        data_root: str | Path,
        *,
        startup_id: str,
        agent_resolver: AgentResolver | None = None,
        sessions: ChatSessionManager | None = None,
    ) -> None:
        self._trigger_service = trigger_service
        self._agent_resolver = agent_resolver
        self._sessions = sessions
        self._startup_id = startup_id
        self._jobs_path = Path(data_root).expanduser() / "bootstrap" / "jobs.json"
        self._jobs: dict[str, BootstrapJob] = {}
        self._invalid_entries: list[Any] = []
        self._storage_load_error: BootstrapStorageError | None = None
        self._loaded = False
        self._activation_task: asyncio.Task[None] | None = None
        self._running_job_ids: set[str] = set()
        self._active_runs: dict[str, Any] = {}

    @property
    def startup_id(self) -> str:
        return self._startup_id

    def create_job(
        self,
        *,
        agent_id: str,
        prompt: str,
        mode: BootstrapMode,
        name: str | None = None,
        session_id: str | None = None,
        project_id: str | None = None,
    ) -> BootstrapJob:
        self._ensure_loaded()
        if len(self._jobs) >= MAX_STORED_BOOTSTRAP_JOBS:
            raise BootstrapJobValidationError(
                f"Bootstrap stores at most {MAX_STORED_BOOTSTRAP_JOBS} jobs; delete history first"
            )
        job = BootstrapJob(
            id=str(uuid4()),
            agent_id=agent_id,
            project_id=project_id,
            name=name if name is not None else _derive_name(prompt),
            prompt=prompt,
            mode=mode,
            session_id=session_id,
            status="active",
            created_at=_utc_now_iso(),
            armed_after_startup_id=self._startup_id,
        )
        self._validate_job(job)
        self._validate_capacity(job)
        self._jobs[job.id] = job
        try:
            self._save()
        except Exception:
            self._jobs.pop(job.id, None)
            raise
        _LOGGER.info("Bootstrap created (job=%s agent=%s mode=%s)", job.id, job.agent_id, job.mode)
        return replace(job)

    def list_jobs(self) -> list[BootstrapJob]:
        self._ensure_loaded(allow_degraded=True)
        return [
            replace(job)
            for job in sorted(self._jobs.values(), key=lambda item: (item.created_at, item.id))
        ]

    def get_job(self, job_id: str) -> BootstrapJob:
        self._ensure_loaded()
        job = self._jobs.get(job_id)
        if job is None:
            raise BootstrapJobNotFoundError(f"Bootstrap job not found: {job_id}")
        return replace(job)

    def update_job(self, job_id: str, **fields: Any) -> BootstrapJob:
        self._ensure_loaded()
        current = self._jobs.get(job_id)
        if current is None:
            raise BootstrapJobNotFoundError(f"Bootstrap job not found: {job_id}")
        unknown = sorted(set(fields) - _MUTABLE_FIELDS)
        if unknown:
            raise BootstrapJobValidationError(f"Unsupported Bootstrap fields: {', '.join(unknown)}")
        if current.status == "completed":
            raise BootstrapJobValidationError("Completed Bootstrap jobs are immutable history")
        self._require_not_running(job_id)
        candidate = replace(current)
        for field, value in fields.items():
            setattr(candidate, field, value)
        if not any(getattr(current, field) != getattr(candidate, field) for field in fields):
            return replace(current)
        candidate.status = "active"
        candidate.armed_after_startup_id = self._startup_id
        candidate.last_started_startup_id = None
        candidate.last_error = None
        self._validate_job(candidate)
        self._validate_capacity(candidate, replacing_id=job_id)
        self._jobs[job_id] = candidate
        try:
            self._save()
        except Exception:
            self._jobs[job_id] = current
            raise
        return replace(candidate)

    def delete_job(self, job_id: str) -> None:
        self._ensure_loaded()
        self._require_not_running(job_id)
        removed = self._jobs.pop(job_id, None)
        if removed is None:
            raise BootstrapJobNotFoundError(f"Bootstrap job not found: {job_id}")
        try:
            self._save()
        except Exception:
            self._jobs[job_id] = removed
            raise

    def enable_job(self, job_id: str) -> BootstrapJob:
        self._ensure_loaded()
        current = self._jobs.get(job_id)
        if current is None:
            raise BootstrapJobNotFoundError(f"Bootstrap job not found: {job_id}")
        if current.status == "completed":
            raise BootstrapJobValidationError("Completed Bootstrap jobs cannot be re-enabled")
        self._require_not_running(job_id)
        candidate = replace(
            current,
            status="active",
            armed_after_startup_id=self._startup_id,
            last_started_startup_id=None,
            last_error=None,
        )
        self._validate_capacity(candidate, replacing_id=job_id)
        self._replace_and_save(current, candidate)
        return replace(candidate)

    def disable_job(self, job_id: str) -> BootstrapJob:
        self._ensure_loaded()
        current = self._jobs.get(job_id)
        if current is None:
            raise BootstrapJobNotFoundError(f"Bootstrap job not found: {job_id}")
        if current.status == "completed":
            raise BootstrapJobValidationError("Completed Bootstrap jobs cannot be paused")
        self._require_not_running(job_id)
        candidate = replace(current, status="paused")
        self._replace_and_save(current, candidate)
        return replace(candidate)

    def restore_job(self, job: BootstrapJob) -> None:
        """Restore an exact prior record during a coordinated RPC rollback."""
        self._ensure_loaded()
        self._require_not_running(job.id)
        current = self._jobs.get(job.id)
        self._jobs[job.id] = replace(job)
        try:
            self._save()
        except Exception:
            if current is None:
                self._jobs.pop(job.id, None)
            else:
                self._jobs[job.id] = current
            raise

    def activate(self) -> None:
        """Start eligible jobs in the background; idempotent per Runtime startup."""
        if self._activation_task is not None and not self._activation_task.done():
            return
        self._ensure_loaded(allow_degraded=True)
        if self._storage_load_error is not None:
            return
        self._activation_task = asyncio.create_task(
            self._activate_jobs(), name=f"bootstrap-activation:{self._startup_id}"
        )
        self._activation_task.add_done_callback(self._log_activation_failure)

    def stop(self) -> None:
        for run in tuple(self._active_runs.values()):
            request_cancel = getattr(run, "request_cancel", None)
            if callable(request_cancel):
                request_cancel(reason="shutdown")
        if self._activation_task is not None and not self._activation_task.done():
            self._activation_task.cancel()

    async def aclose(self) -> None:
        task = self._activation_task
        self.stop()
        if task is not None and not task.done():
            await asyncio.gather(task, return_exceptions=True)
        self._activation_task = None

    async def wait_until_idle(self) -> None:
        task = self._activation_task
        if task is not None:
            await asyncio.shield(task)

    async def _activate_jobs(self) -> None:
        self._reconcile_once_jobs()
        eligible = [job for job in self._jobs.values() if self._eligible(job)]
        groups: dict[tuple[str, ...], list[str]] = {}
        for job in sorted(eligible, key=lambda item: (item.created_at, item.id)):
            key = (
                (
                    (job.project_id or ""),
                    job.agent_id,
                    job.session_id,
                )
                if job.session_id is not None
                else (job.id,)
            )
            groups.setdefault(cast("tuple[str, ...]", key), []).append(job.id)
        slots = asyncio.Semaphore(MAX_CONCURRENT_BOOTSTRAP_RUNS)

        async def run_group(job_ids: list[str]) -> None:
            async with slots:
                for job_id in job_ids:
                    try:
                        await self._run_job(job_id)
                    except Exception:
                        _LOGGER.exception(
                            "Bootstrap execution failed unexpectedly (job=%s)", job_id
                        )

        await asyncio.gather(
            *(
                asyncio.create_task(run_group(ids), name=f"bootstrap-group:{ids[0]}")
                for ids in groups.values()
            )
        )

    def _eligible(self, job: BootstrapJob) -> bool:
        return bool(
            job.status == "active"
            and job.armed_after_startup_id != self._startup_id
            and job.last_started_startup_id != self._startup_id
        )

    async def _run_job(self, job_id: str) -> None:
        job = self._jobs.get(job_id)
        if job is None or not self._eligible(job):
            return
        self._running_job_ids.add(job_id)
        try:
            job.last_started_startup_id = self._startup_id
            job.last_started_at = _utc_now_iso()
            job.last_error = None
            self._save()
            self._validate_references(job)
            run = await self._trigger_service.trigger_run(
                job.agent_id,
                job.prompt,
                session_id=job.session_id,
                internal=True,
                project_id=job.project_id,
                run_kind=RunKind.SYSTEM,
                contributes_to_agent_activity=False,
                resume_process_restart=job.session_id is not None,
            )
            self._active_runs[job_id] = run
            latest = self._jobs.get(job_id)
            if latest is None:
                return
            latest.last_run_id = run.id
            latest.last_session_id = run.session_id
            self._save()
            try:
                await run.wait()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                outcome: BootstrapOutcome = (
                    "cancelled" if run.status == RunStatus.CANCELLED else "failed"
                )
                self._record_terminal(job_id, outcome, error)
            else:
                self._record_terminal(job_id, "success", None)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._record_terminal(job_id, "failed", error)
        finally:
            self._active_runs.pop(job_id, None)
            self._running_job_ids.discard(job_id)

    @staticmethod
    def _log_activation_failure(task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            _LOGGER.error(
                "Bootstrap activation failed: %s",
                error,
                exc_info=(type(error), error, error.__traceback__),
            )

    def _record_terminal(
        self, job_id: str, outcome: BootstrapOutcome, error: BaseException | None
    ) -> None:
        job = self._jobs.get(job_id)
        if job is None:
            return
        job.last_completed_at = _utc_now_iso()
        job.last_outcome = outcome
        job.last_error = _bounded_error(error)
        if job.mode == "once":
            job.status = "completed" if outcome == "success" else "failed"
        self._save()
        _LOGGER.info(
            "Bootstrap finished (job=%s outcome=%s status=%s)", job.id, outcome, job.status
        )

    def _reconcile_once_jobs(self) -> None:
        if self._sessions is None:
            return
        changed = False
        for job in self._jobs.values():
            if job.mode != "once" or job.status != "active" or not job.last_run_id:
                continue
            session_id = job.last_session_id or job.session_id
            if session_id is None:
                continue
            try:
                messages = self._sessions.get(job.agent_id, session_id, job.project_id).load()
            except Exception:
                continue
            summary = next(
                (
                    message
                    for message in reversed(messages)
                    if message.role == "run_summary" and message.run_id == job.last_run_id
                ),
                None,
            )
            if summary is None or summary.status not in {"completed", "failed", "cancelled"}:
                continue
            job.status = "completed" if summary.status == "completed" else "failed"
            job.last_outcome = (
                "success"
                if summary.status == "completed"
                else cast("BootstrapOutcome", summary.status)
            )
            job.last_completed_at = summary.timestamp
            if summary.status != "completed":
                job.last_error = f"Previous Bootstrap Run ended with status {summary.status}"
            changed = True
        if changed:
            self._save()

    def _replace_and_save(self, current: BootstrapJob, candidate: BootstrapJob) -> None:
        self._jobs[current.id] = candidate
        try:
            self._save()
        except Exception:
            self._jobs[current.id] = current
            raise

    def _require_not_running(self, job_id: str) -> None:
        if job_id in self._running_job_ids:
            raise BootstrapJobValidationError(
                f"Bootstrap job is currently running and cannot be changed: {job_id}"
            )

    def _ensure_loaded(self, *, allow_degraded: bool = False) -> None:
        if not self._loaded:
            try:
                self._jobs = self._load()
                self._storage_load_error = None
            except BootstrapStorageError as error:
                self._jobs = {}
                self._invalid_entries = []
                self._storage_load_error = error
                _LOGGER.error("Bootstrap disabled because storage is invalid: %s", error)
            self._loaded = True
        if self._storage_load_error is not None and not allow_degraded:
            raise self._storage_load_error

    def _load(self) -> dict[str, BootstrapJob]:
        self._jobs_path.parent.mkdir(parents=True, exist_ok=True)
        raw = _load_payload(self._jobs_path)
        jobs: dict[str, BootstrapJob] = {}
        self._invalid_entries = []
        for index, item in enumerate(raw):
            diagnostics: list[JsonDiagnostic] = []
            _validate_job_data(diagnostics, index, item)
            errors = [item for item in diagnostics if item.severity == "error"]
            if errors or not isinstance(item, dict):
                self._invalid_entries.append(item)
                _LOGGER.warning("Skipping invalid Bootstrap job at $[%d]", index)
                continue
            try:
                job = BootstrapJob.from_dict(item)
                self._validate_job(job, validate_references=False)
            except (BootstrapJobValidationError, TypeError, ValueError) as error:
                self._invalid_entries.append(item)
                _LOGGER.warning("Skipping invalid Bootstrap job at $[%d]: %s", index, error)
                continue
            if job.id in jobs:
                self._invalid_entries.append(item)
                _LOGGER.warning("Skipping duplicate Bootstrap job id at $[%d]: %s", index, job.id)
                continue
            jobs[job.id] = job
        return jobs

    def _save(self) -> None:
        self._jobs_path.parent.mkdir(parents=True, exist_ok=True)
        payload = [
            job.to_dict()
            for job in sorted(self._jobs.values(), key=lambda item: (item.created_at, item.id))
        ] + list(self._invalid_entries)
        try:
            atomic_write_text(
                self._jobs_path,
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            )
        except OSError as error:
            raise BootstrapStorageError(f"Cannot write {self._jobs_path}: {error}") from error

    def _validate_job(self, job: BootstrapJob, *, validate_references: bool = True) -> None:
        if not is_valid_agent_id(job.agent_id):
            raise BootstrapJobValidationError("agent_id is invalid")
        if job.project_id is not None and not is_valid_project_id(job.project_id):
            raise BootstrapJobValidationError("project_id is invalid")
        if not job.name.strip():
            raise BootstrapJobValidationError("name must be non-empty")
        if not job.prompt.strip():
            raise BootstrapJobValidationError("prompt must be non-empty")
        if job.mode not in BOOTSTRAP_MODES:
            raise BootstrapJobValidationError("mode must be once or always")
        if job.status not in BOOTSTRAP_STATUSES:
            raise BootstrapJobValidationError("status is invalid")
        if job.session_id is not None and not job.session_id.strip():
            raise BootstrapJobValidationError("session_id must be non-empty")
        if validate_references:
            self._validate_references(job)

    def _validate_references(self, job: BootstrapJob) -> None:
        if self._agent_resolver is not None:
            self._agent_resolver.resolve_agent(job.project_id, job.agent_id)
        if job.session_id is not None and self._sessions is not None:
            self._sessions.get(job.agent_id, job.session_id, job.project_id)

    def _validate_capacity(
        self, candidate: BootstrapJob, *, replacing_id: str | None = None
    ) -> None:
        active = sum(
            1 for job in self._jobs.values() if job.id != replacing_id and job.status == "active"
        )
        if candidate.status == "active" and active >= MAX_ACTIVE_BOOTSTRAP_JOBS:
            raise BootstrapJobValidationError(
                f"Bootstrap supports at most {MAX_ACTIVE_BOOTSTRAP_JOBS} active jobs"
            )


def _derive_name(prompt: str) -> str:
    first_line = next((line.strip() for line in prompt.splitlines() if line.strip()), "Bootstrap")
    return first_line[:80]


def _bounded_error(error: BaseException | None) -> str | None:
    if error is None:
        return None
    message = str(error).strip() or type(error).__name__
    return message[:_LAST_ERROR_MAX_CHARS]


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()
