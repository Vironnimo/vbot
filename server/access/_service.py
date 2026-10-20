"""The access service: the access state of one server app and its operations."""

from __future__ import annotations

import asyncio
import ipaddress
import re
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
    DeviceOrigin,
    DeviceStore,
    DeviceStoreUnavailableError,
    new_device,
    normalize_device_name,
    parse_device_token,
)
from core.access.local_token import is_local_access_token
from core.utils.logging import get_logger
from core.utils.timestamps import format_canonical_timestamp
from core.utils.workers import OrderedWorker
from server.access._connections import LiveConnections
from server.access._limits import RateLimit, RefusalLog, SlidingWindow
from server.access._pairing import (
    CLAIM_GRACE_SECONDS,
    LOGIN_TICKET_TTL_SECONDS,
    PAIRING_TTL_SECONDS,
    LoginTickets,
    PairingRegistry,
    PairingRequest,
    format_pairing_code,
)
from server.access._principal import HOST, OPEN, Principal
from server.access._rules import (
    TICKET_PATH_PREFIX,
    RequestFacts,
    host_policy,
    inspect_request,
)

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

#: Refusal reasons of Cron webhook requests, as the refusal log names them.
WEBHOOK_REFUSED = "webhook_refused"
WEBHOOK_RATE_LIMITED = "webhook_rate_limited"
WEBHOOK_TOO_LARGE = "webhook_too_large"

#: ``__Host-``: browsers keep it only when set Secure, with Path=/ and without
#: Domain, so no other site or subdomain can plant or shadow it.
DEVICE_COOKIE = "__Host-vbot_device"
DEVICE_COOKIE_MAX_AGE = 400 * 24 * 3600
PAIRING_POLL_INTERVAL_SECONDS = 2
REFUSAL_LOG_INTERVAL_SECONDS = 60.0
_MAX_SECRET_CHARS = 128
#: Where a login ticket may send its browser: a path on this server with an
#: optional query, from RFC 3986 path and query characters and percent escapes
#: only. No scheme, authority, fragment, backslash or white space can pass.
_MAX_TICKET_NEXT_CHARS = 2048
_TICKET_NEXT = re.compile(r"/(?:[A-Za-z0-9\-._~!$&'()*+,;=:@/?]|%[0-9A-Fa-f]{2})*")


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
    ``unavailable`` marks a well-formed device token that could not be checked
    because the device store is unavailable: neither valid nor refused.
    """

    principal: Principal | None
    failure: str | None = None
    clear_cookie: bool = False
    unavailable: bool = False


@dataclass(frozen=True)
class TicketRedemption:
    """A redeemed login ticket: where the browser lands and the new device's token.

    ``token`` is ``None`` when the browser was already signed in as a browser
    device: it keeps that device instead of getting another one.
    """

    next: str
    token: str | None


def _limit(per_client: int, overall: int, window: float, clock: Callable[[], float]) -> RateLimit:
    return RateLimit(
        SlidingWindow(per_client, window, clock=clock),
        SlidingWindow(overall, window, clock=clock),
    )


def _acquire(limit: RateLimit, key: str, *, local: bool) -> float | None:
    """Count one event; this computer's own clients stay outside the shared budget."""
    return limit.per_client.acquire(key) if local else limit.acquire(key)


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
        bind_host: str | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        utc_now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.config = config
        self._local_token = local_token
        self._admitted = config.admitted_networks()
        self._proxies = config.proxy_networks()
        self._hosts = host_policy(config, bind_host=bind_host)
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
        self._webhook_requests = _limit(60, 600, 60, clock)
        self._webhook_failures = SlidingWindow(20, 600, clock=clock)
        self._refusals: RefusalLog | None = None
        self._background: set[asyncio.Future[Any]] = set()
        self._flush_pending = False
        self._on_change: Callable[[], None] | None = None

    # Lifecycle -------------------------------------------------------------

    async def start(self, data_dir: Path, *, on_change: Callable[[], None] | None = None) -> None:
        """Load the device store and remove devices that expired while stopped.

        ``on_change`` is called on the Event Loop whenever the paired devices or
        a pairing decision change, so that open device lists reload.
        """
        self._on_change = on_change
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
        """Persist pending removals and remembered uses at shutdown."""
        for task in list(self._background):
            await asyncio.gather(task, return_exceptions=True)
        store = self._store
        if store is not None and store.available:
            try:
                await _DEVICE_WRITES.call_async(store.flush_usage)
            except (DeviceStoreUnavailableError, OSError) as exc:
                _LOGGER.warning("Writing pending device state failed (error=%s)", exc)

    # Request path ------------------------------------------------------------

    def inspect(self, scope: Mapping[str, Any]) -> RequestFacts:
        return inspect_request(
            scope, self.config, admitted=self._admitted, proxies=self._proxies, hosts=self._hosts
        )

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
        cookies = _cookie_values(headers, DEVICE_COOKIE)
        if len(cookies) > 1:
            # Which one the browser meant is unknown; clearing could drop the valid one.
            return self._refused("malformed_credentials", enforcing)
        if cookies:
            return self._device(cookies[0], facts, enforcing, from_cookie=True)
        return Authentication(None if enforcing else OPEN)

    def principal_for_capability(self, actor: str) -> Principal | None:
        """Resolve a capability's issuer without renewing its activity.

        This is an Event Loop lookup. A missing or expired Device cannot
        delegate access; expiry also closes its existing connections. An
        unavailable store raises ``devices_unavailable``.
        """
        if actor == HOST.actor:
            return HOST
        if actor == OPEN.actor:
            return OPEN if self.config.mode != "authenticated" else None
        store = self._require_store()
        device = store.get(actor)
        if device is None:
            return None
        if self._utc_now() - device.last_used_at > DEVICE_IDLE_EXPIRY:
            self._spawn(self._expire([device]))
            return None
        return Principal("device", device)

    def refused(
        self, reason: str, facts: RequestFacts, path: str, *, detail: str | None = None
    ) -> None:
        """Log one refused request, aggregated per reason."""
        if self._refusals is None:
            loop = asyncio.get_running_loop()
            self._refusals = RefusalLog(
                _LOGGER, interval=REFUSAL_LOG_INTERVAL_SECONDS, schedule=loop.call_later
            )
        client = facts.client or facts.peer
        self._refusals.refused(
            reason,
            client=None if client is None else str(client),
            detail=f"path={_log_path(path)}" + (f" {detail}" if detail else ""),
        )

    def failed_authentication(self, facts: RequestFacts) -> float | None:
        """Count a refused credential; returns the wait once the client is over its limit."""
        key = _client_key(facts)
        wait = self._auth_failures.blocked(key)
        self._auth_failures.record(key)
        return wait

    # Webhooks ----------------------------------------------------------------

    def admit_webhook(self, facts: RequestFacts, path: str) -> float | None:
        """Count one request to a Cron webhook; the wait when its client is over a limit.

        A client waits once it sends too many requests or keeps being refused;
        this computer's own clients stay outside the budget shared by all.
        """
        key = _client_key(facts)
        wait = self._webhook_failures.blocked(key) or _acquire(
            self._webhook_requests, key, local=facts.direct_loopback
        )
        if wait is not None:
            self.refused(WEBHOOK_RATE_LIMITED, facts, path)
        return wait

    def refused_webhook(self, reason: str, facts: RequestFacts, path: str) -> None:
        """Log a refused webhook request; a refused secret counts toward its client's limit."""
        self.refused(reason, facts, path)
        if reason == WEBHOOK_REFUSED:
            self._webhook_failures.record(_client_key(facts))

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
        # This computer's own clients pair outside the shared budget and the pending
        # cap, so that a flood from elsewhere can never lock out its host.
        local = facts.direct_loopback
        key = _client_key(facts)
        wait = _acquire(self._pair_start, key, local=local)
        if wait is not None:
            self.refused("pairing_rate_limited", facts, "/api/auth/pair/start")
            raise AccessError(
                RATE_LIMITED, "Too many pairing requests", status=429, retry_after=wait
            )
        try:
            request, poll_secret = self.pairings.start(
                name=name, kind=kind, client=_client_text(facts), client_key=key, local=local
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
        wait = _acquire(self._pair_poll, _client_key(facts), local=facts.direct_loopback)
        if wait is not None:
            raise AccessError(RATE_LIMITED, "Polling too fast", status=429, retry_after=wait)
        request = self.pairings.poll(request_id, poll_secret)
        if request is None:
            return {"status": "expired"}, None
        if request.state == "pending":
            return {"status": "pending", "expires_in": self.pairings.expires_in(request)}, None
        if request.state == "denied":
            self.pairings.handed_out(request)
            return {"status": "denied"}, None
        return await self._claim(request, facts)

    async def _claim(
        self, request: PairingRequest, facts: RequestFacts
    ) -> tuple[dict[str, Any], str | None]:
        """Create an approved request's device once; repeated polls get the same answer.

        A device whose approved answer got lost polls again within the claim
        grace period and receives the same device and token. Concurrent polls
        share one claim; a claim that failed on the store is tried again by the
        next poll.
        """
        claim = request.claim
        if claim is None:
            claim = asyncio.ensure_future(self._create_claimed_device(request, facts))
            request.claim = claim
            self._background.add(claim)

            def settled(done: asyncio.Future[Any]) -> None:
                self._background.discard(done)
                failed = not done.cancelled() and done.exception() is not None
                if failed and request.claim is done:
                    # Nothing was created; the next poll claims again.
                    request.claim = None

            claim.add_done_callback(settled)
        # A poll that is cancelled (its client went away) leaves the claim running.
        payload, cookie_token = await asyncio.shield(claim)
        return dict(payload), cookie_token

    async def _create_claimed_device(
        self, request: PairingRequest, facts: RequestFacts
    ) -> tuple[dict[str, Any], str | None]:
        assert request.approved_by is not None
        created = await self._create_device(
            name=request.name,
            kind=request.kind,
            approved_by=request.approved_by,
            client=_client_text(facts),
            via="pairing",
        )
        if created is None:
            self._claim_handed_out(request)
            return {"status": "denied"}, None
        device, token = created
        payload: dict[str, Any] = {"status": "approved", "device": _device_summary(device)}
        self._claim_handed_out(request)
        if device.kind == "browser":
            return payload, token
        payload["token"] = token
        return payload, None

    def _claim_handed_out(self, request: PairingRequest) -> None:
        """Keep a settled claim recoverable after a lost or cancelled response."""
        self.pairings.handed_out(request)
        asyncio.get_running_loop().call_later(CLAIM_GRACE_SECONDS, self.pairings.sweep)

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
        self._changed()
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

    def issue_ticket(
        self, principal: Principal, name: Any, next_path: Any = None
    ) -> dict[str, Any]:
        """Issue a login ticket; ``next_path`` is where its browser lands (``/`` by default)."""
        self._require_authenticated_mode()
        _require_signed_in(principal)
        try:
            device_name = normalize_device_name("Browser" if name is None else name)
        except ValueError as exc:
            raise AccessError(INVALID_REQUEST, str(exc)) from None
        landing = _ticket_next(next_path)
        self._require_store()
        ticket = self.tickets.issue(issued_by=principal.actor, name=device_name, next_path=landing)
        return {
            "ticket": ticket,
            "path": f"{TICKET_PATH_PREFIX}{ticket}",
            "expires_in": round(LOGIN_TICKET_TTL_SECONDS),
        }

    async def redeem_ticket(
        self, ticket: str, facts: RequestFacts, principal: Principal | None = None
    ) -> TicketRedemption | None:
        """Redeem a valid ticket once; ``None`` when it is not valid.

        A browser that is already signed in as a browser device keeps that
        device; any other browser gets a new one. An unavailable device store
        raises ``devices_unavailable`` before the ticket is used up.
        """
        if self.config.mode != "authenticated":
            return None
        store = self._require_store()
        redeemed = self.tickets.redeem(ticket)
        if redeemed is None:
            return None
        current = None if principal is None else principal.device
        if current is not None and current.kind == "browser":
            try:
                rebound = await _DEVICE_WRITES.call_async(
                    lambda: store.rebind_ticket_browser(current.id, redeemed.issued_by)
                )
            except DeviceNotFoundError, ValueError:
                return None
            except (DeviceStoreUnavailableError, OSError) as exc:
                raise _store_failure(exc) from None
            if rebound.approved_by != current.approved_by:
                _LOGGER.info(
                    "Browser sign-in renewed (device=%s approved_by=%s)",
                    rebound.id,
                    rebound.approved_by,
                )
                self._changed()
            _LOGGER.debug(
                "Login ticket redeemed by a signed-in browser; it keeps its device (device=%s)",
                current.id,
            )
            return TicketRedemption(redeemed.next, None)
        created = await self._create_device(
            name=redeemed.name,
            kind="browser",
            approved_by=redeemed.issued_by,
            client=_client_text(facts),
            via="ticket",
        )
        return None if created is None else TicketRedemption(redeemed.next, created[1])

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
        self._changed()
        return {"device": device.describe()}

    async def revoke_devices(
        self,
        device_ids: list[str],
        principal: Principal,
        *,
        missing_ok: bool = False,
        spare: frozenset[str] = frozenset(),
    ) -> list[Device]:
        """Revoke devices and the browsers they signed in with login tickets.

        A browser device created by redeeming a login ticket goes with the
        device that issued the ticket, transitively; a device that claimed an
        approved pairing never does. ``spare`` keeps devices out of that
        cascade (``devices.revoke_others`` keeps the calling device).
        """
        store = self._require_store()
        if not missing_ok and any(
            store.get(device_id) is None and not store.removal_pending(device_id)
            for device_id in device_ids
        ):
            raise AccessError(DEVICE_NOT_FOUND, "No device has this id", status=404)
        targets = _with_ticket_browsers(store.devices(), device_ids, spare=spare)
        # What these devices approved but nobody claimed yet goes with them, before
        # the store write lets anything else run.
        self._withdraw_approvals(set(targets))

        def remove() -> list[Device]:
            # Earlier admitted creations may still be writing. Expand the cascade
            # on the same ordered worker as removal, after those creations settle.
            targets[:] = _with_ticket_browsers(store.devices(), device_ids, spare=spare)
            return store.remove(targets)

        try:
            removed = await _DEVICE_WRITES.call_async(remove)
        except asyncio.CancelledError:
            self._changed()
            raise
        except (DeviceStoreUnavailableError, OSError) as exc:
            # The devices are out of memory already; only the file kept them.
            self._changed()
            raise _store_failure(exc) from None
        finally:
            # Include newly discovered ticket browsers, also when persistence
            # failed or cancellation arrived while the worker was settling.
            self._withdraw_approvals(set(targets))
            closed = self.connections.revoke(set(targets))
        for device in removed:
            _LOGGER.info("Device revoked (device=%s actor=%s)", device.id, principal.actor)
        if closed:
            _LOGGER.debug("Revoked device connections closed (count=%d)", closed)
        if removed:
            self._changed()
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
        if (store is None or not store.available) and parse_device_token(token) is not None:
            # A well-formed token cannot be checked, and the device may be valid:
            # it is neither refused nor counted, and its cookie stays.
            return Authentication(None if enforcing else OPEN, unavailable=enforcing)
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
        self, *, name: str, kind: str, approved_by: str, client: str | None, via: DeviceOrigin
    ) -> tuple[Device, str] | None:
        """Add the device; ``None`` when its approver is no longer a paired device."""
        store = self._require_store()
        device, token = new_device(
            name=name,
            kind=kind,
            approved_by=approved_by,
            client=client,
            now=self._utc_now(),
            via=via,
        )
        try:
            await _DEVICE_WRITES.call_async(lambda: store.add(device))
        except DeviceNotFoundError:
            _LOGGER.info(
                "Device not paired: its approver was revoked (approved_by=%s via=%s)",
                approved_by,
                via,
            )
            return None
        except (DeviceStoreUnavailableError, OSError) as exc:
            raise _store_failure(exc) from None
        _LOGGER.info(
            "Device paired (device=%s kind=%s approved_by=%s via=%s)",
            device.id,
            device.kind,
            approved_by,
            via,
        )
        self._changed()
        return device, token

    async def _expire(self, devices: list[Device]) -> None:
        store = self._store
        if not devices or store is None:
            return
        ids = [device.id for device in devices]
        targets: list[str] = []

        def remove() -> list[Device]:
            # A ticket browser may have renewed its issuer while this write
            # waited. Re-check before expanding and removing the delegation.
            expired = [
                device_id
                for device_id in ids
                if (device := store.get(device_id)) is not None
                and self._utc_now() - device.last_used_at > DEVICE_IDLE_EXPIRY
            ]
            targets[:] = _with_ticket_browsers(store.devices(), expired, spare=frozenset())
            return store.remove(targets)

        try:
            removed = await _DEVICE_WRITES.call_async(remove)
        except (DeviceStoreUnavailableError, OSError) as exc:
            _LOGGER.warning("Removing expired devices failed (count=%d error=%s)", len(ids), exc)
            return
        finally:
            self._withdraw_approvals(set(targets))
            self.connections.revoke(set(targets))
            if targets:
                self._changed()
        for device in removed:
            _LOGGER.info(
                "Device expired (device=%s idle_days=%d)", device.id, DEVICE_IDLE_EXPIRY.days
            )

    def _changed(self) -> None:
        """Tell the listener that devices or pairing decisions changed."""
        listener = self._on_change
        if listener is None:
            return
        try:
            listener()
        except Exception:
            _LOGGER.warning("Announcing a device change failed", exc_info=True)

    def _withdraw_approvals(self, device_ids: set[str]) -> None:
        """Deny the unclaimed pairings these devices approved and drop their login tickets.

        A claim re-checks its approver as well (``DeviceStore.add``); this makes the
        withdrawal visible at once.
        """
        denied = self.pairings.withdraw_approvals(device_ids)
        dropped = self.tickets.drop_issued_by(device_ids)
        if denied or dropped:
            _LOGGER.info(
                "Approvals of revoked devices withdrawn (pairings=%d tickets=%d)", denied, dropped
            )

    def _schedule_flush(self, store: DeviceStore) -> None:
        if self._flush_pending:
            return
        self._flush_pending = True

        def flush() -> None:
            try:
                store.flush_usage()
            except (DeviceStoreUnavailableError, OSError) as exc:
                _LOGGER.warning("Writing pending device state failed (error=%s)", exc)
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
                retry_after=30,
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


def _ticket_next(value: Any) -> str:
    """Validate where a login ticket sends its browser: a same-origin path and query."""
    if value is None:
        return "/"
    if (
        not isinstance(value, str)
        or len(value) > _MAX_TICKET_NEXT_CHARS
        or not _TICKET_NEXT.fullmatch(value)
        or "//" in value
        or value.startswith(TICKET_PATH_PREFIX)
    ):
        raise AccessError(
            INVALID_REQUEST,
            "next must be a path on this server, optionally with a query, such as /?a=b; "
            "no scheme, host, fragment, '//' or backslash",
        )
    return value


def _with_ticket_browsers(
    devices: list[Device], device_ids: list[str], *, spare: frozenset[str]
) -> list[str]:
    """``device_ids`` plus every ticket-created device they issued, transitively."""
    targets = list(dict.fromkeys(device_ids))
    issuers = set(targets)
    while issuers:
        issued = [
            device.id
            for device in devices
            if device.via == "ticket"
            and device.approved_by in issuers
            and device.id not in targets
            and device.id not in spare
        ]
        targets.extend(issued)
        issuers = set(issued)
    return targets


def _require_signed_in(principal: Principal) -> None:
    if principal.kind == "open":
        raise AccessError(AUTH_REQUIRED, "Sign in to approve devices", status=401)


def _store_failure(exc: BaseException) -> AccessError:
    _LOGGER.warning("Device store change failed (error=%s)", exc)
    return AccessError(
        DEVICES_UNAVAILABLE,
        "The device store could not be changed; see the server log",
        status=503,
        retry_after=30,
    )


def _device_summary(device: Device) -> dict[str, Any]:
    return {"id": device.id, "name": device.name, "kind": device.kind}


def _client_text(facts: RequestFacts) -> str | None:
    return None if facts.client is None else str(facts.client)


def _client_key(facts: RequestFacts) -> str:
    """The rate limit key of the effective client: its address, an IPv6 one by /64.

    One IPv6 host commonly holds a whole /64; counting its addresses apart would
    multiply its budget.
    """
    if facts.direct_loopback:
        return "loopback"
    client = facts.client or facts.peer
    if client is None:
        return "unknown"
    if isinstance(client, ipaddress.IPv6Address):
        return str(ipaddress.IPv6Network((client, 64), strict=False))
    return str(client)


def _is_secret(value: Any) -> TypeIs[str]:
    return (
        isinstance(value, str)
        and 0 < len(value) <= _MAX_SECRET_CHARS
        and value.isascii()
        and all(character.isalnum() or character in "_-" for character in value)
    )


def _cookie_values(headers: list[tuple[bytes, bytes]], name: str) -> list[str]:
    """Every value of cookie ``name`` across ``Cookie`` headers."""
    values: list[str] = []
    for key, value in headers:
        if key != b"cookie":
            continue
        for part in value.decode("latin-1").split(";"):
            cookie_name, separator, cookie_value = part.strip().partition("=")
            if separator and cookie_name == name:
                values.append(cookie_value.strip().strip('"'))
    return values


def _log_path(path: str) -> str:
    """At most two path segments: deeper ones can carry capabilities or tickets."""
    parts = path.split("/")
    shown = "/".join(parts[:3])
    return shown if len(parts) <= 3 else f"{shown}/..."
