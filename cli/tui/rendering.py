"""Terminal presentation of semantic timeline entries supplied by the server."""

from __future__ import annotations

import json
import re
import textwrap
from datetime import UTC, datetime
from typing import Any

from core.utils.ids import is_safe_id

# A Tool row is one line; the complete payload stays in its expanded detail.
_TOOL_TITLE_WIDTH = 72

# A notice block names its level; its text reads as the message's own sentence.
_NOTICE_LABELS = {"warning": "Warning", "error": "Error"}
_MEMORY_CHANGE_MARKS = {"added": "+", "removed": "-", "replaced": "~"}

type JsonObject = dict[str, Any]

_CONTROL = re.compile(
    r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]|[\x00-\x08\x0b-\x1f\x7f-\x9f]"
)


def terminal_text(value: str) -> str:
    """Keep content text from emitting terminal control sequences."""
    return _CONTROL.sub("", value)


def content_text(content: Any) -> str:
    if isinstance(content, str):
        return terminal_text(content)
    if isinstance(content, list):
        result: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                result.append(str(block.get("text", "")))
            elif block.get("type") in {"media", "file", "image", "file_mention"}:
                label = block.get(
                    "filename", block.get("path", block.get("attachment_id", "attachment"))
                )
                result.append(f"\nfile: {label}")
        return terminal_text("\n".join(result))
    return "" if content is None else terminal_text(str(content))


def one_line(value: str, width: int = 96) -> str:
    """One picker row for content that may span many lines."""
    return textwrap.shorten(" ".join(value.split()), width=width, placeholder="…")


def truncated(value: str, width: int) -> str:
    """Cut a one-line value to the room it has, so nothing wraps out of sight.

    `one_line` drops whole words and suits prose; a row that carries a path keeps
    its beginning instead, which is what the width is there to show.
    """
    if len(value) <= width:
        return value
    return f"{value[: max(1, width - 1)]}…"


def scalar_text(value: Any) -> str:
    if value is None:
        return "none"
    if isinstance(value, bool):
        return "yes" if value else "no"
    return terminal_text(str(value))


def structured(value: Any, *, level: int = 0) -> str:
    """Read a payload as indented `key: value` lines instead of raw JSON.

    Detail dialogs and expanded Tool rows show machine payloads to a human, so
    braces, quotes and commas only cost attention here.
    """
    pad = "  " * level
    if isinstance(value, dict):
        lines: list[str] = []
        for key, item in value.items():
            if isinstance(item, (dict, list)) and item:
                lines.append(f"{pad}{key}:")
                lines.append(structured(item, level=level + 1))
            elif isinstance(item, str) and "\n" in item:
                lines.append(f"{pad}{key}:")
                lines.append("\n".join(f"{pad}  {line}" for line in item.splitlines()))
            elif item == "" or item is None:
                continue
            else:
                lines.append(f"{pad}{key}: {scalar_text(item)}")
        return "\n".join(lines)
    if isinstance(value, list):
        if not value:
            return ""
        lines = []
        for item in value:
            if isinstance(item, (dict, list)) and item:
                lines.append(f"{pad}-")
                lines.append(structured(item, level=level + 1))
            else:
                lines.append(f"{pad}- {scalar_text(item)}")
        return "\n".join(lines)
    return f"{pad}{scalar_text(value)}"


def pretty(value: Any) -> str:
    """Kept name for callers that render a payload for a human to read."""
    return structured(value)


def item_text(item: JsonObject) -> str:
    kind = item.get("type")
    if kind == "message":
        return content_text((item.get("message") or {}).get("content"))
    if kind == "user_message":
        content = item.get("content")
        if content is None:
            content = (item.get("message") or {}).get("content")
        return content_text(content)
    if kind == "takeover_separator":
        message = item.get("message") or {}
        content = message.get("content")
        if isinstance(content, str):
            try:
                content = json.loads(content)
            except ValueError:
                content = None
        if isinstance(content, dict):
            source, destination = content.get("from"), content.get("to")
            if isinstance(source, str) and isinstance(destination, str):
                return terminal_text(f"{source} → {destination}")
        return "Control passed to another Agent."
    if kind == "compaction_separator":
        status = item.get("status") or "completed"
        if status not in {"running", "failed", "completed"}:
            status = "completed"
        descriptions = {
            "running": "Context compaction is running.",
            "failed": "Context compaction failed.",
            "completed": "Context compaction completed.",
        }
        details: list[str] = []
        message = item.get("message") or {}
        usage = message.get("usage") or {}
        before = item.get("contextTokensBefore", usage.get("context_tokens_before"))
        after = item.get("contextTokensAfter", usage.get("context_tokens_after"))
        if isinstance(before, (int, float)) and isinstance(after, (int, float)):
            details.append(f"Context: {before:,} → {after:,} tokens")
        duration_ms = item.get("durationMs", usage.get("compaction_duration_ms"))
        if isinstance(duration_ms, (int, float)) and duration_ms >= 0:
            details.append(f"Duration: {duration_text(duration_ms)}")
        return terminal_text("\n".join([descriptions[status], *details]))
    return content_text(item.get("content", item.get("text", "")))


def item_heading(item: JsonObject) -> str | None:
    """Name only the rows whose kind is not obvious from how they look."""
    kind = item.get("type")
    if kind == "message":
        role = str((item.get("message") or {}).get("role", "message"))
        # The operator's own rows carry an accent bar, model output reads as the
        # default body; a label above each of them repeats what the eye sees.
        return None if role in {"user", "assistant"} else role.capitalize()
    if kind == "user_message":
        return "User Steer · delivered"
    if kind == "takeover_separator":
        return "Agent takeover"
    if kind == "compaction_separator":
        status = item.get("status") or "completed"
        if status not in {"running", "failed", "completed"}:
            status = "completed"
        return f"Compaction · {status}"
    return None


def tool_title(item: JsonObject) -> str:
    """One readable line per Tool call, shortened instead of clipped at the edge."""
    title = str(item.get("name", "Tool"))
    summary = (item.get("display") or {}).get("summary")
    if summary:
        title += f" · {terminal_text(str(summary))}"
    status = str(item.get("status") or "")
    # A Tool that ran through needs no verdict in its row; running and failed
    # calls are the two the operator must not read past.
    if status not in {"success", "completed", ""}:
        title += f" · {status}"
    return textwrap.shorten(title, width=_TOOL_TITLE_WIDTH, placeholder="…")


def duration_text(milliseconds: float) -> str:
    """A duration as the WebUI writes it: 0.4s, 12s, 1m 5s, 1h 2m."""
    seconds = max(0.0, milliseconds / 1000)
    if seconds < 10:
        return f"{seconds:.1f}s"
    if seconds < 60:
        return f"{round(seconds)}s"
    hours, rest = divmod(round(seconds), 3600)
    minutes, seconds_left = divmod(rest, 60)
    return f"{hours}h {minutes}m" if hours else f"{minutes}m {seconds_left}s"


def _instant(value: Any) -> datetime | None:
    try:
        instant = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return instant if instant.tzinfo else instant.replace(tzinfo=UTC)


def tool_time(item: JsonObject, now: datetime | None = None) -> str:
    """How long a Tool call ran, or has run so far; empty while it is prepared."""
    status = str(item.get("status") or "")
    timing = item.get("timing") or {}
    if status == "running":
        started = _instant(
            timing.get("started_at")
            or (item.get("startedEvent") or {}).get("timestamp")
            or item.get("timestamp")
        )
        if started is None:
            return ""
        elapsed = ((now or datetime.now(UTC)) - started).total_seconds() * 1000
        return duration_text(elapsed)
    if status in {"preparing", "pending", "queued", "interrupted"}:
        return ""
    duration = item.get("durationMs")
    if not isinstance(duration, (int, float)):
        duration = timing.get("duration_ms")
    if not isinstance(duration, (int, float)):
        started, ended = _instant(timing.get("started_at")), _instant(timing.get("completed_at"))
        if started is None or ended is None or ended < started:
            return ""
        duration = (ended - started).total_seconds() * 1000
    return duration_text(duration) if duration >= 0 else ""


def tool_detail(item: JsonObject) -> str:
    display = item.get("display") or {}
    hidden = display.get("hidden_argument_keys") or []
    args = {key: value for key, value in (item.get("arguments") or {}).items() if key not in hidden}
    parts: list[str] = []
    # Each section's lines sit indented under its label.
    if args:
        parts.append(f"Arguments\n{_indent(structured(args))}")
    if item.get("output"):
        parts.append(f"Output\n{_indent(content_text(item['output']))}")
    if item.get("result") is not None:
        parts.append(f"Result\n{_indent(structured(_json_value(item['result'])))}")
    for detail in display.get("details") or []:
        if not isinstance(detail, dict):
            continue
        if detail.get("type") == "file_changes":
            for change in detail.get("files", []):
                parts.append(f"{change.get('change', '')} {change.get('path', '')}".strip())
                for hunk in change.get("hunks", []):
                    parts.append(f"@@ -{hunk.get('old_start')} +{hunk.get('new_start')} @@")
                    parts.extend(hunk.get("lines", []))
                if change.get("omitted_lines"):
                    parts.append(f"{change['omitted_lines']} more diff lines omitted by the server")
            continue
        block = _detail_block(detail, item)
        if block:
            parts.append(block)
    if display.get("media"):
        parts.extend(["Files (download through F8)", structured(display["media"])])
    return terminal_text("\n\n".join(parts))


def _indent(text: str, pad: str = "  ") -> str:
    return "\n".join(f"{pad}{line}" if line else "" for line in text.splitlines())


def _path_value(value: Any, path: list[Any]) -> Any:
    for key in path:
        if isinstance(value, dict) and isinstance(key, str):
            value = value.get(key)
        elif isinstance(value, list) and isinstance(key, int) and 0 <= key < len(value):
            value = value[key]
        else:
            return None
    return value


def _source_text(source: Any, item: JsonObject) -> str:
    """Read a detail block's value from the call's arguments or its own result."""
    if not isinstance(source, dict):
        return ""
    origin = source.get("from")
    path = source.get("path")
    if origin not in {"arguments", "result"} or not isinstance(path, list):
        return ""
    root = item.get("arguments") if origin == "arguments" else _json_value(item.get("result"))
    value = _path_value(root, path)
    if value is None or value == "":
        return ""
    if isinstance(value, dict | list):
        return structured(value)
    return scalar_text(value)


def _detail_block(block: JsonObject, item: JsonObject) -> str:
    """Read one of a Tool's structured detail blocks as text a human reads."""
    kind = block.get("type")
    if kind == "notice":
        text = scalar_text(block.get("text"))
        prefix = _NOTICE_LABELS.get(str(block.get("level")))
        return f"{prefix}: {text}" if prefix else text
    if kind == "text":
        label = scalar_text(block.get("label") or "text")
        given = block.get("text")
        text = given if isinstance(given, str) else _source_text(block.get("source"), item)
        text = text.strip()
        if not text:
            return ""
        return f"{label}:\n{_indent(text)}" if "\n" in text else f"{label}: {text}"
    if kind == "results":
        rows: list[str] = []
        for entry in block.get("items") or []:
            if not isinstance(entry, dict):
                continue
            head = " · ".join(
                scalar_text(entry[field]) for field in ("title", "meta", "time") if entry.get(field)
            )
            if head:
                rows.append(head)
            body = entry.get("text")
            if isinstance(body, str) and body.strip():
                rows.append(_indent(body.strip()))
        return "\n".join(rows)
    if kind == "memory_changes":
        revision = block.get("revision")
        rows = [
            f"Memory ({scalar_text(block.get('scope'))})"
            + (f" · revision {revision}" if isinstance(revision, int) else "")
        ]
        for change in block.get("changes") or []:
            if not isinstance(change, dict):
                continue
            mark = _MEMORY_CHANGE_MARKS.get(str(change.get("op")), "·")
            previous = change.get("previous")
            if change.get("op") == "replaced" and previous:
                rows.append(f"{mark} {scalar_text(previous)} → {scalar_text(change.get('text'))}")
            else:
                rows.append(f"{mark} {scalar_text(change.get('text'))}")
        return "\n".join(rows)
    return structured(block)


def flatten(items: list[JsonObject]) -> list[JsonObject]:
    result = []
    for item in items:
        result.append(item)
        if item.get("type") == "assistant_run":
            result.extend(item.get("items", []))
    return result


def file_links(items: list[JsonObject]) -> list[tuple[str, str]]:
    """Expose delivery links for an explicit local download; never fetch them here."""
    result: dict[str, str] = {}
    for item in flatten(items):
        for media in (item.get("display") or {}).get("media") or []:
            if isinstance(media, dict) and isinstance(media.get("url"), str):
                result[media["url"]] = str(
                    media.get("filename") or media.get("label") or media["url"]
                )
        for label, url in re.findall(r"\[([^\]]*)\]\(([^\s)]+)(?:\s+[^)]*)?\)", item_text(item)):
            if any(
                prefix in url
                for prefix in ("/api/files/", "/api/attachments/", "/api/speech/artifacts/")
            ):
                result[url] = label or url
        message = item.get("message")
        if not isinstance(message, dict):
            message = item
        content = message.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("attachment_id"):
                    url = f"/api/attachments/{block['attachment_id']}"
                    result[url] = str(block.get("filename") or block["attachment_id"])
        tool_result = _json_value(item.get("result")) if item.get("type") == "tool_call" else None
        if isinstance(tool_result, dict):
            artifacts = tool_result.get("artifacts")
            if isinstance(artifacts, list):
                for artifact in artifacts:
                    _add_artifact_link(result, artifact)
            data = tool_result.get("data")
            if isinstance(data, dict):
                _add_artifact_link(result, data.get("artifact"))
    return [(label, url) for url, label in result.items()]


def _json_value(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _add_artifact_link(result: dict[str, str], artifact: Any) -> None:
    if not isinstance(artifact, dict):
        return
    attachment_id = artifact.get("attachment_id")
    if artifact.get("kind") == "read_media" and is_safe_id(attachment_id):
        result[f"/api/attachments/{attachment_id}"] = str(artifact.get("filename") or attachment_id)
        return
    url = artifact.get("url")
    if not isinstance(url, str) or not any(
        prefix in url for prefix in ("/api/files/", "/api/attachments/", "/api/speech/artifacts/")
    ):
        return
    result[url] = str(artifact.get("filename") or artifact.get("label") or url)
