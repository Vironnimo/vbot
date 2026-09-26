"""Model-facing text of canonical Tool Results.

Sessions persist every Tool Result as the compact JSON envelope
``{"ok", "error", "data", "artifacts"}``. Provider wires send the Model a
plain-text rendering of it instead: JSON string escaping obscures multi-line
output such as code, logs and diffs, and the envelope's fixed keys cost tokens
without telling the Model anything. The rendering is deterministic, so replayed
history keeps its prompt-cache prefix. Content that is not an envelope, such as
legacy text or a compacted digest, passes through unchanged.
"""

from __future__ import annotations

import json
import textwrap
from collections.abc import Mapping, Sequence
from typing import Any

_ENVELOPE_KEYS = frozenset({"ok", "error", "data", "artifacts"})

# Success data renders its first non-empty string body field verbatim below the
# other fields, so file contents and command output read exactly as produced.
_BODY_FIELDS = ("content", "output")

_EMPTY_SUCCESS_TEXT = "ok"


def tool_result_envelope(content: Any) -> Mapping[str, Any] | None:
    """Return the parsed envelope of canonical Tool message content, if it is one."""

    if not isinstance(content, str) or not content.lstrip().startswith("{"):
        return None
    try:
        value = json.loads(content)
    except ValueError:
        return None
    if not (
        isinstance(value, dict)
        and value.keys() == _ENVELOPE_KEYS
        and isinstance(value["ok"], bool)
        and isinstance(value["artifacts"], list)
    ):
        return None
    if value["ok"]:
        return value if value["error"] is None and isinstance(value["data"], dict) else None
    error = value["error"]
    if value["data"] is not None or not isinstance(error, dict):
        return None
    if not isinstance(error.get("code"), str) or not isinstance(error.get("message"), str):
        return None
    return value


def tool_result_text(content: Any, *, content_blocks: Sequence[Mapping[str, Any]] = ()) -> Any:
    """Return what the Model reads for canonical Tool message content.

    Render exactly once at the final wire boundary: a literal body may itself
    look like an envelope. Supplemental text follows the rendered body.
    """

    envelope = tool_result_envelope(content)
    text = content if envelope is None else render_tool_result_envelope(envelope)
    return _append_text(text, _supplemental_text(content_blocks))


def tool_result_function_response(
    content: Any, *, content_blocks: Sequence[Mapping[str, Any]] = ()
) -> dict[str, Any]:
    """Return a function-response object for wires that require JSON objects.

    Envelopes render to ``{"output": text}`` or, for a failure, ``{"error": text}``.
    Other content keeps a JSON object as-is and wraps anything else as output.
    """

    envelope = tool_result_envelope(content)
    supplemental_text = _supplemental_text(content_blocks)
    if envelope is not None:
        key = "output" if envelope["ok"] else "error"
        return {key: _append_text(render_tool_result_envelope(envelope), supplemental_text)}
    if supplemental_text:
        return {"output": _append_text(content, supplemental_text)}
    if isinstance(content, str):
        try:
            parsed = json.loads(content)
        except ValueError:
            parsed = content
    else:
        parsed = content
    return dict(parsed) if isinstance(parsed, Mapping) else {"output": parsed}


def _supplemental_text(content_blocks: Sequence[Mapping[str, Any]]) -> list[str]:
    return [
        text
        for block in content_blocks
        if block.get("type") == "text" and isinstance(text := block.get("text"), str) and text
    ]


def _append_text(content: Any, supplemental_text: Sequence[str]) -> Any:
    if not supplemental_text:
        return content
    base_text = content if isinstance(content, str) else ""
    return "\n\n".join(part for part in (base_text, *supplemental_text) if part)


def render_tool_result_envelope(envelope: Mapping[str, Any]) -> str:
    """Render one valid Tool Result envelope as Model-facing text.

    A failure reads ``Error (<code>): <message>`` followed by any further error
    fields. A success lists its data fields as ``name: value`` lines, omitting
    null values, and then its body field verbatim after a blank line.
    """

    body: str | None = None
    if envelope["ok"]:
        data: Mapping[str, Any] = envelope["data"]
        body_key = next(
            (key for key in _BODY_FIELDS if isinstance(data.get(key), str) and data[key]),
            None,
        )
        lines = [
            _field(key, value)
            for key, value in data.items()
            if key != body_key and value is not None
        ]
        if body_key is not None:
            body = data[body_key]
    else:
        error: Mapping[str, Any] = envelope["error"]
        lines = [f"Error ({error['code']}): {error['message']}"]
        lines.extend(
            _field(key, value)
            for key, value in error.items()
            if key not in {"code", "message"} and value is not None
        )
    if envelope["artifacts"]:
        lines.append(_field("artifacts", envelope["artifacts"]))
    header = "\n".join(lines)
    if body is None:
        return header or _EMPTY_SUCCESS_TEXT
    return f"{header}\n\n{body}" if header else body


def _field(key: str, value: Any) -> str:
    if isinstance(value, str):
        if not value:
            return f'{key}: ""'
        if "\n" in value or "\r" in value:
            return f"{key}:\n{textwrap.indent(value, '  ')}"
        return f"{key}: {value}"
    return f"{key}: {json.dumps(value, ensure_ascii=False, separators=(',', ':'))}"
