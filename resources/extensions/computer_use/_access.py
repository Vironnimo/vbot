"""Which applications an Agent may see and operate, per Session, and how it asks.

The user grants applications to one Session through a pending input request.
Each grant carries the tier of the application's category; grants and the
Session's display choice expire together after a period without Computer Use
calls in that Session. vBot's own applications are never grantable.
"""

from __future__ import annotations

import asyncio
import copy
import difflib
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from core.utils.ids import new_id

from .target import AppInfo, Display, WindowInfo

TIERS = ("read", "click", "full")
TIER_WORDS = {"read": "view only", "click": "click only", "full": "full control"}
TIER_DETAILS = {
    "read": "screenshots, no input",
    "click": "clicks and scrolling, no typing or keys",
    "full": "mouse and keyboard",
}
CATEGORY_TIERS = {"browser": "read", "terminal": "click", "ide": "click"}

# Grants and the display choice end this long after the Session's last call.
GRANT_IDLE_SECONDS = 30 * 60
# An access request nobody answers in time counts as declined.
REQUEST_TIMEOUT_SECONDS = 300.0
ACCESS_REQUEST_KIND = "computer_access"
RESPONSE_ACTIONS = ("accept", "decline", "cancel")

type SessionKey = tuple[str | None, str, str]


def category_tier(category: str) -> str:
    return CATEGORY_TIERS.get(category, "full")


def tier_allows(granted: str, needed: str) -> bool:
    return TIERS.index(granted) >= TIERS.index(needed)


def tier_text(tier: str) -> str:
    return f"{TIER_WORDS[tier]}: {TIER_DETAILS[tier]}"


@dataclass(frozen=True)
class Grant:
    app: AppInfo
    tier: str


@dataclass
class SessionState:
    """A Session's grants, display choice and the display its last screenshot showed."""

    grants: list[Grant] = field(default_factory=list)
    display: str = "auto"
    shown: Display | None = None
    last_used: float = 0.0

    def grant_for(self, app: AppInfo) -> Grant | None:
        return next((grant for grant in self.grants if grant.app.matches(app)), None)

    def add(self, app: AppInfo, tier: str) -> None:
        self.grants = [grant for grant in self.grants if not grant.app.matches(app)]
        self.grants.append(Grant(app, tier))

    def describe(self) -> str:
        return ", ".join(f"{grant.app.name} ({TIER_WORDS[grant.tier]})" for grant in self.grants)


class Sessions:
    """Per-Session state that expires *idle* seconds after the Session's last call."""

    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        idle: float = GRANT_IDLE_SECONDS,
    ) -> None:
        self.clock = clock
        self.idle = idle
        self._states: dict[SessionKey, SessionState] = {}

    def use(self, key: SessionKey) -> SessionState:
        """Return *key*'s state for a new call, after dropping every expired state."""
        now = self.clock()
        for other, state in tuple(self._states.items()):
            if now - state.last_used > self.idle:
                del self._states[other]
        state = self._states.setdefault(key, SessionState())
        state.last_used = now
        return state

    def clear(self) -> None:
        self._states.clear()


def window_visible(state: SessionState, own_keys: frozenset[str]) -> Callable[[WindowInfo], bool]:
    """Return whether a window's application is granted to *state* (at any tier)."""

    def visible(window: WindowInfo) -> bool:
        if window.app.keys & own_keys:
            return False
        return state.grant_for(window.app) is not None

    return visible


@dataclass(frozen=True)
class Resolution:
    apps: list[AppInfo]
    problems: list[str]


def resolve_app(name: str, apps: Sequence[AppInfo]) -> AppInfo | str:
    """Return the app *name* means, or an English problem with suggestions.

    A case-insensitive exact name or executable match wins; otherwise the one
    app whose name contains *name*. Several or no candidates are a problem.
    """
    folded = name.strip().casefold()
    if not folded:
        return "An empty app name was given."
    unique = list({app.name.casefold(): app for app in apps}.values())
    exact = [
        app
        for app in unique
        if app.name.casefold() == folded
        or {f"exe:{folded}", f"exe:{folded}.exe"} & {key.casefold() for key in app.keys}
    ]
    if len(exact) == 1:
        return exact[0]
    candidates = exact or [app for app in unique if folded in app.name.casefold()]
    if len(candidates) == 1:
        return candidates[0]
    if candidates:
        names = ", ".join(sorted(app.name for app in candidates)[:8])
        return f'"{name}" matches several apps: {names}. Use one of these names.'
    close = difflib.get_close_matches(
        folded, [app.name.casefold() for app in unique], n=5, cutoff=0.6
    )
    by_folded = {app.name.casefold(): app.name for app in unique}
    if close:
        return f'"{name}" was not found. Did you mean: {", ".join(by_folded[c] for c in close)}?'
    return (
        f'"{name}" was not found among the installed and running apps. Search them with '
        f'computer_apps {{"action":"list","query":"..."}}.'
    )


def resolve_apps(names: Sequence[str], apps: Sequence[AppInfo]) -> Resolution:
    """Resolve each requested name; a comma list that names several apps is split."""
    resolved: list[AppInfo] = []
    problems: list[str] = []
    for name in names:
        match = resolve_app(name, apps)
        if isinstance(match, str) and "," in name:
            parts = [resolve_app(part, apps) for part in name.split(",") if part.strip()]
            if parts and all(isinstance(part, AppInfo) for part in parts):
                resolved.extend(part for part in parts if isinstance(part, AppInfo))
                continue
        if isinstance(match, str):
            problems.append(match)
        elif not any(app.matches(match) for app in resolved):
            resolved.append(match)
    return Resolution(resolved, problems)


def request_message(agent_name: str, grants: Sequence[Grant], reason: str | None) -> str:
    """The text the user reads when deciding about an access request."""
    lines = [f'Agent "{agent_name}" asks to use these apps on the vBot server\'s desktop:']
    lines.extend(f"- {grant.app.name}: {tier_text(grant.tier)}" for grant in grants)
    if reason:
        lines.append(f"Reason: {reason.strip()}")
    lines.append(
        "The Agent moves the real mouse and keyboard while it works; other apps stay hidden "
        "from it. Access ends 30 minutes after its last Computer Use action in this Session. "
        "Accept to allow, or decline."
    )
    return "\n".join(lines)


@dataclass
class _Pending:
    id: str
    session_id: str
    payload: dict[str, Any]
    answer: asyncio.Future[str]


class AccessRequests:
    """Pending access requests; ``on_change`` receives each added or removed id."""

    def __init__(self, on_change: Callable[[str], None] | None = None) -> None:
        self._pending: dict[str, _Pending] = {}
        self._on_change = on_change
        self.timeout = REQUEST_TIMEOUT_SECONDS

    async def ask(self, session_id: str, message: str) -> str:
        """Wait for the user's answer: ``accept``, ``decline``, ``cancel`` or ``timeout``.

        Cancelling the waiting task (a cancelled Run) withdraws the request.
        """
        identifier = new_id("req", claim=lambda candidate: candidate not in self._pending)
        answer: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._pending[identifier] = _Pending(identifier, session_id, {"message": message}, answer)
        expiry = asyncio.timeout(self.timeout)
        try:
            self._changed(identifier)
            async with expiry:
                return await answer
        except TimeoutError:
            if not expiry.expired():
                raise
            return "timeout"
        finally:
            self._pending.pop(identifier, None)
            self._changed(identifier)

    def _changed(self, identifier: str) -> None:
        if self._on_change is not None:
            self._on_change(identifier)

    def list(self) -> list[dict[str, Any]]:
        return [
            {
                "id": item.id,
                "kind": ACCESS_REQUEST_KIND,
                "payload": copy.deepcopy(item.payload),
                "session_id": item.session_id,
            }
            for item in self._pending.values()
        ]

    def respond(self, identifier: Any, response: Any) -> dict[str, Any]:
        pending = self._pending.get(identifier) if isinstance(identifier, str) else None
        if pending is None or pending.answer.done():
            raise ValueError("Computer Use access request no longer exists")
        action = response.get("action") if isinstance(response, dict) else None
        if action not in RESPONSE_ACTIONS:
            raise ValueError("Computer Use access response needs action accept, decline or cancel")
        pending.answer.set_result(action)
        return {"id": identifier, "answered": True}

    def cancel_all(self) -> None:
        """Answer every pending request with ``cancel`` (shutdown)."""
        for pending in tuple(self._pending.values()):
            if not pending.answer.done():
                pending.answer.set_result("cancel")


__all__ = [
    "ACCESS_REQUEST_KIND",
    "CATEGORY_TIERS",
    "GRANT_IDLE_SECONDS",
    "REQUEST_TIMEOUT_SECONDS",
    "TIERS",
    "TIER_WORDS",
    "AccessRequests",
    "Grant",
    "Resolution",
    "SessionKey",
    "SessionState",
    "Sessions",
    "category_tier",
    "request_message",
    "resolve_app",
    "resolve_apps",
    "tier_allows",
    "tier_text",
    "window_visible",
]
