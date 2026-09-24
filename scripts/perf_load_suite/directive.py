"""The ``[[perf ...]]`` directive that scripts one fake-Provider turn.

The load driver embeds one directive in every User message it sends. The fake
Provider finds it in the latest real User message of each request and derives
the response for that request from it plus the number of Tool-call rounds the
Agent already completed since that message.

Syntax (whitespace-separated ``key=value`` pairs, ``id`` required)::

    [[perf id=L10-s003-t1 steps=4 tokens=400 rate=80 think_ms=600
      tools=read,search_files,bash calls=1]]
    [[perf id=L10-s003-w1 warmup_tokens=20000 rate=0]]

- ``steps``: Model requests per turn. The first ``steps - 1`` responses are
  Tool calls, the last one streams text.
- ``tokens`` / ``rate`` / ``think_ms``: the final text response waits
  ``think_ms`` before its first token, then streams ``tokens`` filler tokens at
  ``rate`` tokens per second (``rate=0`` streams unpaced).
- ``tools`` / ``calls``: the Tool names rotated through the Tool rounds and how
  many parallel calls each round carries.
- ``warmup_tokens``: a single large text response used to grow Session history.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

DIRECTIVE_PATTERN = re.compile(r"\[\[perf\s+([^\[\]]*)\]\]")
DEFAULT_TOOLS: tuple[str, ...] = ("read", "search_files", "bash")
MAX_CALLS_PER_ROUND = 8
_TAG_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,80}$")
_TOOL_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_INTEGER_FIELDS = ("steps", "tokens", "think_ms", "calls", "warmup_tokens")
_KNOWN_KEYS = frozenset({"id", "rate", "tools", *_INTEGER_FIELDS})


class DirectiveError(ValueError):
    """A ``[[perf ...]]`` directive is present but malformed."""


@dataclass(frozen=True)
class PerfDirective:
    """One scripted turn for the fake Provider."""

    tag: str
    steps: int = 1
    tokens: int = 100
    rate: float = 80.0
    think_ms: int = 0
    tools: tuple[str, ...] = field(default=DEFAULT_TOOLS)
    calls: int = 1
    warmup_tokens: int = 0

    def __post_init__(self) -> None:
        if not _TAG_PATTERN.match(self.tag):
            raise DirectiveError(f"invalid directive id {self.tag!r}")
        if self.steps < 1:
            raise DirectiveError("steps must be at least 1")
        if self.tokens < 0 or self.think_ms < 0 or self.warmup_tokens < 0:
            raise DirectiveError("tokens, think_ms and warmup_tokens must not be negative")
        if self.rate < 0:
            raise DirectiveError("rate must not be negative")
        if not 1 <= self.calls <= MAX_CALLS_PER_ROUND:
            raise DirectiveError(f"calls must be between 1 and {MAX_CALLS_PER_ROUND}")
        if not self.tools or any(not _TOOL_NAME_PATTERN.match(name) for name in self.tools):
            raise DirectiveError("tools must be a non-empty comma-separated list of Tool names")
        if self.warmup_tokens and self.steps != 1:
            raise DirectiveError("a warmup directive cannot also script Tool steps")

    @property
    def is_warmup(self) -> bool:
        return self.warmup_tokens > 0

    @property
    def tool_rounds(self) -> int:
        """Number of Tool-call responses before the final text response."""
        return self.steps - 1

    @property
    def text_tokens(self) -> int:
        return self.warmup_tokens if self.is_warmup else self.tokens

    def scripted_text_ms(self) -> float:
        """Provider time the final text response is scripted to take."""
        streaming_ms = self.text_tokens / self.rate * 1000.0 if self.rate > 0 else 0.0
        return self.think_ms + streaming_ms

    def render(self) -> str:
        """Return the directive text the fake Provider parses back."""
        parts = [f"id={self.tag}"]
        if self.is_warmup:
            parts.append(f"warmup_tokens={self.warmup_tokens}")
        else:
            parts.extend(
                [
                    f"steps={self.steps}",
                    f"tokens={self.tokens}",
                    f"tools={','.join(self.tools)}",
                    f"calls={self.calls}",
                ]
            )
        parts.append(f"rate={_format_rate(self.rate)}")
        parts.append(f"think_ms={self.think_ms}")
        return f"[[perf {' '.join(parts)}]]"


def find_directive(text: str) -> PerfDirective | None:
    """Return the first directive in ``text``; ``None`` when there is none.

    Raises :class:`DirectiveError` for a directive that is present but invalid,
    so a scripting mistake fails the turn visibly instead of silently changing
    what the fake Provider does.
    """
    match = DIRECTIVE_PATTERN.search(text)
    if match is None:
        return None
    return parse_directive_body(match.group(1))


def parse_directive_body(body: str) -> PerfDirective:
    """Parse the ``key=value`` pairs between ``[[perf`` and ``]]``."""
    values: dict[str, str] = {}
    for item in body.split():
        key, separator, value = item.partition("=")
        if not separator or not key or not value:
            raise DirectiveError(f"expected key=value, got {item!r}")
        if key not in _KNOWN_KEYS:
            raise DirectiveError(f"unknown directive key {key!r}")
        if key in values:
            raise DirectiveError(f"duplicate directive key {key!r}")
        values[key] = value
    if "id" not in values:
        raise DirectiveError("directive requires id=<tag>")

    integers = {key: _parse_int(key, values[key]) for key in _INTEGER_FIELDS if key in values}
    rate = _parse_float("rate", values["rate"]) if "rate" in values else None
    tools = (
        tuple(name for name in values["tools"].split(",") if name) if "tools" in values else None
    )

    directive_fields: dict[str, object] = {"tag": values["id"], **integers}
    if rate is not None:
        directive_fields["rate"] = rate
    if tools is not None:
        directive_fields["tools"] = tools
    return PerfDirective(**directive_fields)  # type: ignore[arg-type]


def _parse_int(key: str, value: str) -> int:
    try:
        return int(value)
    except ValueError as exc:
        raise DirectiveError(f"{key} must be an integer, got {value!r}") from exc


def _parse_float(key: str, value: str) -> float:
    try:
        return float(value)
    except ValueError as exc:
        raise DirectiveError(f"{key} must be a number, got {value!r}") from exc


def _format_rate(rate: float) -> str:
    return str(int(rate)) if float(rate).is_integer() else repr(rate)
