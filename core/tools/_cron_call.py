"""Collect cron-call refusals and render their complete corrected calls."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from core.tools.contracts import ToolContractError

TIMEZONE_FIELD = "timezone"
ENABLED_FIELD = "enabled"


OMIT = object()
"""``render_call`` override that removes a field from the rendered call."""


class CronCallRefusedError(ValueError):
    """A ``cron`` call was refused before any side effect; the message names the fix."""


REFUSAL_PREFIX = "cron was not run: "


_CALL_ORDER = (
    "action",
    "id",
    "event_id",
    "target",
    "name",
    "prompt",
    "schedule",
    "repeat",
    "session",
    "deliver",
    "watch",
)


_LONG_PROMPT = 120
_LONG_PROMPT_STAND_IN = "<the prompt from this call>"


class _Problems:
    """Refusals collected while a call is read, reported together with one corrected call."""

    def __init__(self) -> None:
        self.texts: list[str] = []
        self.choice: tuple[str, list[dict[str, Any]]] | None = None
        self.overrides: dict[str, Any] = {}

    def add(self, text: str, **overrides: Any) -> None:
        """Refuse the call; ``overrides`` replace refused values in the corrected call."""
        self.texts.append(text)
        self.overrides.update(overrides)

    def choose(self, text: str, alternatives: list[dict[str, Any]]) -> None:
        """Refuse a call with several plausible readings; each alternative overrides fields."""
        if self.choice is None:
            self.choice = (text, alternatives)
        else:
            self.texts.append(text)

    def raise_if_any(self, arguments: Mapping[str, Any]) -> None:
        call = {**arguments, **self.overrides}
        if self.choice is not None:
            text, alternatives = self.choice
            calls = " or ".join(render_call(call, **overrides) for overrides in alternatives)
            raise ToolContractError(REFUSAL_PREFIX + " ".join([*self.texts, text, calls]))
        if self.texts:
            texts = " ".join(self.texts)
            raise ToolContractError(f"{REFUSAL_PREFIX}{texts} Send: {render_call(call)}")


def render_call(arguments: Mapping[str, Any], **overrides: Any) -> str:
    """Render a canonical ``cron`` call as compact JSON; ``OMIT`` removes a field."""
    call = {**arguments, **overrides}
    ordered: dict[str, Any] = {}
    for name in (*_CALL_ORDER, TIMEZONE_FIELD, ENABLED_FIELD):
        if name in call and call[name] is not OMIT:
            ordered[name] = call[name]
    prompt = ordered.get("prompt")
    if isinstance(prompt, str) and len(prompt) > _LONG_PROMPT:
        ordered["prompt"] = _LONG_PROMPT_STAND_IN
    return json.dumps(ordered, ensure_ascii=False, separators=(",", ":"))


def refusal(text: str, arguments: Mapping[str, Any], **overrides: Any) -> str:
    """Return a refusal message that ends with the corrected call."""
    return f"{REFUSAL_PREFIX}{text} Send: {render_call(arguments, **overrides)}"


def _set_field(arguments: dict[str, Any], name: str, item: Any, problems: _Problems) -> None:
    if name in arguments and arguments[name] != item:
        problems.add(f"it gives two different values for {name}; provide one.")
        return
    arguments[name] = item
