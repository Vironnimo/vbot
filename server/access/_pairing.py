"""Pending pairing requests and login tickets: short-lived, single-use, in memory only."""

from __future__ import annotations

import hashlib
import secrets
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from core.utils.ids import new_id

PAIRING_TTL_SECONDS = 600.0
#: How long a decided request keeps answering its device after the first answer
#: that told it the outcome: a device whose answer got lost polls again and gets
#: the same outcome (an approved one with the same token) instead of ``expired``.
#: The outcome, and with it a native device's token, stays in memory that long.
CLAIM_GRACE_SECONDS = 60.0
LOGIN_TICKET_TTL_SECONDS = 60.0
#: Unambiguous characters: no 0/O, 1/I/L or U/V look-alikes. 30^8 codes.
PAIRING_CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTVWXYZ"
PAIRING_CODE_LENGTH = 8
#: Bounds memory under a flood that the rate limits let through. Requests of this
#: computer's own clients are not counted; they are bounded by their rate limit.
MAX_PENDING_PAIRINGS = 256
MAX_LOGIN_TICKETS = 256

PairingState = Literal["pending", "approved", "denied"]


def format_pairing_code(code: str) -> str:
    """The display form ``XXXX-XXXX``."""
    return f"{code[:4]}-{code[4:]}"


def normalize_pairing_code(value: str) -> str | None:
    """Accept the code as typed (any case, with dashes or spaces); ``None`` when malformed."""
    code = "".join(value.split()).replace("-", "").upper()
    if len(code) != PAIRING_CODE_LENGTH or any(c not in PAIRING_CODE_ALPHABET for c in code):
        return None
    return code


def _digest(secret: str) -> bytes:
    return hashlib.sha256(secret.encode("utf-8")).digest()


@dataclass
class PairingRequest:
    """One device waiting to be approved."""

    id: str
    code: str
    name: str
    kind: str
    client: str | None
    requested_at: float
    expires_at: float
    poll_secret_hash: bytes = field(repr=False)
    state: PairingState = "pending"
    approved_by: str | None = None
    #: The rate limit key of the client that asked, and whether it is this computer.
    client_key: str = ""
    local: bool = False
    #: Whether a poll has learned the decision; the grace period runs from then.
    handed_out: bool = False
    #: The device's claim of the approval once started: opaque to the registry,
    #: owned by the access service. A claimed approval is never withdrawn here.
    claim: Any = field(default=None, repr=False)


class PairingRegistry:
    """Pending pairing requests by code and by request id.

    ``clock`` is monotonic (expiry); ``wall_clock`` gives the request time shown
    to the approver.
    """

    def __init__(self, *, clock: Callable[[], float], wall_clock: Callable[[], float]) -> None:
        self._clock = clock
        self._wall_clock = wall_clock
        self._requests: OrderedDict[str, PairingRequest] = OrderedDict()

    def start(
        self,
        *,
        name: str,
        kind: str,
        client: str | None,
        client_key: str = "",
        local: bool = False,
    ) -> tuple[PairingRequest, str]:
        """Create a request; returns it with the poll secret only the new device learns.

        A full table makes room by dropping the oldest undecided request of the
        client that holds the most, so one client cannot crowd out the others.
        ``OverflowError`` only when every counted request is already approved.
        """
        self.sweep()
        if not local:
            self._make_room()
        codes = {request.code for request in self._requests.values()}
        code = ""
        while not code or code in codes:
            code = "".join(
                secrets.choice(PAIRING_CODE_ALPHABET) for _ in range(PAIRING_CODE_LENGTH)
            )
        poll_secret = secrets.token_urlsafe(32)
        request = PairingRequest(
            id=new_id("pair"),
            code=code,
            name=name,
            kind=kind,
            client=client,
            requested_at=self._wall_clock(),
            expires_at=self._clock() + PAIRING_TTL_SECONDS,
            poll_secret_hash=_digest(poll_secret),
            client_key=client_key,
            local=local,
        )
        self._requests[request.id] = request
        return request, poll_secret

    def _make_room(self) -> None:
        counted = [request for request in self._requests.values() if not request.local]
        if len(counted) < MAX_PENDING_PAIRINGS:
            return
        held: dict[str, list[PairingRequest]] = {}
        for request in counted:
            if request.state != "approved":
                held.setdefault(request.client_key, []).append(request)
        if not held:
            raise OverflowError("too many pending pairing requests")
        # Oldest first within a client; the client holding the most gives way.
        crowding = max(held.values(), key=len)
        del self._requests[crowding[0].id]

    def withdraw_approvals(self, approvers: set[str]) -> int:
        """Deny the unclaimed requests these approvers approved; returns how many."""
        withdrawn = 0
        for request in self._requests.values():
            if (
                request.state == "approved"
                and request.claim is None
                and request.approved_by in approvers
            ):
                request.state = "denied"
                request.approved_by = None
                withdrawn += 1
        return withdrawn

    def pending(self, code: str) -> PairingRequest | None:
        """The pending request a typed code names, if it is still open."""
        normalized = normalize_pairing_code(code)
        if normalized is None:
            return None
        self.sweep()
        for request in self._requests.values():
            if request.state == "pending" and secrets.compare_digest(request.code, normalized):
                return request
        return None

    def decide(self, request: PairingRequest, *, approved: bool, principal: str) -> None:
        """Approve or deny a pending request; its device claims the outcome by polling."""
        if request.state != "pending" or self._requests.get(request.id) is not request:
            raise LookupError("pairing request is no longer pending")
        request.state = "approved" if approved else "denied"
        request.approved_by = principal if approved else None

    def poll(self, request_id: str, poll_secret: str) -> PairingRequest | None:
        """The request a device polls for, or ``None`` when unknown, expired or not its own.

        A decided request keeps answering for ``CLAIM_GRACE_SECONDS`` after the
        first poll that learned the decision, then it is forgotten.
        """
        self.sweep()
        request = self._requests.get(request_id)
        if request is None or not secrets.compare_digest(
            request.poll_secret_hash, _digest(poll_secret)
        ):
            return None
        if request.state != "pending" and not request.handed_out:
            request.handed_out = True
            request.expires_at = self._clock() + CLAIM_GRACE_SECONDS
        return request

    def expires_in(self, request: PairingRequest) -> int:
        return max(0, round(request.expires_at - self._clock()))

    def sweep(self) -> None:
        now = self._clock()
        for request_id in [key for key, item in self._requests.items() if item.expires_at <= now]:
            del self._requests[request_id]


@dataclass(frozen=True)
class LoginTicket:
    """What redeeming one ticket creates: a browser device approved by ``issued_by``.

    ``next`` is the validated same-origin path (with query) the browser lands on.
    """

    issued_by: str
    name: str
    expires_at: float
    next: str = "/"


class LoginTickets:
    """Single-use tickets that turn a top-level navigation into a browser device."""

    def __init__(self, *, clock: Callable[[], float]) -> None:
        self._clock = clock
        self._tickets: OrderedDict[bytes, LoginTicket] = OrderedDict()

    def issue(self, *, issued_by: str, name: str, next_path: str = "/") -> str:
        self._sweep()
        while len(self._tickets) >= MAX_LOGIN_TICKETS:
            self._tickets.popitem(last=False)
        ticket = secrets.token_urlsafe(32)
        self._tickets[_digest(ticket)] = LoginTicket(
            issued_by=issued_by,
            name=name,
            expires_at=self._clock() + LOGIN_TICKET_TTL_SECONDS,
            next=next_path,
        )
        return ticket

    def redeem(self, ticket: str) -> LoginTicket | None:
        """Consume a ticket; ``None`` when unknown, used or expired."""
        self._sweep()
        if len(ticket) > 128:
            return None
        return self._tickets.pop(_digest(ticket), None)

    def drop_issued_by(self, issuers: set[str]) -> int:
        """Forget the tickets these principals issued; returns how many."""
        dropped = [key for key, item in self._tickets.items() if item.issued_by in issuers]
        for key in dropped:
            del self._tickets[key]
        return len(dropped)

    def _sweep(self) -> None:
        now = self._clock()
        for key in [key for key, item in self._tickets.items() if item.expires_at <= now]:
            del self._tickets[key]
