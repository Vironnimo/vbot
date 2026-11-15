"""Registration-bound public application operations and resource observations.

The application edge injects an explicit catalog of its public operations. This
module owns invocation and observer lifetime, never transport or domain policy.
"""

from __future__ import annotations

import asyncio
import copy
import re
import threading
from collections.abc import Awaitable, Callable, Iterable, Mapping
from contextlib import AbstractContextManager, nullcontext, suppress
from contextvars import Context, ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from core.extensions._callbacks import (
    ExtensionHandlerTimeoutError,
    invoke_isolated_handler,
    wait_for_extension_callbacks,
)
from core.extensions._service_exports import (
    ExportedResource,
    ExportedService,
    detached_json,
    guarded_handler,
    local_name,
    qualified_name,
)
from core.utils.log_conditions import LoggedConditions
from core.utils.logging import get_logger

if TYPE_CHECKING:
    from core.extensions._declarations import ExtensionRegistrationIdentity
    from core.extensions.extensions import ExtensionRegistry

type JsonObject = dict[str, Any]
type ServiceKind = Literal["query", "execute"]

_LOGGER = get_logger("extensions.services")
_OBSERVER_DEADLINE_SECONDS = 5.0
_CORE_RESOURCES = frozenset({"runs", "sessions", "queue"})
_CURRENT_SUBSCRIPTION: ContextVar[_Subscription | None] = ContextVar(
    "extension_resource_subscription", default=None
)


class ExtensionServiceError(ValueError):
    """An expected public-service refusal with a transport-independent code."""

    code: str
    data: Any

    def __init__(self, code: str, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.data = data


@dataclass(frozen=True)
class ExtensionService:
    """One explicitly published operation, implemented by its existing owner."""

    name: str
    kind: ServiceKind
    parameters: JsonObject
    handler: Callable[[JsonObject], Awaitable[JsonObject]]
    resources: tuple[str, ...] = ()


def _parameter_error(error: ValidationError) -> ExtensionServiceError:
    """Describe the offending field and constraint without repeating its value."""
    path = list(error.absolute_path)
    if isinstance(error.instance, dict):
        if error.validator == "required" and isinstance(error.validator_value, list):
            missing = sorted(set(error.validator_value) - error.instance.keys())
            if missing:
                path.append(missing[0])
        elif (
            error.validator == "additionalProperties"
            and error.validator_value is False
            and isinstance(error.schema, Mapping)
        ):
            properties = error.schema.get("properties", {})
            patterns = error.schema.get("patternProperties", {})
            unknown = sorted(
                key
                for key in error.instance
                if key not in properties
                and not any(re.search(pattern, key) for pattern in patterns)
            )
            if unknown:
                path.append(unknown[0])
    pointer = "/" + "/".join(str(part).replace("~", "~0").replace("/", "~1") for part in path)
    return ExtensionServiceError(
        "invalid_request",
        f"Service parameter {pointer} does not satisfy {error.validator}",
        {
            "path": path,
            "schema_path": list(error.absolute_schema_path),
            "validator": error.validator,
        },
    )


class ExtensionSubscription:
    """An idempotent disposer with an observable, detached delivery-health snapshot."""

    def __init__(self, subscription: _Subscription) -> None:
        self._subscription = subscription

    def __call__(self) -> None:
        self.close()

    def close(self) -> None:
        self._subscription.close()

    @property
    def status(self) -> JsonObject:
        return self._subscription.status()


class ExtensionServices:
    """One loaded Extension's operator-integration API.

    These calls do not act as a Tool or acquire a human/device identity. Tool
    implementations keep their ToolContext and its sanctioned delegation APIs.
    Observations are coalesced invalidations: reread the resource after delivery.
    """

    def __init__(
        self,
        catalog: ExtensionServiceCatalog,
        owner: ExtensionRegistrationIdentity,
        require_current: Callable[[], None],
    ) -> None:
        self._catalog = catalog
        self._owner = owner
        self._require_current = require_current

    def describe(self) -> JsonObject:
        self._require_current()
        return self._catalog.describe()

    async def query(self, name: str, params: JsonObject | None = None) -> JsonObject:
        self._require_current()
        result = await self._catalog.invoke("query", name, params)
        self._require_current()
        return result

    async def execute(self, name: str, params: JsonObject | None = None) -> JsonObject:
        self._require_current()
        # An admitted mutation may disable its own Extension. Preserve its
        # definitive outcome instead of reporting failure after it committed.
        return await self._catalog.invoke("execute", name, params)

    def subscribe(
        self,
        resources: Iterable[str],
        handler: Callable[[JsonObject], Any],
        *,
        on_error: Callable[[JsonObject], Any] | None = None,
    ) -> ExtensionSubscription:
        self._require_current()
        return self._catalog.subscribe(
            self._owner, self._require_current, resources, handler, on_error=on_error
        )

    def publish(self, resource: str, scope: JsonObject | None = None) -> None:
        """Invalidate one resource declared by this exact Extension registration."""
        self._require_current()
        self._catalog.publish_export(self._owner, resource, scope)


class ExtensionServiceCatalog:
    """Application-owned catalog with bounded, owner-bound observer dispatch."""

    def __init__(self) -> None:
        self._services: dict[str, tuple[ExtensionService, Draft202012Validator]] = {}
        self._resources = _CORE_RESOURCES
        self._binding: object | None = None
        self._application_resources = _CORE_RESOURCES
        self._exported_services: dict[str, ExportedService] = {}
        self._exported_resources: dict[str, ExportedResource] = {}
        self._exclusions: dict[str, str] = {}
        self._publish_export: Callable[[JsonObject], None] | None = None
        self._export_context: Callable[[], AbstractContextManager[None]] = nullcontext
        self._subscriptions: set[_Subscription] = set()
        self._lock = threading.RLock()
        self._revision = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._closed = False
        self._conditions = LoggedConditions()

    def _capture_loop(self) -> None:
        """Bind observation delivery before callbacks enter a synchronous worker."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        with self._lock:
            if self._loop is not None and self._loop is not loop and not self._loop.is_closed():
                raise RuntimeError("Application services already belong to another Event Loop")
            self._loop = loop

    def for_owner(
        self, owner: ExtensionRegistrationIdentity, require_current: Callable[[], None]
    ) -> ExtensionServices:
        self._capture_loop()
        return ExtensionServices(self, owner, require_current)

    def bind(
        self,
        services: Iterable[ExtensionService],
        *,
        resources: Iterable[str] = (),
        exclusions: Mapping[str, str] | None = None,
        publish_export: Callable[[JsonObject], None] | None = None,
        export_context: Callable[[], AbstractContextManager[None]] | None = None,
    ) -> Callable[[], None]:
        self._capture_loop()
        candidate = {}
        resource_names = set(_CORE_RESOURCES) | set(resources)
        for service in services:
            if (
                not service.name
                or service.name in candidate
                or service.kind not in {"query", "execute"}
            ):
                raise ValueError("Service names must be unique and kinds must be query or execute")
            Draft202012Validator.check_schema(service.parameters)
            declared = ExtensionService(
                service.name,
                service.kind,
                copy.deepcopy(service.parameters),
                service.handler,
                tuple(service.resources),
            )
            candidate[service.name] = (declared, Draft202012Validator(declared.parameters))
            resource_names.update(service.resources)
        if any(not isinstance(name, str) or not name for name in resource_names):
            raise ValueError("Resource names must be non-empty strings")
        binding = object()
        with self._lock:
            self._services = candidate
            self._application_resources = frozenset(resource_names)
            self._resources = self._application_resources | self._exported_resources.keys()
            self._binding = binding
            self._exclusions = dict(exclusions or {})
            self._publish_export = publish_export
            self._export_context = export_context or nullcontext

        def unbind() -> None:
            with self._lock:
                if self._binding is binding:
                    self._services = {}
                    self._binding = None
                    self._application_resources = _CORE_RESOURCES
                    self._resources = _CORE_RESOURCES | self._exported_resources.keys()
                    self._exclusions = {}
                    self._publish_export = None
                    self._export_context = nullcontext

        return unbind

    def describe(self) -> JsonObject:
        return {
            "available": self._binding is not None or bool(self._exported_services),
            "services": sorted(
                [
                    {
                        "name": service.name,
                        "kind": service.kind,
                        "parameters": copy.deepcopy(service.parameters),
                        "resources": list(service.resources),
                    }
                    for service, _ in sorted(self._services.values(), key=lambda item: item[0].name)
                ]
                + [item.describe(name) for name, item in self._exported_services.items()],
                key=lambda entry: entry["name"],
            ),
            "resources": sorted(self._resources),
            "resource_definitions": [
                item.describe(name) for name, item in sorted(self._exported_resources.items())
            ],
            "excluded_services": [
                {"name": name, "reason": reason}
                for name, reason in sorted(self._exclusions.items())
            ],
        }

    async def invoke(self, kind: ServiceKind, name: str, params: JsonObject | None) -> JsonObject:
        if isinstance(name, str) and name in self._exported_services:
            return await self._invoke_export(kind, name, params)
        if self._binding is None:
            raise ExtensionServiceError(
                "service_unavailable", "Application services are unavailable"
            )
        if not isinstance(name, str) or name not in self._services:
            raise ExtensionServiceError("service_not_found", "Unknown application service")
        service, validator = self._services[name]
        if kind != service.kind:
            raise ExtensionServiceError(
                "service_kind_mismatch", "Service invocation kind does not match"
            )
        supplied = {} if params is None else params
        if not isinstance(supplied, dict):
            raise ExtensionServiceError("invalid_request", "Service parameters must be an object")
        arguments = copy.deepcopy(supplied)
        errors = sorted(
            validator.iter_errors(arguments),
            key=lambda error: (
                tuple(str(part) for part in error.absolute_path),
                tuple(str(part) for part in error.absolute_schema_path),
            ),
        )
        if errors:
            raise _parameter_error(errors[0])
        binding = self._binding
        result = await service.handler(arguments)
        if kind == "query" and self._binding is not binding:
            raise ExtensionServiceError(
                "service_unavailable", "Application services changed during the read"
            )
        return copy.deepcopy(result)

    def install_exports(self, registry: ExtensionRegistry | None) -> None:
        """Publish all registrations before the first startup callback can consume them."""
        self._capture_loop()
        services: dict[str, ExportedService] = {}
        resources: dict[str, ExportedResource] = {}
        if registry is not None:
            for record in registry.records():
                if record.status != "loaded":
                    continue
                owner = registry.registration_identity(record.name)

                def require_current(identity: ExtensionRegistrationIdentity = owner) -> None:
                    if not registry.is_registration_current(identity):
                        raise ExtensionServiceError(
                            "service_unavailable", "Extension service retired"
                        )

                def require_service_current(
                    identity: ExtensionRegistrationIdentity = owner,
                ) -> None:
                    if not registry.is_service_registration_current(identity):
                        raise ExtensionServiceError(
                            "service_unavailable", "Extension service retired"
                        )

                for resource in record.declarations.resources:
                    name = qualified_name(owner.name, resource.name)
                    resources[name] = ExportedResource(
                        owner,
                        resource,
                        Draft202012Validator(resource.scope_schema)
                        if resource.scope_schema is not None
                        else None,
                        require_current,
                    )
                for service in record.declarations.services:
                    name = qualified_name(owner.name, service.name)
                    services[name] = ExportedService(
                        owner,
                        service,
                        tuple(qualified_name(owner.name, item) for item in service.resources),
                        Draft202012Validator(service.parameters),
                        require_service_current,
                    )
        with self._lock:
            self._exported_services = services
            self._exported_resources = resources
            self._resources = self._application_resources | resources.keys()

    async def _invoke_export(
        self, kind: ServiceKind, name: str, params: JsonObject | None
    ) -> JsonObject:
        exported = self._exported_services[name]
        exported.require_current()
        declaration = exported.declaration
        if kind != declaration.kind:
            raise ExtensionServiceError(
                "service_kind_mismatch", "Service invocation kind does not match"
            )
        arguments = {} if params is None else params
        if not isinstance(arguments, dict):
            raise ExtensionServiceError("invalid_request", "Service parameters must be an object")
        try:
            arguments = detached_json(arguments)
        except ValueError as exc:
            raise ExtensionServiceError("invalid_request", str(exc)) from exc
        error = next(exported.validator.iter_errors(arguments), None)
        if error is not None:
            raise _parameter_error(error)

        def require_current() -> None:
            exported.require_current()
            if self._exported_services.get(name) is not exported:
                raise ExtensionServiceError("service_unavailable", "Extension service replaced")

        with self._export_context():
            result = await invoke_isolated_handler(
                guarded_handler(declaration.handler, require_current),
                arguments,
                extension_name=exported.owner.name,
                description=f"service {name}",
            )
        if kind == "query":
            exported.require_current()
            if self._exported_services.get(name) is not exported:
                raise ExtensionServiceError(
                    "service_unavailable", "Extension service changed during the read"
                )
        if not isinstance(result, dict):
            raise ExtensionServiceError("invalid_result", "Service result must be a JSON object")
        try:
            return dict(detached_json(result, limit=8 * 1024 * 1024))
        except ValueError as exc:
            raise ExtensionServiceError("invalid_result", str(exc)) from exc

    def publish_export(
        self, owner: ExtensionRegistrationIdentity, resource: str, scope: JsonObject | None
    ) -> None:
        name = qualified_name(owner.name, local_name(resource))
        with self._lock:
            exported = self._exported_resources.get(name)
            if exported is None or exported.owner != owner:
                raise ExtensionServiceError(
                    "resource_not_found", "Resource is not declared by this registration"
                )
            exported.require_current()
            if scope is not None and not isinstance(scope, dict):
                raise ExtensionServiceError("invalid_request", "Resource scope must be an object")
            try:
                value = detached_json(scope, limit=64 * 1024) if scope is not None else None
            except ValueError as exc:
                raise ExtensionServiceError("invalid_request", str(exc)) from exc
            if exported.validator is not None and value is not None:
                error = next(exported.validator.iter_errors(value), None)
                if error is not None:
                    raise _parameter_error(error)
            self.publish(name, value)
            if self._publish_export is not None:
                # Browser invalidations contain public declarations only. Scope
                # belongs to Python observers and may contain private metadata.
                self._publish_export(
                    {
                        "owner": owner.name,
                        "epoch": owner.epoch,
                        "resource": name,
                        "local_resource": exported.declaration.name,
                        "revision": self._revision,
                        "services": sorted(
                            service_name
                            for service_name, service in self._exported_services.items()
                            if name in service.resources
                        ),
                    }
                )

    def subscribe(
        self,
        owner: ExtensionRegistrationIdentity,
        require_current: Callable[[], None],
        resources: Iterable[str],
        handler: Callable[[JsonObject], Any],
        *,
        on_error: Callable[[JsonObject], Any] | None = None,
    ) -> ExtensionSubscription:
        if (
            isinstance(resources, str)
            or not callable(handler)
            or (on_error is not None and not callable(on_error))
        ):
            raise ValueError("Supply resource names and an observer callback")
        selected = frozenset(resources)
        self._capture_loop()
        with self._lock:
            require_current()
            if not selected or not selected <= self._resources:
                raise ValueError("Subscription names an unavailable resource")
            loop = self._loop
            if self._closed or loop is None or loop.is_closed() or not loop.is_running():
                raise ExtensionServiceError(
                    "service_unavailable", "Resource observations need a running Runtime Event Loop"
                )
            subscription = _Subscription(
                self, owner, require_current, selected, handler, loop, on_error
            )
            self._subscriptions.add(subscription)
        return ExtensionSubscription(subscription)

    def publish(self, resource: str, scope: Mapping[str, Any] | None = None) -> None:
        """Publish an invalidation from any thread, retaining no domain content."""
        if self._closed or resource not in self._resources:
            return
        with self._lock:
            self._revision += 1
            event: JsonObject = {"kind": resource, "revision": self._revision}
            if scope:
                event["scope"] = copy.deepcopy(dict(scope))
            for subscription in tuple(self._subscriptions):
                subscription.enqueue(event)

    async def release_owner(self, owner: ExtensionRegistrationIdentity) -> None:
        with self._lock:
            selected = [item for item in self._subscriptions if item.owner == owner]
        active = _CURRENT_SUBSCRIPTION.get()
        tasks = [item.task for item in selected if item.task is not None and item is not active]
        for item in selected:
            item.close()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        with self._lock:
            self._exported_services = {
                name: item for name, item in self._exported_services.items() if item.owner != owner
            }
            self._exported_resources = {
                name: item for name, item in self._exported_resources.items() if item.owner != owner
            }
            self._resources = self._application_resources | self._exported_resources.keys()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            for subscription in tuple(self._subscriptions):
                subscription.close()
            self._services = {}
            self._binding = None
            self._exported_services = {}
            self._exported_resources = {}
            self._resources = _CORE_RESOURCES
            self._publish_export = None
            self._export_context = nullcontext


class _Subscription:
    def __init__(
        self,
        catalog: ExtensionServiceCatalog,
        owner: ExtensionRegistrationIdentity,
        require_current: Callable[[], None],
        resources: frozenset[str],
        handler: Callable[[JsonObject], Any],
        loop: asyncio.AbstractEventLoop,
        on_error: Callable[[JsonObject], Any] | None,
    ) -> None:
        self.catalog = catalog
        self.owner = owner
        self.require_current = require_current
        self.resources = resources
        self.handler = handler
        self.on_error = on_error
        self.loop = loop
        self.pending: dict[str, JsonObject] = {}
        self.scheduled = False
        self.closed = False
        self.task: asyncio.Task[None] | None = None
        self.failures = 0
        self.consecutive_failures = 0
        self.last_error: JsonObject | None = None

    def status(self) -> JsonObject:
        with self.catalog._lock:
            return {
                "state": "closed"
                if self.closed
                else "degraded"
                if self.consecutive_failures
                else "active",
                "failures": self.failures,
                "consecutive_failures": self.consecutive_failures,
                "last_error": copy.deepcopy(self.last_error),
            }

    def enqueue(self, event: JsonObject) -> None:
        # Called while the catalog lock is held. At most one pending event per
        # resource and one wake-up per observer, even when a worker floods it.
        resource = event["kind"]
        if self.closed or resource not in self.resources:
            return
        previous = self.pending.get(resource)
        event = copy.deepcopy(event)
        if previous is not None and previous.get("scope") != event.get("scope"):
            event.pop("scope", None)
        self.pending[resource] = event
        if not self.scheduled:
            self.scheduled = True
            try:
                self.loop.call_soon_threadsafe(self.start, context=Context())
            except RuntimeError:
                # Shutdown may close the subscriber's loop before a late
                # domain worker has delivered its last invalidation.
                self.close()

    def start(self) -> None:
        with self.catalog._lock:
            if self.closed:
                return
            self.task = self.loop.create_task(
                self.drain(), name=f"extension-observer:{self.owner.name}", context=Context()
            )

    async def drain(self) -> None:
        try:
            while True:
                with self.catalog._lock:
                    if self.closed or not self.pending:
                        self.scheduled = False
                        return
                # A prior deadline may leave admitted work settling. Preserve
                # coalesced events until it finishes, without a polling loop.
                await wait_for_extension_callbacks(self.owner.name)
                with self.catalog._lock:
                    if self.closed:
                        return
                    resource = next(iter(self.pending))
                    event = self.pending.pop(resource)
                try:
                    self.require_current()
                except ValueError:
                    self.close()
                    return
                token = _CURRENT_SUBSCRIPTION.set(self)
                try:
                    await invoke_isolated_handler(
                        self.handler,
                        event,
                        extension_name=self.owner.name,
                        description="resource observer",
                        timeout_seconds=_OBSERVER_DEADLINE_SECONDS,
                    )
                except Exception as error:
                    diagnostic = self.record_failure(event, error)
                    if self.on_error is not None and not self.closed:
                        await wait_for_extension_callbacks(self.owner.name)
                        if not self.closed:
                            await self.report_failure(diagnostic)
                else:
                    self.record_success()
                finally:
                    _CURRENT_SUBSCRIPTION.reset(token)
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.exception("Extension observer dispatch failed (extension=%s)", self.owner.name)
            self.close()
        finally:
            self.task = None

    def record_failure(self, event: JsonObject, error: Exception) -> JsonObject:
        timeout = isinstance(error, ExtensionHandlerTimeoutError)
        diagnostic: JsonObject = {
            "code": "callback_timeout" if timeout else "callback_failed",
            "error_type": type(error).__name__,
            "resource": event["kind"],
            "revision": event["revision"],
        }
        with self.catalog._lock:
            self.failures += 1
            self.consecutive_failures += 1
            self.last_error = diagnostic
        if self.catalog._conditions.started(self, (diagnostic["code"], type(error))):
            if timeout:
                _LOGGER.warning("Extension observer timed out (extension=%s)", self.owner.name)
            else:
                _LOGGER.error(
                    "Extension observer failed (extension=%s)",
                    self.owner.name,
                    exc_info=(type(error), error, error.__traceback__),
                )
        return copy.deepcopy(diagnostic)

    def record_success(self) -> None:
        with self.catalog._lock:
            self.consecutive_failures = 0
            self.last_error = None
        if self.catalog._conditions.ended(self):
            _LOGGER.info("Extension observer recovered (extension=%s)", self.owner.name)

    async def report_failure(self, diagnostic: JsonObject) -> None:
        assert self.on_error is not None
        try:
            await invoke_isolated_handler(
                self.on_error,
                diagnostic,
                extension_name=self.owner.name,
                description="resource observer error callback",
                timeout_seconds=_OBSERVER_DEADLINE_SECONDS,
            )
        except Exception as error:
            if self.catalog._conditions.started((self, "error-handler"), type(error).__name__):
                _LOGGER.error(
                    "Extension observer error callback failed (extension=%s)",
                    self.owner.name,
                    exc_info=True,
                )
        else:
            self.catalog._conditions.ended((self, "error-handler"))

    def close(self) -> None:
        with self.catalog._lock:
            if self.closed:
                return
            self.closed = True
            self.pending.clear()
            self.catalog._subscriptions.discard(self)
            self.catalog._conditions.ended(self)
            self.catalog._conditions.ended((self, "error-handler"))
            task = self.task
        # A callback may disable its own Extension through a service action.
        # Retire it immediately, but do not cancel/await its own parent drain;
        # that admitted callback can return under the existing deadline.
        if task is not None and not task.done() and _CURRENT_SUBSCRIPTION.get() is not self:
            # Its loop may already have closed during Runtime shutdown.
            with suppress(RuntimeError):
                self.loop.call_soon_threadsafe(task.cancel)
