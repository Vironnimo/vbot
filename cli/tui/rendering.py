"""Terminal presentation of semantic timeline entries supplied by the server."""

from __future__ import annotations

import json
import re
import textwrap
from typing import Any

from textual import on
from textual.containers import Vertical
from textual.markup import escape
from textual.message import Message
from textual.widgets import Button, Collapsible, Markdown, Static

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
            details.append(f"Duration: {duration_ms / 1000:.1f}s")
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
    return escape(textwrap.shorten(title, width=_TOOL_TITLE_WIDTH, placeholder="…"))


def tool_detail(item: JsonObject) -> str:
    display = item.get("display") or {}
    hidden = display.get("hidden_argument_keys") or []
    args = {key: value for key, value in (item.get("arguments") or {}).items() if key not in hidden}
    parts: list[str] = []
    if args:
        parts.extend(["Arguments", structured(args)])
    if item.get("output"):
        parts.extend(["Output", content_text(item["output"])])
    if item.get("result") is not None:
        parts.extend(["Result", structured(_json_value(item["result"]))])
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


class TimelineEntry(Vertical):
    """Update the affected row in place, keeping expansion and text selection local."""

    DEFAULT_CSS = """
    TimelineEntry { height: auto; padding: 0 1; margin-bottom: 1; }
    TimelineEntry > .entry-heading { color: $text-muted; height: auto; }
    TimelineEntry Markdown { height: auto; background: transparent; padding: 0; }
    TimelineEntry Collapsible { padding: 0; margin: 0; border: none; }
    TimelineEntry Collapsible > CollapsibleTitle { color: $text-muted; }
    TimelineEntry.failed-entry Collapsible > CollapsibleTitle { color: $error; }
    TimelineEntry .tool-detail { padding: 1 0 1 2; color: $text-muted; }
    TimelineEntry.reasoning-entry Collapsible > CollapsibleTitle { text-style: italic; }
    TimelineEntry.user-entry { border-left: thick $accent; background: $panel; }
    """

    class LoadHistory(Message):
        def __init__(self, item: JsonObject) -> None:
            super().__init__()
            self.item = item

    @on(Button.Pressed, ".load-run-history")
    def load_history(self, event: Button.Pressed) -> None:
        event.stop()
        if self.item is not None:
            self.post_message(self.LoadHistory(self.item))

    def __init__(self, *, expanded: set[str] | None = None) -> None:
        super().__init__()
        self.expanded = expanded if expanded is not None else set()
        self.item: JsonObject | None = None
        self.entries: dict[str, TimelineEntry] = {}
        self.entry_order: tuple[str, ...] = ()
        self.body: Markdown | Static | None = None
        self.heading: Static | None = None
        self.fold: Collapsible | None = None
        self.detail_text = ""

    @on(Collapsible.Expanded)
    async def disclosure_opened(self, event: Collapsible.Expanded) -> None:
        if event.collapsible is self.fold and self.item is not None:
            self.expanded.add(str(self.item["id"]))
            await self._detail_body()

    @on(Collapsible.Collapsed)
    def disclosure_closed(self, event: Collapsible.Collapsed) -> None:
        if event.collapsible is self.fold and self.item is not None:
            self.expanded.discard(str(self.item["id"]))

    async def _detail_body(self) -> None:
        assert self.item is not None and self.fold is not None
        text = (
            tool_detail(self.item) if self.item.get("type") == "tool_call" else item_text(self.item)
        )
        if self.body is None:
            self.body = (
                Static(text, markup=False, classes="tool-detail")
                if self.item.get("type") == "tool_call"
                else Markdown(text, open_links=False)
            )
            await self.fold.query_one(Collapsible.Contents).mount(self.body)
        elif isinstance(self.body, Markdown):
            if text.startswith(self.detail_text):
                await self.body.append(text[len(self.detail_text) :])
            else:
                await self.body.update(text)
        else:
            if text != self.detail_text:
                self.body.update(text)
        self.detail_text = text

    async def update_item(self, item: JsonObject) -> None:
        if self.item is item:
            return
        first = self.item is None
        previous_text = item_text(self.item) if self.item is not None else ""
        self.item = item
        kind = item.get("type")
        if kind == "history_gap":
            if first:
                await self.mount(Button("Load earlier steps…", classes="load-run-history"))
            return
        if kind == "assistant_run":
            status = str(item.get("status", ""))
            # A finished Run needs no label: the answer speaks for itself. A Run
            # that ended badly is the case where naming it helps.
            label = (
                f"Assistant · {status}" if status in {"failed", "cancelled", "interrupted"} else ""
            )
            if self.heading is None:
                self.heading = Static(markup=False, classes="entry-heading")
                await self.mount(self.heading)
            self.heading.update(label)
            self.heading.display = bool(label)
            self.set_class(status == "failed", "failed-entry")
            children = item.get("items", [])
            wanted = {str(child["id"]) for child in children}
            for key in list(self.entries):
                if key not in wanted:
                    await self.entries.pop(key).remove()
            for child in children:
                key = str(child["id"])
                if key not in self.entries:
                    widget = self.entries[key] = TimelineEntry()
                    await self.mount(widget)
                await self.entries[key].update_item(child)
            order = tuple(str(child["id"]) for child in children)
            if order != self.entry_order:
                for index, child in enumerate(children):
                    self.move_child(
                        self.entries[str(child["id"])],
                        after=self.heading
                        if index == 0
                        else self.entries[str(children[index - 1]["id"])],
                    )
                self.entry_order = order
            return
        text = item_text(item)
        if kind == "tool_call":
            title = tool_title(item)
            self.set_class(str(item.get("status", "")) in {"error", "failed"}, "failed-entry")
            if first:
                self.fold = Collapsible(title=title, collapsed=str(item["id"]) not in self.expanded)
                await self.mount(self.fold)
            else:
                assert self.fold is not None
                self.fold.title = title
            if self.fold is not None and not self.fold.collapsed:
                await self._detail_body()
        elif kind == "reasoning":
            if first:
                self.fold = Collapsible(
                    title="Reasoning", collapsed=str(item["id"]) not in self.expanded
                )
                await self.mount(self.fold)
                self.set_class(True, "reasoning-entry")
            if self.fold is not None and not self.fold.collapsed:
                await self._detail_body()
        else:
            if kind == "run_status":
                text = f"Assistant · {item.get('status', '')}"
            if kind == "model_fallback":
                text = f"*{text or 'Model fallback'}*"
            heading = item_heading(item)
            if heading is not None:
                if self.heading is None:
                    self.heading = Static(markup=False, classes="entry-heading")
                    await self.mount(self.heading)
                self.heading.update(heading)
            if kind in {"message", "user_message"}:
                user_message = kind == "user_message" or (
                    (item.get("message") or {}).get("role") == "user"
                )
                self.set_class(user_message, "user-entry")
            if first:
                self.body = Markdown(text, open_links=False)
                await self.mount(self.body)
            else:
                assert self.body is not None
                assert isinstance(self.body, Markdown)
                if text.startswith(previous_text) and kind not in {
                    "model_fallback",
                    "compaction_separator",
                    "takeover_separator",
                }:
                    await self.body.append(text[len(previous_text) :])
                else:
                    await self.body.update(text)
