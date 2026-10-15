"""Accessor-local draft/view state and application of the server's timeline wire format.

These records contain no history/Run reconciliation policy: the server supplies
complete semantic rows and explicit keyed replacements/removals.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

type JsonObject = dict[str, Any]


class TimelineGapError(Exception):
    """A suffix patch requires a fresh authoritative stream snapshot."""


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
    uncertain: bool = False
    uncertain_submission: JsonObject | None = None
    items: list[JsonObject] = field(default_factory=list)
    facts: JsonObject = field(default_factory=dict)
    sequence: int = -1
    stream_run: str | None = None
    next_before: str | None = None
    parent: JsonObject | None = None
    refresh_revision: int = 0

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

    def submission(self, text: str) -> JsonObject:
        result = self.params()
        result["reply_surface"] = "tui"
        if self.session is None:
            result["new_session"] = {}
            if self.project_selected and "@" not in self.agent:
                result["new_session"]["working_project_id"] = self.project
            if self.overrides:
                result["new_session"]["agent_overrides"] = dict(self.overrides)
        if self.attachments:
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
        if self.mentions:
            result["file_mentions"] = list(self.mentions)
        return result

    def page(self, response: JsonObject, *, older: bool = False) -> None:
        if not older:
            active = response.get("active_run") or {}
            current_run = active.get("run_id")
            if current_run != self.stream_run:
                self.stream_run, self.sequence = current_run, -1
        incoming = copy.deepcopy(response.get("items", []))
        if older:
            known = {item["id"] for item in self.items}
            self.items = [item for item in incoming if item["id"] not in known] + self.items
        elif response.get("incremental") and not response.get("history_reset"):
            replaced = set(response.get("replace_run_ids", []))
            ids = {item["id"] for item in incoming}
            positions = [
                index
                for index, item in enumerate(self.items)
                if item.get("run_id") in replaced or item["id"] in ids
            ]
            position = positions[0] if positions else len(self.items)
            self.items = [
                item
                for item in self.items
                if item.get("run_id") not in replaced and item["id"] not in ids
            ]
            self.items[position:position] = incoming
            self.facts.update(response)
            if "active_run" not in response:
                self.facts.pop("active_run", None)
            self.sequence = response.get("timeline_sequence", self.sequence)
        else:
            self.items = incoming
            self.facts = response
            self.sequence = response.get("timeline_sequence", -1)
        if older or not response.get("incremental"):
            self.next_before = response.get("next_before") if response.get("has_more") else None

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
            replacement = copy.deepcopy(payload.get("items", []))
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
        self.items = [item for item in self.items if item["id"] not in removed]
        for entry in payload.get("upsert", []):
            existing = next(
                (index for index, item in enumerate(self.items) if item["id"] == entry["id"]), None
            )
            if existing is None:
                self.items.append(copy.deepcopy(entry))
            else:
                self.items[existing] = copy.deepcopy(entry)
        run = next(
            (
                item
                for item in self.items
                if item.get("type") == "assistant_run"
                and item.get("runId", item.get("run_id")) == run_id
            ),
            None,
        )
        if run is not None:
            run.update(copy.deepcopy(payload.get("run") or {}))
            children = {item["id"]: item for item in run.get("items", [])}
            for item_id in payload.get("remove_children", []):
                children.pop(item_id, None)
            for item in payload.get("children", []):
                children[item["id"]] = copy.deepcopy(item)
            for change in payload.get("child_updates", []):
                child = children.get(change["id"])
                if child is not None:
                    for key, suffix in change.get("append", {}).items():
                        child[key] = str(child.get(key, "")) + suffix
                    child.update(copy.deepcopy(change.get("set", {})))
            order = payload.get("order", list(children))
            run["items"] = [children[item_id] for item_id in order if item_id in children]
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
