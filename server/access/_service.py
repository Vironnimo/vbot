"""The access service: the access state of one server app and its operations."""

from __future__ import annotations

import asyncio
import secrets
import time
from collections.abc import Callable, Coroutine, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeIs

from core.access.config import AccessConfig
from core.access.devices import (
    DEVICE_IDLE_EXPIRY,
    DEVICE_KINDS,
    Device,
    DeviceNotFoundError,
    DeviceStore,
    DeviceStoreUnavailableError,
    new_device,
    normalize_device_name,
)
from core.access.local_token import is_local_access_token
from core.utils.logging import get_logger
from core.utils.timestamps import format_canonical_timestamp
from core.utils.workers import OrderedWorker
from server.access._connections import LiveConnections
from server.access._limits import RateLimit, RefusalLog, SlidingWindow
from server.access._pairing import (
    LOGIN_TICKET_TTL_SECONDS,
    PAIRING_TTL_SECONDS,
    LoginTickets,
    PairingRegistry,
    PairingRequest,
    format_pairing_code,
)
from server.access._principal import HOST, OPEN, Principal
from server.access._rules import TICKET_PATH_PREFIX, RequestFacts, inspect_request

_LOGGER = get_logger("server.access")
# Device store writes run in submission order, off the Event Loop.
_DEVICE_WRITES = OrderedWorker(name="access-devices")

AUTH_REQUIRED = "auth_required"
ACCESS_DENIED = "access_denied"
RATE_LIMITED = "rate_limited"
AUTH_DISABLED = "auth_disabled"
DEVICES_UNAVAILABLE = "devices_unavailable"
INVALID_REQUEST = "invalid_request"
PAIRING_NOT_FOUND = "pairing_not_found"
DEVICE_NOT_FOUND = "device_not_found"

DEVICE_COOKIE = "vbot_device"
DEVICE_COOKIE_MAX_AGE = 400 * 24 * 3600
PAIRING_POLL_INTERVAL_SECONDS = 2
REFUSAL_LOG_INTERVAL_SECONDS = 60.0
_MAX_SECRET_CHARS = 128


class AccessError(Exception):
    """An access request the server refuses, with its error code and HTTP status."""

    def __init__(
        self, code: str, message: str, *, status: int = 400, retry_after: float | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.retry_after = None if retry_after is None else max(1, round(retry_after))


@dataclass(frozen=True)
class Authentication:
    """The outcome of checking a request's credential.

    ``failure`` names why a presented credential was refused (``authenticated``
    mode only); ``clear_cookie`` asks the response to drop a dead device cookie.
    """

    principal: Principal | None
    failure: str | None = None
    clear_cookie: bool = False


def _limit(per_client: int, overall: int, window: float, clock: Callable[[], float]) -> RateLimit:
    return RateLimit(
        SlidingWindow(per_client, window, clock=clock),
        SlidingWindow(overall, window, clock=clock),
    )


class AccessService:
    """Access mode, device store, pairing, tickets, rate limits and live connections.

    Request-path methods (``inspect``, ``authenticate``) are synchronous and
    memory-only; device store writes run on an ordered worker thread.
    """

    def __init__(
        self,
        config: AccessConfig,
        *,
        local_token: str | None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        utc_now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.config = config
        self._local_token = local_token
        self._admitted = config.admitted_networks()
        self._proxies = config.proxy_networks()
        self._utc_now = utc_now
        self._wall_clock = wall_clock
        self._store: DeviceStore | None = None
        self.pairings = PairingRegistry(clock=clock, wall_clock=wall_clock)
        self.tickets = LoginTickets(clock=clock)
        self.connections = LiveConnections()
        self._pair_start = _limit(10, 100, 600, clock)
        self._pair_poll = _limit(60, 600, 60, clock)
        self._auth_failures = _limit(20, 200, 600, clock)
        self._approval_failures = _limit(10, 50, 600, clock)
        self._refusals: RefusalLog | None = None
        self._background: set[asyncio.Task[None]] = set()
        self._flush_pending = False

    # Lifecycle -------------------------------------------------------------

    async def start(self, data_dir: Path) -> None:
        """Load the device store and remove devices that expired while stopped."""
        store = DeviceStore(data_dir)
        await _DEVICE_WRITES.call_async(store.load)
        self._store = store
        if not store.available:
            _LOGGER.warning(
                "Device store unavailable; no paired device can sign in until it is repaired "
                "(path=%s error=%s)",
                store.path,
                store.error,
            )
            return
        await self._expire(store.expired_devices(now=self._utc_now()))

    async def close(self) -> None:
        """Write remembered device uses; called once at shutdown."""
        for task in list(self._background):
            await asyncio.gather(task, return_exceptions=True)
        store = self._store
        if store is not None and store.available:
            try:
                await _DEVICE_WRITES.call_async(store.flush_usage)
            except OSError as exc:
                _LOGGER.warning("Writing device last use failed (error=%s)", exc)

    # Request path ------------------------------------------------------------

    def inspect(self, scope: Mapping[str, Any]) -> RequestFacts:
        return inspect_request(scope, self.config, admitted=self._admitted, proxies=self._proxies)

    def authenticate(
        self, headers: list[tuple[bytes, bytes]], facts: RequestFacts
    ) -> Authentication:
        """Resolve the request's principal from its credential.

        ``Authorization: Bearer`` wins over the device cookie. Outside
        ``authenticated`` a missing or refused credential still admits the
        request as ``open``: those modes need none.
        """
        enforcing = self.config.mode == "authenticated"
        authorizations = [value for name, value in headers if name == b"authorization"]
        if authorizations:
            text = authorizations[0].decode("latin-1")
            scheme, _separator, token = text.partition(" ")
            token = token.strip()
            if len(authorizations) > 1 or scheme.casefold() != "bearer" or not token:
                return self._refused("malformed_credentials", enforcing)
            if is_local_access_token(token):
                if (
                    self._local_token is not None
                    and facts.direct_loopback
                    and secrets.compare_digest(token, self._local_token)
                ):
                    return Authentication(HOST)
                return self._refused(
                    "invalid_local_token" if facts.direct_loopback else "local_token_not_local",
                    enforcing,
                )
            return self._device(token, facts, enforcing, from_cookie=False)
        cookie = _cookie_value(headers, DEVICE_COOKIE)
        if cookie is not None:
            return self._device(cookie, facts, enforcing, from_cookie=True)
        return Authentication(None if enforcing else OPEN)

    def refused(self, reason: str, facts: RequestFacts, path: str) -> None:
        """Log one refused request, aggregated per reason."""
        if self._refusals is None:
            loop = asyncio.get_running_loop()
            self._refusals = RefusalLog(
                _LOGGER, interval=REFUSAL_LOG_INTERVAL_SECONDS, schedule=loop.call_later
            )
        client = facts.client or facts.peer
        self._refusals.refused(
            reason, client=None if client is None else str(client), detail=f"path={_log_path(path)}"
        )

    def failed_authentication(self, facts: RequestFacts) -> float | None:
        """Count a refused credential; returns the wait once the client is over its limit."""
        key = _client_key(facts)
        wait = self._auth_failures.blocked(key)
        self._auth_failures.record(key)
        return wait

    # Pairing -----------------------------------------------------------------

    def pair_start(self, body: Any, facts: RequestFacts) -> dict[str, Any]:
        self._require_authenticated_mode()
        if not isinstance(body, dict):
            raise AccessError(INVALID_REQUEST, "The body must be a JSON object")
        kind = body.get("kind")
        if kind not in DEVICE_KINDS:
            raise AccessError(INVALID_REQUEST, f"kind must be one of {', '.join(DEVICE_KINDS)}")
        try:
            name = normalize_device_name(body.get("name"))
        except ValueError as exc:
            raise AccessError(INVALID_REQUEST, str(exc)) from None
        self._require_store()
        wait = self._pair_start.acquire(_client_key(facts))
        if wait is not None:
            self.refused("pairing_rate_limited", facts, "/api/auth/pair/start")
            raise AccessError(
                RATE_LIMITED, "Too many pairing requests", status=429, retry_after=wait
            )
        try:
            request, poll_secret = self.pairings.start(
                name=name, kind=kind, client=_client_text(facts)
            )
        except OverflowError:
            raise AccessError(
                RATE_LIMITED, "Too many pending pairing requests", status=429, retry_after=60
            ) from None
        _LOGGER.debug(
            "Pairing requested (request=%s kind=%s client=%s)",
            request.id,
            kind,
            _client_text(facts),
        )
        return {
            "request_id": request.id,
            "code": format_pairing_code(request.code),
            "poll_secret": poll_secret,
            "expires_in": round(PAIRING_TTL_SECONDS),
            "poll_interval": PAIRING_POLL_INTERVAL_SECONDS,
        }

    async def pair_poll(self, body: Any, facts: RequestFacts) -> tuple[dict[str, Any], str | None]:
        """Poll one request; returns the payload and, for a browser, the cookie token."""
        self._require_authenticated_mode()
        if not isinstance(body, dict):
            raise AccessError(INVALID_REQUEST, "The body must be a JSON object")
        request_id, poll_secret = body.get("request_id"), body.get("poll_secret")
        if not _is_secret(request_id) or not _is_secret(poll_secret):
            raise AccessError(INVALID_REQUEST, "request_id and poll_secret are required")
        wait = self._pair_poll.acquire(_client_key(facts))
        if wait is not None:
            raise AccessError(RATE_LIMITED, "Polling too fast", status=429, retry_after=wait)
        request = self.pairings.poll(request_id, poll_secret)
        if request is None:
            return {"status": "expired"}, None
        if request.state == "pending":
            return {"status": "pending", "expires_in": self.pairings.expires_in(request)}, None
        if request.state == "denied":
            return {"status": "denied"}, None
        assert request.approved_by is not None
        device, token = await self._create_device(
            name=request.name,
            kind=request.kind,
            approved_by=request.approved_by,
            client=_client_text(facts),
            via="pairing",
        )
        payload: dict[str, Any] = {"status": "approved", "device": _device_summary(device)}
        if device.kind == "browser":
            return payload, token
        payload["token"] = token
        return payload, None

    def pending_pairing(self, code: Any, principal: Principal) -> dict[str, Any]:
        """What the approver sees before confirming a code."""
        request = self._pending(code, principal)
        return {
            "code": format_pairing_code(request.code),
            "name": request.name,
            "kind": request.kind,
            "client": request.client,
            "requested_at": format_canonical_timestamp(
                datetime.fromtimestamp(request.requested_at, UTC)
            ),
            "expires_in": self.pairings.expires_in(request),
        }

    def decide_pairing(self, code: Any, principal: Principal, *, approved: bool) -> dict[str, Any]:
        request = self._pending(code, principal)
        self.pairings.decide(request, approved=approved, principal=principal.actor)
        _LOGGER.info(
            "Pairing %s (request=%s kind=%s actor=%s)",
            "approved" if approved else "denied",
            request.id,
            request.kind,
            principal.actor,
        )
        return {"status": "approved" if approved else "denied", "name": request.name}

    def _pending(self, code: Any, principal: Principal) -> PairingRequest:
        self._require_authenticated_mode()
        _require_signed_in(principal)
        key = principal.actor
        wait = self._approval_failures.blocked(key)
        if wait is not None:
            raise AccessError(
                RATE_LIMITED, "Too many wrong pairing codes", status=429, retry_after=wait
            )
        request = self.pairings.pending(code) if isinstance(code, str) else None
        if request is None:
            self._approval_failures.record(key)
            _LOGGER.debug("Pairing code not found (actor=%s)", principal.actor)
            raise AccessError(
                PAIRING_NOT_FOUND,
                "No device is waiting with this code; check it or start pairing again",
                status=404,
            )
        return request

    # Login tickets -------------------------------------------------------------

    def issue_ticket(self, principal: Principal, name: Any) -> dict[str, Any]:
        self._require_authenticated_mode()
        _require_signed_in(principal)
        try:
            device_name = normalize_device_name("Browser" if name is None else name)
        except ValueError as exc:
            raise AccessError(INVALID_REQUEST, str(exc)) from None
        self._require_store()
        ticket = self.tickets.issue(issued_by=principal.actor, name=device_name)
        return {
            "ticket": ticket,
            "path": f"{TICKET_PATH_PREFIX}{ticket}",
            "expires_in": round(LOGIN_TICKET_TTL_SECONDS),
        }

    async def redeem_ticket(self, ticket: str, facts: RequestFacts) -> str | None:
        """Turn a valid ticket into a browser device; returns its token or ``None``."""
        if self.config.mode != "authenticated":
            return None
        redeemed = self.tickets.redeem(ticket)
        if redeemed is None:
            return None
        _device, token = await self._create_device(
            name=redeemed.name,
            kind="browser",
            approved_by=redeemed.issued_by,
            client=_client_text(facts),
            via="ticket",
        )
        return token

    # Device management ---------------------------------------------------------

    def list_devices(self, principal: Principal | None) -> dict[str, Any]:
        store = self._require_store()
        current = principal.device.id if principal and principal.device else None
        return {
            "devices": [
                {**device.describe(), "current": device.id == current} for device in store.devices()
            ]
        }

    async def rename_device(
        self, device_id: Any, name: Any, principal: Principal
    ) -> dict[str, Any]:
        store = self._require_store()
        if not isinstance(device_id, str):
            raise AccessError(INVALID_REQUEST, "id must be a device id")
        try:
            normalize_device_name(name)
            device = await _DEVICE_WRITES.call_async(lambda: store.rename(device_id, name))
        except ValueError as exc:
            raise AccessError(INVALID_REQUEST, str(exc)) from None
        except DeviceNotFoundError:
            raise AccessError(DEVICE_NOT_FOUND, "No device has this id", status=404) from None
        except (DeviceStoreUnavailableError, OSError) as exc:
            raise _store_failure(exc) from None
        _LOGGER.info("Device renamed (device=%s actor=%s)", device.id, principal.actor)
        return {"device": device.describe()}

    async def revoke_devices(
        self, device_ids: list[str], principal: Principal, *, missing_ok: bool = False
    ) -> list[Device]:
        store = self._require_store()
        if not missing_ok and any(store.get(device_id) is None for device_id in device_ids):
            raise AccessError(DEVICE_NOT_FOUND, "No device has this id", status=404)
        try:
            removed = await _DEVICE_WRITES.call_async(lambda: store.remove(device_ids))
        except (DeviceStoreUnavailableError, OSError) as exc:
            # The devices are out of memory already; only the file kept them.
            self.connections.revoke(set(device_ids))
            raise _store_failure(exc) from None
        closed = self.connections.revoke({device.id for device in removed})
        for device in removed:
            _LOGGER.info("Device revoked (device=%s actor=%s)", device.id, principal.actor)
        if closed:
            _LOGGER.debug("Revoked device connections closed (count=%d)", closed)
        return removed

    def other_device_ids(self, principal: Principal) -> list[str]:
        store = self._require_store()
        current = principal.device.id if principal.device else None
        return [device.id for device in store.devices() if device.id != current]

    # Internals ---------------------------------------------------------------

    def _device(
        self, token: str, facts: RequestFacts, enforcing: bool, *, from_cookie: bool
    ) -> Authentication:
        store = self._store
        now = self._utc_now()
        outcome, device = ("invalid", None) if store is None else store.lookup(token, now=now)
        if outcome == "valid" and device is not None and store is not None:
            if store.record_use(device.id, _client_text(facts), now=now):
                self._schedule_flush(store)
            return Authentication(Principal("device", device))
        if outcome == "expired" and device is not None:
            self._spawn(self._expire([device]))
        return self._refused(
            "expired_device" if outcome == "expired" else "invalid_token",
            enforcing,
            clear_cookie=from_cookie,
        )

    @staticmethod
    def _refused(reason: str, enforcing: bool, *, clear_cookie: bool = False) -> Authentication:
        if not enforcing:
            return Authentication(OPEN)
        return Authentication(None, failure=reason, clear_cookie=clear_cookie)

    async def _create_device(
        self, *, name: str, kind: str, approved_by: str, client: str | None, via: str
    ) -> tuple[Device, str]:
        store = self._require_store()
        device, token = new_device(
            name=name, kind=kind, approved_by=approved_by, client=client, now=self._utc_now()
        )
        try:
            await _DEVICE_WRITES.call_async(lambda: store.add(device))
        except (DeviceStoreUnavailableError, OSError) as exc:
            raise _store_failure(exc) from None
        _LOGGER.info(
            "Device paired (device=%s kind=%s approved_by=%s via=%s)",
            device.id,
            device.kind,
            approved_by,
            via,
        )
        return device, token

    async def _expire(self, devices: list[Device]) -> None:
        store = self._store
        if not devices or store is None:
            return
        ids = [device.id for device in devices]
        try:
            removed = await _DEVICE_WRITES.call_async(lambda: store.remove(ids))
        except (DeviceStoreUnavailableError, OSError) as exc:
            _LOGGER.warning("Removing expired devices failed (count=%d error=%s)", len(ids), exc)
            return
        self.connections.revoke({device.id for device in removed})
        for device in removed:
            _LOGGER.info(
                "Device expired (device=%s idle_days=%d)", device.id, DEVICE_IDLE_EXPIRY.days
            )

    def _schedule_flush(self, store: DeviceStore) -> None:
        if self._flush_pending:
            return
        self._flush_pending = True

        def flush() -> None:
            try:
                store.flush_usage()
            except (DeviceStoreUnavailableError, OSError) as exc:
                _LOGGER.warning("Writing device last use failed (error=%s)", exc)
            finally:
                self._flush_pending = False

        if not _DEVICE_WRITES.hand_off(flush, limit=1):
            self._flush_pending = False

    def _spawn(self, work: Coroutine[Any, Any, None]) -> None:
        task = asyncio.get_running_loop().create_task(work, name="access-device-expiry")
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    def _require_store(self) -> DeviceStore:
        store = self._store
        if store is None or not store.available:
            raise AccessError(
                DEVICES_UNAVAILABLE,
                "The device store is unavailable; see the server log",
                status=503,
            )
        return store

    def _require_authenticated_mode(self) -> None:
        if self.config.mode != "authenticated":
            raise AccessError(
                AUTH_DISABLED,
                f"Pairing and sign-in links exist only in the access mode "
                f"'With login (internet)'; this server runs '{self.config.label}'",
                status=409,
            )


def _require_signed_in(principal: Principal) -> None:
    if principal.kind == "open":
        raise AccessError(AUTH_REQUIRED, "Sign in to approve devices", status=401)


def _store_failure(exc: BaseException) -> AccessError:
    _LOGGER.warning("Device store change failed (error=%s)", exc)
    return AccessError(
        DEVICES_UNAVAILABLE, "The device store could not be changed; see the server log", status=503
    )


def _device_summary(device: Device) -> dict[str, Any]:
    return {"id": device.id, "name": device.name, "kind": device.kind}


def _client_text(facts: RequestFacts) -> str | None:
    return None if facts.client is None else str(facts.client)


def _client_key(facts: RequestFacts) -> str:
    client = facts.client or facts.peer
    return "unknown" if client is None else str(client)


def _is_secret(value: Any) -> TypeIs[str]:
    return isinstance(value, str) and 0 < len(value) <= _MAX_SECRET_CHARS


def _cookie_value(headers: list[tuple[bytes, bytes]], name: str) -> str | None:
    """The first value of cookie ``name`` across ``Cookie`` headers."""
    for key, value in headers:
        if key != b"cookie":
            continue
        for part in value.decode("latin-1").split(";"):
            cookie_name, separator, cookie_value = part.strip().partition("=")
            if separator and cookie_name == name:
                return cookie_value.strip().strip('"')
    return None


def _log_path(path: str) -> str:
    """At most two path segments: deeper ones can carry capabilities or tickets."""
    parts = path.split("/")
    shown = "/".join(parts[:3])
    return shown if len(parts) <= 3 else f"{shown}/..."
