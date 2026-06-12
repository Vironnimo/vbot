"""Central task-model bindings, targets, and discovery service."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, cast

from core.model_tasks.constants import SUPPORTED_TASK_TYPES
from core.model_tasks.local_targets import (
    DEFAULT_LOCAL_TASK_TARGET_REGISTRY,
    LocalTaskTargetRegistry,
)
from core.model_tasks.options import TaskModelOptionSchema, option_schema_for
from core.models import ModelQuery
from core.providers.accounts import compose_connection_id, validate_account_id
from core.utils.errors import ConfigError, VBotError

JsonObject = dict[str, Any]
TaskModelTargetKind = Literal["provider", "local"]


class TaskModelError(VBotError):
    """Base class for expected task-model errors."""


class TaskModelValidationError(TaskModelError):
    """Raised when a task-model payload or target id is malformed."""


@dataclass(frozen=True)
class TaskModelBinding:
    """Persisted binding from a task type to one concrete target."""

    task_type: str
    target: str
    options: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> JsonObject:
        return {
            "target": self.target,
            "options": dict(self.options),
        }


@dataclass(frozen=True)
class TaskModelTargetRef:
    """Parsed task-model target id."""

    kind: TaskModelTargetKind
    target: str
    provider_id: str = ""
    model_id: str = ""
    connection_id: str = ""
    local_connection_id: str = ""
    account_id: str = ""
    local_id: str = ""


@dataclass(frozen=True)
class TaskModelTarget:
    """Client-facing task-model target descriptor."""

    id: str
    kind: TaskModelTargetKind
    label: str
    task_types: tuple[str, ...]
    usable: bool
    provider_id: str = ""
    model_id: str = ""
    connection_id: str = ""
    connection_label: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> JsonObject:
        return {
            "id": self.id,
            "kind": self.kind,
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "connection_id": self.connection_id,
            "connection_label": self.connection_label,
            "label": self.label,
            "task_types": list(self.task_types),
            "usable": self.usable,
            "metadata": dict(self.metadata),
        }


def validate_task_type(task_type: str) -> str:
    """Return a normalized task type or raise a validation error."""

    if not isinstance(task_type, str) or task_type not in SUPPORTED_TASK_TYPES:
        allowed = ", ".join(sorted(SUPPORTED_TASK_TYPES))
        raise TaskModelValidationError(f"Unsupported task type '{task_type}'. Supported: {allowed}")
    return task_type


def parse_task_model_target_id(target: str) -> TaskModelTargetRef:
    """Parse a provider or local task-model target id.

    Provider targets use ``provider/model::connection[:account]``; the
    connection part may also carry a redundant ``provider:`` prefix.
    """

    if not isinstance(target, str) or not target.strip():
        raise TaskModelValidationError("Task model target must be a non-empty string")

    normalized_target = target.strip()
    if normalized_target.startswith("local/"):
        local_id = normalized_target.removeprefix("local/")
        if not local_id or "::" in local_id or "/" in local_id:
            raise TaskModelValidationError(f"Invalid local task model target: {target}")
        return TaskModelTargetRef(kind="local", target=normalized_target, local_id=local_id)

    model_part, separator, connection_part = normalized_target.partition("::")
    if separator != "::" or not connection_part:
        raise TaskModelValidationError(
            "Provider task model targets must include a connection suffix, "
            "for example openrouter/openai/gpt-4o-transcribe::api-key"
        )

    provider_id, slash, model_id = model_part.partition("/")
    if slash != "/" or not provider_id or not model_id:
        raise TaskModelValidationError(f"Invalid provider task model target: {target}")

    local_connection_id = connection_part
    provider_prefix = f"{provider_id}:"
    if local_connection_id.startswith(provider_prefix):
        local_connection_id = local_connection_id.removeprefix(provider_prefix)
    if not local_connection_id:
        raise TaskModelValidationError(f"Invalid provider task model target: {target}")

    account_id = ""
    bare_connection_id, account_separator, account_part = local_connection_id.partition(":")
    if account_separator:
        if not bare_connection_id:
            raise TaskModelValidationError(f"Invalid provider task model target: {target}")
        try:
            account_id = validate_account_id(account_part)
        except ConfigError as error:
            raise TaskModelValidationError(
                f"Invalid account id in task model target '{target}': {error}"
            ) from error
        local_connection_id = bare_connection_id

    return TaskModelTargetRef(
        kind="provider",
        target=normalized_target,
        provider_id=provider_id,
        model_id=model_id,
        connection_id=compose_connection_id(provider_id, local_connection_id, account_id or None),
        local_connection_id=local_connection_id,
        account_id=account_id,
    )


def public_provider_target_id(provider_id: str, model_id: str, local_connection_id: str) -> str:
    """Return the settings-facing provider target id."""

    return f"{provider_id}/{model_id}::{local_connection_id}"


class TaskModelService:
    """Resolve specialized task-model settings, targets, and option schemas."""

    def __init__(
        self,
        providers: Any,
        models: Any,
        credentials: Any,
        storage: Any,
        *,
        local_targets: LocalTaskTargetRegistry | None = None,
    ) -> None:
        self._providers = providers
        self._models = models
        self._credentials = credentials
        self._storage = storage
        self._local_targets = local_targets or DEFAULT_LOCAL_TASK_TARGET_REGISTRY

    def settings(self) -> JsonObject:
        """Return normalized persisted task-model settings."""

        return cast(JsonObject, self._storage.load_model_task_settings())

    def update(self, model_tasks: Mapping[str, Any]) -> JsonObject:
        """Persist task-model settings and return the normalized section."""

        return cast(JsonObject, self._storage.update_model_task_settings(model_tasks))

    def binding_for(self, task_type: str) -> TaskModelBinding:
        """Return the configured binding for *task_type*."""

        normalized_task_type = validate_task_type(task_type)
        settings = self.settings()
        binding = settings.get(normalized_task_type)
        if not isinstance(binding, Mapping):
            raise TaskModelError(f"No task model configured for {normalized_task_type}")
        target = binding.get("target")
        if not isinstance(target, str) or not target:
            raise TaskModelError(f"No task model configured for {normalized_task_type}")
        options = binding.get("options")
        return TaskModelBinding(
            task_type=normalized_task_type,
            target=target,
            options=dict(options) if isinstance(options, Mapping) else {},
        )

    def list_targets(self, task_type: str) -> list[TaskModelTarget]:
        """Return usable provider and local targets for *task_type*."""

        normalized_task_type = validate_task_type(task_type)
        targets: list[TaskModelTarget] = []
        targets.extend(self._provider_targets(normalized_task_type))
        targets.extend(self._local_task_targets(normalized_task_type))
        return sorted(targets, key=lambda target: (target.kind, target.label.lower(), target.id))

    def options(self, task_type: str, target: str) -> TaskModelOptionSchema:
        """Return the backend-owned option schema for a target.

        Provider targets get their option schema from the task/provider
        defaults in :mod:`core.model_tasks.options`, which is now
        model-aware: we resolve the target's :class:`core.models.Model`
        from the injected registry and pass its capabilities (voice list,
        supported parameters) and ``model_id`` (for family-specific
        Recraft/Sourceful image profiles and aspect-ratio/image-size
        exceptions) to the schema builder. If the model is missing from
        the registry we fall back to the provider-level conservative
        schema that the model-aware branches extend.

        Local targets get the schema declared by the registered
        descriptor — future user-configured local engines advertise their
        own fields through ``LocalTaskTargetDescriptor.option_fields`` so
        the Settings UI can render them generically.
        """

        normalized_task_type = validate_task_type(task_type)
        target_ref = parse_task_model_target_id(target)
        if target_ref.kind == "local":
            descriptor = self._local_targets.get(target_ref.local_id)
            return TaskModelOptionSchema(
                task_type=normalized_task_type,
                target=target_ref.target,
                fields=descriptor.option_fields,
            )
        model = self._resolve_model(target_ref.provider_id, target_ref.model_id)
        return option_schema_for(
            normalized_task_type,
            target_ref.provider_id,
            target_ref.target,
            model=model,
        )

    def _resolve_model(self, provider_id: str, model_id: str) -> Any | None:
        """Return the registry's ``Model`` for *(provider_id, model_id)*, or ``None``.

        Returns ``None`` when the registry has no ``get`` method (test
        double missing the seam) or when the lookup raises ``KeyError``
        (model not in the catalog, e.g. an override-only entry that has
        not been refreshed yet). The model-aware schema builder treats
        ``None`` as "fall back to provider-level conservative defaults".
        """

        get_model = getattr(self._models, "get", None)
        if not callable(get_model):
            return None
        try:
            return get_model(provider_id, model_id)
        except KeyError:
            return None

    def options_with_defaults(self, binding: TaskModelBinding) -> JsonObject:
        """Return binding options merged over schema defaults."""

        schema = self.options(binding.task_type, binding.target)
        return {**schema.default_options(), **dict(binding.options)}

    def _provider_targets(self, task_type: str) -> list[TaskModelTarget]:
        """Return provider targets for *task_type*, filtered by capability and credentials.

        Capability / task matching is delegated to the shared :class:`ModelQuery`
        in :mod:`core.models` so that the same vocabulary and matching rules
        are used by ``model.list`` and ``task_model.list_targets``. This
        method owns the provider-side concerns: credential gating (which
        connections are usable) and multi-connection expansion (one target
        per usable connection). The query itself is credential-free.
        """

        targets: list[TaskModelTarget] = []
        for provider_id in self._providers.list_ids():
            provider = self._providers.get(provider_id)
            usable_connections = [
                connection
                for connection in provider.connections
                if self._credentials.has_credentials(provider_id, f"{provider_id}:{connection.id}")
            ]
            if not usable_connections:
                continue

            multiple_connections = len(usable_connections) > 1
            for matched_provider_id, model in self._models.query(
                ModelQuery(provider_id=provider_id, tasks=(task_type,))
            ):
                for connection in usable_connections:
                    if model.connections and connection.id not in model.connections:
                        continue
                    connection_label = getattr(connection, "label", connection.id)
                    label = f"{provider.name} / {model.name}"
                    if multiple_connections:
                        label = f"{label} ({connection_label})"
                    targets.append(
                        TaskModelTarget(
                            id=public_provider_target_id(
                                matched_provider_id, model.model_id, connection.id
                            ),
                            kind="provider",
                            provider_id=matched_provider_id,
                            model_id=model.model_id,
                            connection_id=f"{matched_provider_id}:{connection.id}",
                            connection_label=connection_label,
                            label=label,
                            task_types=tuple(model.capabilities.task_types),
                            usable=True,
                        )
                    )
        return targets

    def _local_task_targets(self, task_type: str) -> list[TaskModelTarget]:
        return [
            TaskModelTarget(
                id=descriptor.public_id,
                kind="local",
                label=descriptor.label,
                task_types=descriptor.task_types,
                usable=descriptor.usable,
                metadata=descriptor.metadata or {},
            )
            for descriptor in self._local_targets.list_for_task(task_type)
        ]
