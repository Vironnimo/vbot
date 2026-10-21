"""Accessor-local draft/view state and application of the server's timeline wire format.

These records contain no history/Run reconciliation policy: the server supplies
complete semantic rows and explicit keyed replacements/removals.
"""

from __future__ import annotations

import copy
import uuid
from dataclasses import dataclass, field
from typing import Any

type JsonObject = dict[str, Any]


class TimelineGapError(Exception):
    """A suffix patch requires a fresh authoritative stream snapshot."""


def _run_id(item: JsonObject) -> str | None:
    return item.get("runId", item.get("run_id"))


def _facts(response: JsonObject) -> JsonObject:
    # Content has one owner. Retaining the wire response here kept every raw
    # Message and an extra timeline alive after the visible cache was replaced.
    return {key: value for key, value in response.items() if key not in {"items", "messages"}}


def _reuse(previous: Any, incoming: Any) -> Any:
    """Reuse equal snapshot subtrees; published timeline objects stay immutable."""
    if previous is incoming:
        return previous
    if isinstance(previous, dict) and isinstance(incoming, dict):
        values = {key: _reuse(previous.get(key), value) for key, value in incoming.items()}
        if previous.keys() == values.keys() and all(values[key] is previous[key] for key in values):
            return previous
        return values
    if isinstance(previous, list) and isinstance(incoming, list):
        entries = [
            _reuse(previous[index] if index < len(previous) else None, value)
            for index, value in enumerate(incoming)
        ]
        if len(previous) == len(entries) and all(
            left is right for left, right in zip(previous, entries, strict=True)
        ):
            return previous
        return entries
    return previous if type(previous) is type(incoming) and previous == incoming else incoming


def _snapshot_items(previous: list[JsonObject], incoming: list[JsonObject]) -> list[JsonObject]:
    known = {item["id"]: item for item in previous}
    return [
        _reuse(
            known.get(item["id"]), _retain_window_prefix(known.get(item["id"]), copy.deepcopy(item))
        )
        for item in incoming
    ]


def _retain_window_prefix(previous: JsonObject | None, incoming: JsonObject) -> JsonObject:
    start = incoming.get("window_start")
    if previous is None or not isinstance(start, int):
        return incoming
    incoming_ids = {child["id"] for child in incoming.get("items", [])}
    prefix = [
        child
        for child in previous.get("items", [])
        if child.get("type") != "history_gap"
        and child["id"] not in incoming_ids
        and child.get("position", start) < start
    ]
    if not prefix:
        return incoming
    gap = [child for child in previous["items"] if child.get("type") == "history_gap"]
    tail = [child for child in incoming.get("items", []) if child.get("type") != "history_gap"]
    return {**incoming, "items": gap + prefix + tail}


@dataclass
class Conversation:
    agent: str
    session: str | None = None
    project: str | None = None
    project_selected: bool = False
    title: str = "New conversation"
    draft: str = ""
    attachments: list[JsonObject] = field(default_factory=list)
    mentions: list[str] = field(default_factory=list)
    overrides: JsonObject = field(default_factory=dict)
    scroll_y: float = 0
    follow_tail: bool = True
    reading_anchor: tuple[str, float] | None = None
    row_heights: dict[str, int] = field(default_factory=dict)
    expanded_rows: set[str] = field(default_factory=set)
    measured_width: int = 0
    cache_bytes: int = 0
    unread_output: bool = False
    uncertain: bool = False
    uncertain_submission: JsonObject | None = None
    pending_session_id: str | None = None
    items: list[JsonObject] = field(default_factory=list)
    facts: JsonObject = field(default_factory=dict)
    sequence: int = -1
    stream_run: str | None = None
    next_before: str | None = None
    parent: JsonObject | None = None
    refresh_revision: int = 0
    completion_run_id: str | None = None
    completion_read: str | None = None
    completion_read_pending: str | None = None
    completion_read_failed: str | None = None
    available: bool = True
    draft_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    @property
    def key(self) -> tuple[str, str | None, bool, str | None]:
        if self.session:
            return self.agent, self.session, False, None
        return (
            self.agent,
            None,
            self.project_selected,
            self.project if self.project_selected else None,
        )

    def params(self, *, draft: bool = False) -> JsonObject:
        result: JsonObject = {"agent_id": self.agent}
        if self.session:
            result["session_id"] = self.session
        elif draft and self.project_selected and "@" not in self.agent:
            result["working_project_id"] = self.project
        return result

    def submission(self, text: str, *, attachments: bool = True) -> JsonObject:
        """The `chat.stream` request; ``attachments=False`` leaves the uploads in the draft."""
        result = self.params()
        result["reply_surface"] = "tui"
        if self.session is None:
            result["new_session"] = {}
            if self.pending_session_id:
                result["new_session"]["session_id"] = self.pending_session_id
            if self.project_selected and "@" not in self.agent:
                result["new_session"]["working_project_id"] = self.project
            if self.overrides:
                result["new_session"]["agent_overrides"] = dict(self.overrides)
        if attachments and self.attachments:
            blocks = [{"type": "text", "text": text}] if text else []
            for attachment in self.attachments:
                media_type = attachment.get("media_type", "application/octet-stream")
                blocks.append(
                    {
                        "type": "media"
                        if media_type.split("/", 1)[0] in {"image", "audio", "video"}
                        else "file",
                        "attachment_id": attachment["attachment_id"],
                        "filename": attachment["filename"],
                        "media_type": media_type,
                    }
                )
            result["content"] = blocks
        else:
            result["content"] = text
        return result

    def page(self, response: JsonObject, *, older: bool = False) -> None:
        if not older:
            active = response.get("active_run") or {}
            current_run = active.get("run_id")
            if current_run != self.stream_run:
                self.stream_run, self.sequence = current_run, -1
        reset = response.get("history_reset") or (
            self.facts.get("history_generation")
            and response.get("history_generation")
            and self.facts["history_generation"] != response["history_generation"]
        )
        incoming = _snapshot_items([] if reset else self.items, response.get("items", []))
        if older:
            known = {item["id"] for item in self.items}
            self.items = [item for item in incoming if item["id"] not in known] + self.items
        elif (
            response.get("incremental")
            and not response.get("history_reset")
            and (
                not self.facts.get("history_generation")
                or response.get("history_generation", self.facts["history_generation"])
                == self.facts["history_generation"]
            )
        ):
            replaced = set(response.get("replace_run_ids", []))
            ids = {item["id"] for item in incoming}
            positions = [
                index
                for index, item in enumerate(self.items)
                if _run_id(item) in replaced or item["id"] in ids
            ]
            position = positions[0] if positions else len(self.items)
            self.items = [
                item
                for item in self.items
                if _run_id(item) not in replaced and item["id"] not in ids
            ]
            self.items[position:position] = incoming
            self.facts.update(_facts(response))
            if "active_run" not in response:
                self.facts.pop("active_run", None)
            self.sequence = response.get("timeline_sequence", self.sequence)
        else:
            self.items = incoming
            self.facts = _facts(response)
            self.sequence = response.get("timeline_sequence", -1)
        if older or not response.get("incremental"):
            self.next_before = response.get("next_before") if response.get("has_more") else None

    def release_history(self) -> None:
        """Evict reconstructible content while retaining operator-owned state."""
        self.items = []
        self.facts = {}
        self.next_before = None
        self.sequence = -1
        self.stream_run = None
        self.row_heights.clear()
        self.cache_bytes = 0

    def child_page(self, response: JsonObject) -> bool:
        """Merge a page of earlier Run children; false when its gap is gone."""
        run_id = response["run_id"]
        for index, run in enumerate(self.items):
            if run.get("type") != "assistant_run" or _run_id(run) != run_id:
                continue
            # The page continues exactly the gap it was read for; once the Run
            # was reset or extended past it, merging would leave a hole.
            gap = next(
                (child for child in run["items"] if child.get("type") == "history_gap"), None
            )
            if gap is None or gap.get("before") != response.get("before"):
                return False
            children = {
                child["id"]: child for child in run["items"] if child.get("type") != "history_gap"
            }
            for child in response["items"]:
                children.setdefault(child["id"], child)
            replacement = {
                **run,
                "items": sorted(children.values(), key=lambda child: child.get("position", 0)),
            }
            self.items = [*self.items[:index], replacement, *self.items[index + 1 :]]
            return True
        return False

    def event(self, event: JsonObject) -> bool:
        """Apply only explicit wire edits; false means stale/duplicate delivery."""
        kind = event.get("type")
        payload = event.get("payload", event)
        run_id = payload.get("run_id")
        sequence = payload.get("sequence", 0)
        if kind == "timeline_snapshot":
            if self.stream_run == run_id and sequence < self.sequence:
                return False
            self.stream_run, self.sequence = run_id, sequence
            replacement = _snapshot_items(self.items, payload.get("items", []))
            ids = {item["id"] for item in replacement}
            first = next(
                (
                    index
                    for index, item in enumerate(self.items)
                    if item.get("runId", item.get("run_id")) == run_id or item["id"] in ids
                ),
                len(self.items),
            )
            self.items = [
                item
                for item in self.items
                if item.get("runId", item.get("run_id")) != run_id and item["id"] not in ids
            ]
            self.items[first:first] = replacement
            self._live_facts(payload)
            return True
        if kind != "timeline_update" or (self.stream_run == run_id and sequence <= self.sequence):
            return False
        if payload.get("base_sequence") != self.sequence:
            raise TimelineGapError
        self.stream_run, self.sequence = run_id, sequence
        removed = set(payload.get("remove", []))
        items = [item for item in self.items if item["id"] not in removed]
        positions = {item["id"]: index for index, item in enumerate(items)}
        for entry in payload.get("upsert", []):
            existing = positions.get(entry["id"])
            if existing is None:
                positions[entry["id"]] = len(items)
                items.append(copy.deepcopy(entry))
            else:
                items[existing] = _reuse(items[existing], copy.deepcopy(entry))
        run = next(
            (
                item
                for item in items
                if item.get("type") == "assistant_run"
                and item.get("runId", item.get("run_id")) == run_id
            ),
            None,
        )
        if run is not None:
            updated_run = {**run, **copy.deepcopy(payload.get("run") or {})}
            children = {item["id"]: item for item in run.get("items", [])}
            for item_id in payload.get("remove_children", []):
                children.pop(item_id, None)
            for item in payload.get("children", []):
                children[item["id"]] = copy.deepcopy(item)
            for identifier, position in payload.get("positions", {}).items():
                if identifier in children:
                    children[identifier] = {**children[identifier], "position": position}
            for change in payload.get("child_updates", []):
                child = children.get(change["id"])
                if child is not None:
                    child = dict(child)
                    for key, suffix in change.get("append", {}).items():
                        child[key] = str(child.get(key, "")) + suffix
                    child.update(copy.deepcopy(change.get("set", {})))
                    children[change["id"]] = child
            order = payload.get("order", list(children))
            if payload.get("windowed"):
                order = sorted(children, key=lambda key: children[key].get("position", 0))
            updated_run["items"] = [children[item_id] for item_id in order if item_id in children]
            items[positions[run["id"]]] = updated_run
        self.items = items
        if "item_order" in payload:
            group = {item["id"]: item for item in self.items if item["id"] in payload["item_order"]}
            first = next(
                (index for index, item in enumerate(self.items) if item["id"] in group),
                len(self.items),
            )
            self.items = [item for item in self.items if item["id"] not in group]
            self.items[first:first] = [group[key] for key in payload["item_order"] if key in group]
        self._live_facts(payload)
        return True

    def _live_facts(self, payload: JsonObject) -> None:
        state = payload.get("state") or {}
        for key in ("context_usage", "session_usage"):
            if key in state:
                self.facts[key] = copy.deepcopy(state[key])
        if payload.get("terminal"):
            self.facts.pop("active_run", None)
        elif state:
            self.facts.setdefault("active_run", {}).update(copy.deepcopy(state))
            self.facts["active_run"]["run_id"] = payload["run_id"]
