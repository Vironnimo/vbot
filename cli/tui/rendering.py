"""Terminal presentation of semantic timeline entries supplied by the server."""

from __future__ import annotations

import json
import re
from typing import Any

from textual.containers import Vertical
from textual.markup import escape
from textual.widgets import Collapsible, Markdown, Static

from core.utils.ids import is_safe_id

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
                result.append(f"\n📎 {label}")
        return terminal_text("\n".join(result))
    return "" if content is None else terminal_text(str(content))


def pretty(value: Any) -> str:
    return terminal_text(json.dumps(value, indent=2, ensure_ascii=False, default=str))


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
    kind = item.get("type")
    if kind == "message":
        return str((item.get("message") or {}).get("role", "message")).capitalize()
    if kind == "user_message":
        return "User Steer · Delivered"
    if kind == "takeover_separator":
        return "Agent takeover"
    if kind == "compaction_separator":
        status = item.get("status") or "completed"
        if status not in {"running", "failed", "completed"}:
            status = "completed"
        return f"Compaction · {status}"
    return None


def tool_detail(item: JsonObject) -> str:
    display = item.get("display") or {}
    hidden = display.get("hidden_argument_keys") or []
    args = {key: value for key, value in (item.get("arguments") or {}).items() if key not in hidden}
    parts = ["Arguments", pretty(args)]
    if item.get("output"):
        parts.extend(["Output", content_text(item["output"])])
    if item.get("result") is not None:
        parts.extend(["Result", pretty(item["result"])])
    for detail in display.get("details") or []:
        if detail.get("type") == "file_changes":
            for change in detail.get("files", []):
                parts.append(f"{change.get('change', '')} {change.get('path', '')}")
                for hunk in change.get("hunks", []):
                    parts.append(f"@@ -{hunk.get('old_start')} +{hunk.get('new_start')} @@")
                    parts.extend(hunk.get("lines", []))
                if change.get("omitted_lines"):
                    parts.append(f"{change['omitted_lines']} more diff lines omitted by the server")
        else:
            parts.append(pretty(detail))
    if display.get("media"):
        parts.extend(["Files (download through F8)", pretty(display["media"])])
    return terminal_text("\n\n".join(parts))


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
    TimelineEntry .tool-detail { padding: 1; }
    TimelineEntry.user-entry { border-left: thick $accent; }
    """

    def __init__(self) -> None:
        super().__init__()
        self.item: JsonObject | None = None
        self.entries: dict[str, TimelineEntry] = {}
        self.entry_order: tuple[str, ...] = ()
        self.body: Markdown | Static | None = None
        self.heading: Static | None = None
        self.fold: Collapsible | None = None

    async def update_item(self, item: JsonObject) -> None:
        if self.item is item:
            return
        first = self.item is None
        previous_text = item_text(self.item) if self.item is not None else ""
        self.item = item
        kind = item.get("type")
        if kind == "assistant_run":
            if self.heading is None:
                self.heading = Static(markup=False, classes="entry-heading")
                await self.mount(self.heading)
            self.heading.update(f"Assistant · {item.get('status', '')}")
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
            title = f"{item.get('name', 'Tool')} · {item.get('status', '')}"
            summary = (item.get("display") or {}).get("summary")
            if summary:
                title += f" · {summary}"
            title = escape(terminal_text(title))
            if first:
                self.body = Static(tool_detail(item), markup=False, classes="tool-detail")
                self.fold = Collapsible(self.body, title=title, collapsed=True)
                await self.mount(self.fold)
            else:
                assert self.body is not None and self.fold is not None
                self.fold.title = title
                self.body.update(tool_detail(item))
        elif kind == "reasoning":
            if first:
                self.body = Markdown(text, open_links=False)
                self.fold = Collapsible(self.body, title="Reasoning", collapsed=True)
                await self.mount(self.fold)
            else:
                assert self.body is not None
                assert isinstance(self.body, Markdown)
                if text.startswith(previous_text):
                    await self.body.append(text[len(previous_text) :])
                else:
                    await self.body.update(text)
        else:
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
