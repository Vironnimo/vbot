"""Registration-bound public application operations and resource observations.

The application edge injects an explicit catalog of its public operations. This
module owns invocation and observer lifetime, never transport or domain policy.
"""

from __future__ import annotations

import asyncio
import copy
import threading
from collections.abc import Awaitable, Callable, Iterable, Mapping
from contextlib import suppress
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from jsonschema import Draft202012Validator

from core.extensions._callbacks import invoke_isolated_handler
from core.utils.logging import get_logger

if TYPE_CHECKING:
    from core.extensions._declarations import ExtensionRegistrationIdentity

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
        self, resources: Iterable[str], handler: Callable[[JsonObject], Any]
    ) -> Callable[[], None]:
        self._require_current()
        return self._catalog.subscribe(self._owner, self._require_current, resources, handler)


class ExtensionServiceCatalog:
    """Application-owned catalog with bounded, owner-bound observer dispatch."""

    def __init__(self) -> None:
        self._services: dict[str, tuple[ExtensionService, Draft202012Validator]] = {}
        self._resources = _CORE_RESOURCES
        self._binding: object | None = None
        self._subscriptions: set[_Subscription] = set()
        self._lock = threading.RLock()
        self._revision = 0

    def for_owner(
        self, owner: ExtensionRegistrationIdentity, require_current: Callable[[], None]
    ) -> ExtensionServices:
        return ExtensionServices(self, owner, require_current)

    def bind(
        self, services: Iterable[ExtensionService], *, resources: Iterable[str] = ()
    ) -> Callable[[], None]:
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
        self._services = candidate
        self._resources = frozenset(resource_names)
        self._binding = binding

        def unbind() -> None:
            if self._binding is binding:
                self._services = {}
                self._binding = None

        return unbind

    def describe(self) -> JsonObject:
        return {
            "available": self._binding is not None,
            "services": [
                {
                    "name": service.name,
                    "kind": service.kind,
                    "parameters": copy.deepcopy(service.parameters),
                    "resources": list(service.resources),
                }
                for service, _ in sorted(self._services.values(), key=lambda item: item[0].name)
            ],
            "resources": sorted(self._resources),
        }

    async def invoke(self, kind: ServiceKind, name: str, params: JsonObject | None) -> JsonObject:
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
        if not validator.is_valid(arguments):
            raise ExtensionServiceError(
                "invalid_request", "Service parameters do not match its schema"
            )
        binding = self._binding
        result = await service.handler(arguments)
        if kind == "query" and self._binding is not binding:
            raise ExtensionServiceError(
                "service_unavailable", "Application services changed during the read"
            )
        return copy.deepcopy(result)

    def subscribe(
        self,
        owner: ExtensionRegistrationIdentity,
        require_current: Callable[[], None],
        resources: Iterable[str],
        handler: Callable[[JsonObject], Any],
    ) -> Callable[[], None]:
        if isinstance(resources, str) or not callable(handler):
            raise ValueError("Supply resource names and an observer callback")
        selected = frozenset(resources)
        if not selected or not selected <= self._resources:
            raise ValueError("Subscription names an unavailable resource")
        subscription = _Subscription(self, owner, require_current, selected, handler)
        with self._lock:
            self._subscriptions.add(subscription)
        return subscription.close

    def publish(self, resource: str, scope: Mapping[str, Any] | None = None) -> None:
        """Publish an invalidation from any thread, retaining no domain content."""
        if resource not in self._resources:
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

    def close(self) -> None:
        with self._lock:
            for subscription in tuple(self._subscriptions):
                subscription.close()
            self._services = {}
            self._binding = None


class _Subscription:
    def __init__(
        self,
        catalog: ExtensionServiceCatalog,
        owner: ExtensionRegistrationIdentity,
        require_current: Callable[[], None],
        resources: frozenset[str],
        handler: Callable[[JsonObject], Any],
    ) -> None:
        self.catalog = catalog
        self.owner = owner
        self.require_current = require_current
        self.resources = resources
        self.handler = handler
        self.loop = asyncio.get_running_loop()
        self.pending: dict[str, JsonObject] = {}
        self.scheduled = False
        self.closed = False
        self.task: asyncio.Task[None] | None = None

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
                self.loop.call_soon_threadsafe(self.start)
            except RuntimeError:
                # Shutdown may close the subscriber's loop before a late
                # domain worker has delivered its last invalidation.
                self.close()

    def start(self) -> None:
        with self.catalog._lock:
            if self.closed:
                return
            self.task = self.loop.create_task(
                self.drain(), name=f"extension-observer:{self.owner.name}"
            )

    async def drain(self) -> None:
        try:
            while True:
                with self.catalog._lock:
                    if self.closed or not self.pending:
                        self.scheduled = False
                        return
                    resource = next(iter(self.pending))
                    event = self.pending.pop(resource)
                self.require_current()
                token = _CURRENT_SUBSCRIPTION.set(self)
                try:
                    await invoke_isolated_handler(
                        self.handler,
                        event,
                        extension_name=self.owner.name,
                        description="resource observer",
                        timeout_seconds=_OBSERVER_DEADLINE_SECONDS,
                    )
                finally:
                    _CURRENT_SUBSCRIPTION.reset(token)
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.error(
                "Extension observer stopped (extension=%s)", self.owner.name, exc_info=True
            )
            self.close()
        finally:
            self.task = None

    def close(self) -> None:
        with self.catalog._lock:
            if self.closed:
                return
            self.closed = True
            self.pending.clear()
            self.catalog._subscriptions.discard(self)
            task = self.task
        # A callback may disable its own Extension through a service action.
        # Retire it immediately, but do not cancel/await its own parent drain;
        # that admitted callback can return under the existing deadline.
        if task is not None and not task.done() and _CURRENT_SUBSCRIPTION.get() is not self:
            # Its loop may already have closed during Runtime shutdown.
            with suppress(RuntimeError):
                self.loop.call_soon_threadsafe(task.cancel)
