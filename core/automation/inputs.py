"""Configured, authenticated inputs for concrete tasks supplied by programs.

Configuration belongs here; Chat owns durable acceptance, Sessions and execution.
An input never creates a Cron job or a second waiting-work queue.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import hmac
import json
import secrets
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

from core.config_validation import (
    JsonDiagnostic,
    JsonValidationReport,
    add_error,
    read_json_file,
    validate_json_file,
)
from core.json_documents import (
    JsonDocumentFormat,
    json_document,
    json_list,
    json_object,
    validate_collection_root,
    write_json_document,
)
from core.projects import AgentOverrides
from core.runs import SILENT_ANSWER
from core.secrets import WEBHOOK_SECRETS
from core.sessions import SessionAddress
from core.utils.errors import VBotError
from core.utils.ids import is_safe_id, new_id
from core.utils.logging import get_logger
from core.utils.workers import BoundedWorkerPool, settle_before_cancelling

if TYPE_CHECKING:
    from core.chat import ChatLoop
    from core.projects import AgentResolver
    from core.secrets import SecretStore
    from core.sessions import ChatSessionManager

INPUT_PATH = "/api/inputs/"
_FIELDS = {"id", "name", "agent_id", "project_id", "working_project_id", "enabled", "run_overrides"}
_OVERRIDES = {"model", "thinking_effort", "thinking_effort_default"}
_SHAPE = json_document(
    {"inputs"},
    {
        "inputs": json_list(
            json_object(
                _FIELDS,
                {
                    "run_overrides": json_object(_OVERRIDES),
                },
            ),
            key="id",
        )
    },
)
_WORKERS = BoundedWorkerPool(name="direct-inputs", max_workers=2)
_LOGGER = get_logger("automation.inputs")


class DirectInputError(VBotError):
    """An input or its request cannot be used."""


class DirectInputNotFoundError(DirectInputError):
    """The authenticated input does not own this resource."""


def input_overrides(value: Any) -> AgentOverrides:
    if not isinstance(value, dict) or set(value) - _OVERRIDES:
        raise DirectInputError(
            "run_overrides accepts model, thinking_effort and thinking_effort_default"
        )
    try:
        return AgentOverrides(**value)
    except (TypeError, ValueError) as error:
        raise DirectInputError(str(error)) from error


def validate_inputs_data(data: Any) -> list[JsonDiagnostic]:
    diagnostics: list[JsonDiagnostic] = []
    rows = validate_collection_root(diagnostics, data, version=1, shape=_SHAPE, collection="inputs")
    ids: set[str] = set()
    for index, row in enumerate(rows or ()):
        path = f"$.inputs[{index}]"
        if not isinstance(row, dict):
            add_error(diagnostics, path, "must be an object")
            continue
        for field in ("id", "name", "agent_id"):
            if not isinstance(row.get(field), str) or not row[field].strip():
                add_error(diagnostics, f"{path}.{field}", "must be a non-empty string")
        identity = row.get("id")
        if isinstance(identity, str):
            if identity in ids or not is_safe_id(identity):
                add_error(diagnostics, f"{path}.id", "must be a unique opaque id")
            ids.add(identity)
        for field in ("project_id", "working_project_id"):
            if row.get(field) is not None and (not isinstance(row[field], str) or not row[field]):
                add_error(diagnostics, f"{path}.{field}", "must be a non-empty string or null")
        if not isinstance(row.get("enabled", True), bool):
            add_error(diagnostics, f"{path}.enabled", "must be a boolean")
        try:
            input_overrides(
                {
                    key: value
                    for key, value in row.get("run_overrides", {}).items()
                    if key in _OVERRIDES
                }
            )
        except DirectInputError, AttributeError:
            add_error(
                diagnostics,
                f"{path}.run_overrides",
                "must contain valid Model and Reasoning overrides",
            )
    return diagnostics


def validate_inputs_file(path: Path) -> JsonValidationReport:
    return validate_json_file(path, validate_inputs_data, missing_ok=True)


_FORMAT = JsonDocumentFormat("direct inputs", 1, _SHAPE, validate_inputs_data)


class DirectInputService:
    def __init__(
        self,
        data_dir: Path,
        *,
        chat: ChatLoop,
        sessions: ChatSessionManager,
        resolver: AgentResolver,
        secret_store: SecretStore,
    ) -> None:
        self._path = data_dir / "automation" / "inputs.json"
        self._chat = chat
        self._sessions = sessions
        self._resolver = resolver
        self._secrets = secret_store
        self._lock = asyncio.Lock()
        self._file_lock = threading.RLock()
        self._base_url = ""
        self._reach: str | None = None
        self._decoy = secrets.token_urlsafe(32)
        report, document = read_json_file(self._path, validate_inputs_data, missing_ok=True)
        self._error = (
            None
            if report.ok
            else "Task input configuration is unreadable; repair automation/inputs.json"
        )
        self._rows = document["inputs"] if report.ok and document is not None else []
        if self._error:
            _LOGGER.warning("Task input configuration unavailable")

    def bind_url(self, base_url: str, reach: str) -> None:
        self._base_url, self._reach = base_url.rstrip("/"), reach

    def list_inputs(self) -> list[dict[str, Any]]:
        if self._error:
            raise DirectInputError(self._error)
        return copy.deepcopy(self._rows)

    def get(self, identity: str) -> dict[str, Any]:
        for row in self.list_inputs():
            if row["id"] == identity:
                return row
        raise DirectInputNotFoundError("Task input not found")

    def payload(self, row: dict[str, Any]) -> dict[str, Any]:
        path = INPUT_PATH + row["id"]
        return {
            **row,
            "path": path,
            "url": self._base_url + path if self._base_url else None,
            "reach": self._reach,
        }

    async def configure(self, identity: str | None, changes: dict[str, Any]) -> dict[str, Any]:
        async with self._lock:
            return await settle_before_cancelling(self._configure(identity, changes))

    async def _configure(self, identity: str | None, changes: dict[str, Any]) -> dict[str, Any]:
        rows = self.list_inputs()
        previous = self.get(identity) if identity else None
        row: dict[str, Any] = (
            self.get(identity)
            if identity
            else {
                "id": new_id("in", claim=lambda value: all(entry["id"] != value for entry in rows)),
                "enabled": True,
                "run_overrides": {},
            }
        )
        if set(changes) - (_FIELDS - {"id"}):
            raise DirectInputError("Unsupported task input setting")
        if "run_overrides" in changes:
            input_overrides(changes["run_overrides"])
        row.update(changes)
        diagnostics = validate_inputs_data({"format_version": 1, "inputs": [row]})
        if any(item.severity == "error" for item in diagnostics):
            raise DirectInputError("Task input requires a name, Agent and valid execution settings")
        overrides = input_overrides(
            {key: value for key, value in row.get("run_overrides", {}).items() if key in _OVERRIDES}
        )
        agent = await self._resolver.resolve_agent_async(
            row.get("project_id"), row["agent_id"], run_overrides=overrides, require_model=False
        )
        working = await self._resolver.resolve_working_project_async(
            row.get("project_id"), agent, requested=row.get("working_project_id")
        )
        row["working_project_id"] = working
        if row == previous:
            return self.payload(row)
        rows = [entry for entry in rows if entry["id"] != row["id"]] + [row]
        if identity is None:
            await self.rotate_secret(row["id"], creating=True)
        try:
            await _WORKERS.run(self._save, rows)
        except BaseException:
            if identity is None:
                await _WORKERS.run(self._secrets.remove_entry, WEBHOOK_SECRETS, row["id"])
            raise
        _LOGGER.info(
            "Task input %s (input=%s actor=rpc)", "updated" if identity else "created", row["id"]
        )
        return self.payload(row)

    def _save(self, rows: list[dict[str, Any]]) -> None:
        with self._file_lock:
            write_json_document(self._path, {"inputs": rows}, _FORMAT)
            self._rows = copy.deepcopy(rows)

    async def delete(self, identity: str) -> None:
        async with self._lock:

            async def remove() -> None:
                self.get(identity)
                await _WORKERS.run(
                    self._save, [row for row in self.list_inputs() if row["id"] != identity]
                )
                await _WORKERS.run(self._secrets.remove_entry, WEBHOOK_SECRETS, identity)
                _LOGGER.info("Task input deleted (input=%s actor=rpc)", identity)

            await settle_before_cancelling(remove())

    def retarget_agent(self, old: str, new: str) -> tuple[str, ...]:
        rows = self.list_inputs()
        changed: list[str] = []
        for row in rows:
            if row["agent_id"] == old and row.get("project_id") is None:
                row["agent_id"] = new
                changed.append(row["id"])
        if changed:
            self._save(rows)
        return tuple(changed)

    async def secret(self, identity: str) -> str:
        self.get(identity)
        value = await _WORKERS.run(self._secrets.entry, WEBHOOK_SECRETS, identity)
        if not value:
            raise DirectInputError("Task input has no secret; replace it to create one")
        return value

    async def rotate_secret(self, identity: str, *, creating: bool = False) -> str:
        if not creating:
            self.get(identity)
        value = "vbi_" + secrets.token_urlsafe(32)
        await _WORKERS.run(self._secrets.set_entry, WEBHOOK_SECRETS, identity, value)
        if not creating:
            _LOGGER.info("Task input secret rotated (input=%s actor=rpc)", identity)
        return value

    async def authenticate(self, identity: str, authorization: str, *, submitting: bool) -> bool:
        try:
            row = self.get(identity)
            expected = await self.secret(identity)
        except DirectInputNotFoundError:
            row, expected = {}, self._decoy
        except DirectInputError:
            return False
        scheme, _, token = authorization.partition(" ")
        valid = hmac.compare_digest(expected.encode(), token.encode())
        return (
            bool(row)
            and (not submitting or row.get("enabled", True))
            and scheme.lower() == "bearer"
            and valid
        )

    async def submit(
        self, identity: str, body: dict[str, Any], *, request_id: str
    ) -> dict[str, Any]:
        allowed = {"prompt", "session_id", "run_overrides", "wait", "wait_timeout_seconds"}
        if set(body) - allowed:
            raise DirectInputError(
                "Unsupported request fields; send prompt, session_id, run_overrides, "
                "wait or wait_timeout_seconds"
            )
        prompt = body.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise DirectInputError("prompt must be a non-empty string")
        session_id = body.get("session_id")
        if session_id is not None and (not isinstance(session_id, str) or not session_id):
            raise DirectInputError("session_id must be a non-empty string")
        requested = body.get("run_overrides", {})
        input_overrides(requested)
        digest = hashlib.sha256(
            json.dumps(
                {
                    key: value
                    for key, value in body.items()
                    if key not in {"wait", "wait_timeout_seconds"}
                },
                sort_keys=True,
                ensure_ascii=True,
            ).encode()
        ).hexdigest()
        input_id = f"{identity}:{request_id}"
        prior = await self._chat.lookup_accessor_input(input_id, request_hash=digest)
        if prior is None:
            row = self.get(identity)
            values = {
                key: value
                for key, value in row.get("run_overrides", {}).items()
                if key in _OVERRIDES
            }
            if "thinking_effort" in requested:
                values.pop("thinking_effort_default", None)
            if requested.get("thinking_effort_default"):
                values.pop("thinking_effort", None)
            values.update(requested)
            overrides = input_overrides(values)
            generation = None
            if session_id is not None:
                session = await self._sessions.get_async(
                    SessionAddress(
                        project_id=row.get("project_id"),
                        agent_id=row["agent_id"],
                        session_id=session_id,
                    )
                )
                if (
                    await self._sessions.metadata_value_async(session.address, "input_source_id")
                    != identity
                ):
                    raise DirectInputNotFoundError("Session does not belong to this task input")
                generation = session.generation_id
            await self._chat.submit_accessor_input(
                row["agent_id"],
                prompt,
                input_id=input_id,
                request_hash=digest,
                project_id=row.get("project_id"),
                session_id=session_id,
                new_session=session_id is None,
                working_project_id=row.get("working_project_id"),
                run_overrides=overrides,
                input_source_id=identity,
                expected_generation_id=generation,
            )
        return await self.result(identity, request_id)

    async def wait(self, identity: str, request_id: str, timeout: float) -> dict[str, Any]:
        record = await self._sessions.lookup_accessor_input_async(f"{identity}:{request_id}")
        if record is None:
            raise DirectInputNotFoundError("Request not found")
        submission = await self._chat.lookup_accessor_input(
            record.input_id, request_hash=record.request_hash
        )
        if submission is not None:
            try:
                async with asyncio.timeout(timeout):
                    run = submission.run
                    if run is None and submission.queued_item is not None:
                        run = await asyncio.shield(submission.queued_item.future)
                    if run is not None:
                        await run.wait()
            except Exception as error:
                # Terminal failures are returned as durable status. A deadline
                # ends only the HTTP wait, never the accepted work.
                settled = await self.result(identity, request_id)
                if settled["status"] not in {
                    "completed",
                    "failed",
                    "cancelled",
                    "interrupted",
                    "removed",
                    "unavailable",
                } and not isinstance(error, TimeoutError):
                    raise
        return await self.result(identity, request_id)

    async def result(self, identity: str, request_id: str) -> dict[str, Any]:
        self.get(identity)
        record = await self._sessions.lookup_accessor_input_async(f"{identity}:{request_id}")
        if record is None:
            raise DirectInputNotFoundError("Request not found")
        status = record.run_status or record.state
        result: dict[str, Any] = {
            "request_id": request_id,
            "session_id": record.address.session_id,
            "run_id": record.run_id,
            "status": status,
            "status_url": f"{self._base_url}{INPUT_PATH}{identity}/requests/{request_id}",
        }
        if not record.live:
            result["status"] = "unavailable"
        elif record.run_id and status in {"completed", "failed", "cancelled", "interrupted"}:
            session = await self._sessions.get_async(record.address)
            answer = await session.load_run_result_async(run_id=record.run_id)
            content = answer.assistant.content if answer and answer.assistant else ""
            result["content"] = "" if content == SILENT_ANSWER else content
        return result
