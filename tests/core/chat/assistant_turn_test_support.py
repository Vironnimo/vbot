"""Chat's side of a Provider response, for tests that follow it into the next request.

Chat persists a normalized Provider response as a canonical Assistant turn, and
a later request replays the persisted Session history through Chat's request
shaping before the Adapter renders it. Chat exposes both steps only inside the
Agentic Loop, so these helpers are the single test entry to them.
"""

from __future__ import annotations

from collections.abc import Sequence

from core.chat.events import _visible_message_payload
from core.chat.messages import ChatMessage, JsonObject
from core.chat.wire_shaping import (
    _assistant_continuation_dict,
    _assistant_message_from_response,
    _embed_notes_into_request,
)
from core.providers.reasoning import DEFAULT_REASONING_REPLAY_POLICY, ReasoningReplayPolicy


def assistant_turn_from_response(
    model: str, response: JsonObject, *, reasoning_scope: str | None = None
) -> ChatMessage:
    """The Assistant turn Chat persists for one normalized Provider response.

    ``model`` is the public ``<provider>/<model-id>``. ``reasoning_scope`` is the
    exact route that produced the response; a later request replays native
    Reasoning only on that route.
    """

    return _assistant_message_from_response(model, response, reasoning_scope=reasoning_scope)


def request_history(
    history: Sequence[ChatMessage],
    *,
    replay_policy: ReasoningReplayPolicy = DEFAULT_REASONING_REPLAY_POLICY,
    agent_model: str | None = None,
    current_turn: ChatMessage | None = None,
) -> list[JsonObject]:
    """The conversation messages Chat hands the Adapter for the next request.

    ``history`` is the persisted Session history. Notes become System Reminders,
    persisted Reasoning replays only as ``replay_policy`` allows and only on the
    exact ``agent_model`` route, and a Tool Call without a Result receives an
    interrupted Result. ``current_turn`` is an Assistant turn of the Run in
    progress: Chat carries it after the history in its continuation form, which
    keeps its Reasoning unless the policy is ``none``; its Tool Results follow.
    """

    messages = _embed_notes_into_request(
        list(history), replay_policy=replay_policy, agent_model=agent_model
    )
    if current_turn is not None:
        messages.append(_assistant_continuation_dict(current_turn, replay_policy=replay_policy))
    return messages


def event_payload(turn: ChatMessage) -> JsonObject:
    """The message a Run event shows for this turn, without opaque Reasoning state."""

    return _visible_message_payload(turn)
