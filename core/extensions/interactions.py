"""Neutral channel-interaction contract shared by channels and extensions.

This lives in its own file (not ``extensions.py``, already over the 1000-line
soft limit) because it is a cohesive sub-part of the extensions domain that is
still exposed through the module's public API — the extensions module already
owns every other capability-surface contract type (``HookContext``,
``Deny``/``Modify``/``Replace``, the declarations), and the interaction contract
belongs beside them.

An interactive channel message carries inline buttons; tapping one produces an
:class:`InteractionEvent`. The event's ``data`` is the tapped button's callback
payload, conventionally ``"<prefix>:<payload>"`` — the ``prefix`` (never itself
containing ``:``) routes the tap to the extension that registered a handler for
it, and the ``payload`` is that handler's own private encoding. An extension
handles the tap deterministically in-process (no LLM run) via an
:class:`InteractionResponder`: it acknowledges the tap and optionally edits the
message's text or keyboard.

One prefix, ``run`` (:data:`RUN_TRIGGER_PREFIX`), is reserved by the runtime: a
channel adapter routes such a tap to the conversation engine to *wake the agent*
with the tap context instead of to an extension, so the registry refuses to let
an extension claim it.

Imports nothing from ``core/channels`` or ``core/tools``: the dependency runs
channels → extensions (channels imports these types), never the reverse.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

# Callback-data prefix the runtime reserves for waking the agent: a tap whose
# data is ``run:<payload>`` is routed by the channel adapter to the conversation
# engine (which enqueues an internal Run carrying the tap context), never to an
# extension. The registry skips any extension declaration for a reserved prefix
# (see ``ExtensionRegistry._apply_one_interaction_handler``).
RUN_TRIGGER_PREFIX = "run"
RESERVED_INTERACTION_PREFIXES = frozenset({RUN_TRIGGER_PREFIX})


@dataclass(frozen=True)
class InteractionButton:
    """One inline-keyboard button: its visible ``label`` and callback ``data``.

    ``data`` is the payload delivered back as :attr:`InteractionEvent.data` when
    the button is tapped; by convention it is ``"<prefix>:<payload>"``.
    """

    label: str
    data: str


@dataclass(frozen=True)
class InteractionEvent:
    """One button tap on an interactive channel message.

    ``buttons`` is the message's current keyboard as rows of
    :class:`InteractionButton` (the platform's snapshot at tap time, so a handler
    can recompute the whole keyboard); ``data`` is the tapped button's callback
    payload. ``message_id`` identifies the message to edit — there is no
    server-side persistence, the edit target comes from the tap itself.
    """

    platform: str
    channel_id: str
    chat_id: str
    user_id: str
    message_id: str
    data: str
    buttons: tuple[tuple[InteractionButton, ...], ...]
    text: str | None = None
    user_display_name: str | None = None
    thread_id: str | None = None


class InteractionResponder(Protocol):
    """The handler's reply channel for one tap: acknowledge, and optionally edit.

    ``answer`` stops the tapper's client-side spinner (an empty ``text`` is a
    silent ack; a non-empty one shows a toast, or a modal alert when ``alert``).
    ``edit`` rewrites the message that was tapped — its ``text`` and/or its
    ``buttons`` (keyboard). Every interactive tap must be answered exactly once;
    the channel guarantees a fallback ack if the handler does not answer itself.
    """

    async def answer(self, text: str | None = None, *, alert: bool = False) -> None: ...

    async def edit(
        self,
        *,
        text: str | None = None,
        buttons: list[list[InteractionButton]] | None = None,
    ) -> None: ...


@dataclass(frozen=True)
class InteractionHandlerDeclaration:
    """One ``api.register_interaction_handler`` declaration: routing prefix + handler.

    Collected during ``register`` and applied into the registry's prefix map
    after every extension has registered. The handler is called as
    ``handler(event, responder)`` for each tap whose ``data`` begins with
    ``"<prefix>:"``.
    """

    prefix: str
    handler: Callable[[InteractionEvent, InteractionResponder], Any]
