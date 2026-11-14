"""The instance's durable, voluntary first-setup progress.

Completion records a past choice, not current Provider health. Only this owner
changes the state; Accessors never reset it when credentials or Models disappear.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from pathlib import Path
from threading import RLock
from typing import Any, Literal, cast

from core.config_validation import (
    JsonConfigValidationError,
    JsonDiagnostic,
    JsonValidationReport,
    add_error,
    error_diagnostic,
    load_validated_json_file,
    validate_json_file,
)
from core.json_documents import (
    JsonDocumentFormat,
    JsonDocumentWriteError,
    json_document,
    validate_format_version,
    warn_unknown_fields,
    write_json_document,
)
from core.utils.errors import StorageError
from core.utils.logging import get_logger

type SetupStatus = Literal["pending", "completed", "skipped"]
type SetupStep = Literal["provider", "model", "components", "services"]

SETUP_STEPS: tuple[SetupStep, ...] = ("provider", "model", "components", "services")
_STATUSES = ("pending", "completed", "skipped")
_SHAPE = json_document({"status", "step", "agent_id"})
_LOGGER = get_logger("settings.setup")


def _validate(data: Any) -> list[JsonDiagnostic]:
    if not isinstance(data, dict):
        return [error_diagnostic("$", "Expected a JSON object")]
    diagnostics: list[JsonDiagnostic] = []
    if not validate_format_version(diagnostics, data, 1):
        return diagnostics
    warn_unknown_fields(diagnostics, "$", data, _SHAPE)
    if data.get("status", "pending") not in _STATUSES:
        add_error(diagnostics, "$.status", "Expected pending, completed or skipped")
    if data.get("step", "provider") not in SETUP_STEPS:
        add_error(diagnostics, "$.step", "Expected provider, model, components or services")
    agent_id = data.get("agent_id", "main")
    if not isinstance(agent_id, str) or not agent_id.strip():
        add_error(diagnostics, "$.agent_id", "Expected a non-empty Agent id")
    return diagnostics


_FORMAT = JsonDocumentFormat("First setup", 1, _SHAPE, _validate)


def validate_setup_state_file(path: str | Path) -> JsonValidationReport:
    return validate_json_file(path, _validate, missing_ok=True)


@dataclass(frozen=True)
class SetupState:
    status: SetupStatus = "pending"
    step: SetupStep = "provider"
    agent_id: str = "main"

    def payload(self) -> dict[str, Any]:
        return {**asdict(self), "auto_show": self.status == "pending"}


class SetupStateStore:
    """Serialize monotonic completion and shared progress in one data directory."""

    def __init__(self, data_dir: Path) -> None:
        self.path = data_dir / "setup.json"
        self._lock = RLock()

    def read(self) -> SetupState:
        with self._lock:
            return self._read()

    def _read(self) -> SetupState:
        try:
            data = load_validated_json_file(
                self.path, _validate, missing_ok=True, missing_default={}
            )
        except (JsonConfigValidationError, OSError) as error:
            raise StorageError(f"The saved setup progress could not be read: {error}") from error
        return SetupState(
            status=cast(SetupStatus, data.get("status", "pending")),
            step=cast(SetupStep, data.get("step", "provider")),
            agent_id=data.get("agent_id", "main"),
        )

    def update(
        self,
        *,
        configured: bool = False,
        agent_id: str | None = None,
        step: SetupStep | None = None,
        finish: bool = False,
        actor: str = "rpc",
    ) -> tuple[SetupState, bool]:
        """Save progress, latch observed configuration, or explicitly leave setup.

        An explicit exit needs no configured Provider or Model. An already
        completed or skipped state never returns to pending; reopening only
        changes the saved step. A completed state is never demoted to skipped.
        """
        if step is not None and step not in SETUP_STEPS:
            raise ValueError("Unknown setup step")
        with self._lock:
            previous = self._read()
            status = previous.status
            if configured:
                status = "completed"
            elif finish and status == "pending":
                status = "skipped"
            current = replace(
                previous,
                status=status,
                step=step if step is not None else previous.step,
                agent_id=agent_id if agent_id is not None else previous.agent_id,
            )
            if current == previous:
                return current, False
            try:
                write_json_document(self.path, asdict(current), _FORMAT)
            except (JsonConfigValidationError, JsonDocumentWriteError, OSError) as error:
                raise StorageError(f"The setup progress could not be saved: {error}") from error
        if current.status != previous.status:
            _LOGGER.info("First setup ended (status=%s actor=%s)", current.status, actor)
        return current, True
