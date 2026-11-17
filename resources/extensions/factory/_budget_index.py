"""Incremental native allocations over revisioned domain and canonical Usage feeds.

The service lock owns this disposable projection. A cold start/restore rebuilds;
ordinary admissions touch changed entries and their ancestor allocations only.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any

from . import _workflows

type Json = dict[str, Any]
type Key = tuple[str, str]
_USAGE_FIELDS = (
    "calls",
    "input_tokens",
    "output_tokens",
    "unreported_calls",
    "unpriced_calls",
    "cost_nano_usd",
)


@dataclass
class _Total:
    runs: Counter[str] = field(default_factory=Counter)
    tools: int = 0
    groups: Counter[str] = field(default_factory=Counter)
    model: Counter[str] = field(default_factory=Counter)

    def usage(self, resources: bool) -> Json:
        model = self.model if resources else Counter()
        return {
            "runs": len(self.runs),
            "tool_calls": self.tools,
            "tokens": model["input_tokens"] + model["output_tokens"],
            "cost_usd": None
            if model["calls"] and model["calls"] == model["unpriced_calls"]
            else model["cost_nano_usd"] / 1e9,
            "unpriced_calls": model["unpriced_calls"],
            "unreported_calls": model["unreported_calls"],
        }


@dataclass(frozen=True)
class _Entry:
    scopes: tuple[str, ...]
    runs: frozenset[str]
    tools: int
    groups: frozenset[str]


class FactoryBudgetIndex:
    def __init__(self, service: Any) -> None:
        self.service = service
        self._domain_id: str | None = None
        self._domain_revision = 0
        self._usage_id: str | None = None
        self._usage_revision = 0
        self.records: dict[str, dict[str, Json]] = {
            kind: {} for kind in ("task", "topic", "execution", "card")
        }
        self._entries: dict[Key, _Entry] = {}
        self._totals: dict[str, _Total] = defaultdict(_Total)
        self._group_scopes: dict[str, set[str]] = defaultdict(set)
        self._reports: dict[str, Counter[str]] = {}
        self._topic_executions: dict[str, set[str]] = defaultdict(set)
        self._card_children: dict[str, set[str]] = defaultdict(set)

    async def sync(self, *, resources: bool) -> None:
        changes: dict[Key, Json | None] = {}
        ledger_id, revision = self._domain_id, self._domain_revision
        while True:
            page = await self.service.records().budget_changes(
                revision,
                ledger_id=ledger_id,
            )
            if page["ledger_id"] != ledger_id:
                changes.clear()
                ledger_id = page["ledger_id"]
            for change in page["records"]:
                changes[(change["kind"], change["id"])] = change["value"]
            revision = page["revision"]
            if not page["more"]:
                break
        if ledger_id != self._domain_id:
            self.records = {kind: {} for kind in self.records}
            self._entries.clear()
            self._totals.clear()
            self._group_scopes.clear()
            self._topic_executions.clear()
            self._card_children.clear()
        try:
            self._apply(changes)
        except BaseException:
            self._domain_id, self._domain_revision = None, 0
            raise
        self._domain_id, self._domain_revision = ledger_id, revision
        if resources:
            await self._sync_usage()

    def _apply(self, changes: dict[Key, Json | None]) -> None:
        dirty: set[Key] = set()
        rebuild = False
        changed_cards: set[str] = set()
        for (kind, identifier), value in changes.items():
            previous = self.records[kind].get(identifier)
            if previous == value:
                continue
            if kind == "task":
                rebuild |= previous is not None and (
                    value is None
                    or previous["parent_id"] != value["parent_id"]
                    or previous["factory_id"] != value["factory_id"]
                )
            elif kind == "topic":
                dirty.update(("execution", key) for key in self._topic_executions[identifier])
            elif kind == "execution":
                for record, remove in ((previous, True), (value, False)):
                    if record and record["execution_kind"] == "topic":
                        ids = self._topic_executions[record["scope_id"]]
                        ids.discard(identifier) if remove else ids.add(identifier)
                dirty.add((kind, identifier))
            else:
                for record, remove in ((previous, True), (value, False)):
                    if record and record.get("parent_id"):
                        ids = self._card_children[record["parent_id"]]
                        ids.discard(identifier) if remove else ids.add(identifier)
                changed_cards.add(identifier)
            if value is None:
                self.records[kind].pop(identifier, None)
            else:
                self.records[kind][identifier] = value
        # A rare hierarchy edit transfers retained spending as well as future
        # work. No durable counter is reset or tied to an assignment epoch.
        if rebuild:
            dirty.update(self._entries)
        while changed_cards:
            identifier = changed_cards.pop()
            key = ("card", identifier)
            if key in dirty:
                continue
            dirty.add(key)
            changed_cards.update(self._card_children[identifier])
        for key in dirty:
            previous_entry = self._entries.pop(key, None)
            if previous_entry:
                self._adjust(previous_entry, -1)
            if value := self.records[key[0]].get(key[1]):
                entry = self._entry(key[0], value)
                if entry is not None:
                    self._entries[key] = entry
                    self._adjust(entry, 1)

    def card_task(self, card_id: str) -> str:
        seen: set[str] = set()
        while card_id and card_id not in seen:
            seen.add(card_id)
            card = self.records["card"].get(card_id)
            if not card:
                break
            if task_id := card.get("task_id"):
                return str(task_id)
            card_id = card.get("parent_id", "")
        return ""

    def _entry(self, kind: str, value: Json) -> _Entry | None:
        if kind == "execution":
            factory_id = value["factory_id"]
            task_id = (
                value["scope_id"]
                if value["execution_kind"] != "topic"
                else self.records["topic"].get(value["scope_id"], {}).get("task_id", "")
            )
            groups = {value["group_id"]} if value.get("group_id") else set()
        else:
            task_id = self.card_task(value["id"])
            task = self.records["task"].get(task_id)
            if task is None:
                return None
            factory_id = task["factory_id"]
            groups = {value["group_id"]} | {
                _workflows.group_id(value["id"], step) for step in value.get("steps", [])
            }
        scopes = [f"factory:{factory_id}"]
        seen: set[str] = set()
        while task_id and task_id not in seen:
            seen.add(task_id)
            task = self.records["task"].get(task_id)
            if task is None:
                break
            scopes.append(f"task:{task_id}")
            task_id = task.get("parent_id", "")
        return _Entry(
            tuple(scopes),
            frozenset(value.get("runs", [])),
            value.get("tool_calls", 0),
            frozenset(groups),
        )

    def _adjust(self, entry: _Entry, sign: int) -> None:
        for scope in entry.scopes:
            total = self._totals[scope]
            total.tools += sign * entry.tools
            for run_id in entry.runs:
                total.runs[run_id] += sign
                if total.runs[run_id] == 0:
                    del total.runs[run_id]
            for group in entry.groups:
                before = total.groups[group]
                total.groups[group] += sign
                if not before:
                    self._group_scopes[group].add(scope)
                    total.model.update(self._reports.get(group, {}))
                elif total.groups[group] == 0:
                    self._group_scopes[group].discard(scope)
                    total.model.subtract(self._reports.get(group, {}))
                    del total.groups[group]

    async def _sync_usage(self) -> None:
        while True:
            page = await self.service.host.execution_usage_changes(
                self._usage_revision,
                ledger_id=self._usage_id,
            )
            if page["ledger_id"] != self._usage_id:
                self._usage_id = page["ledger_id"]
                self._reports.clear()
                for total in self._totals.values():
                    total.model.clear()
            for item in page["groups"]:
                group, report = item["group_id"], item["usage"]
                current = Counter({key: report.get(key, 0) for key in _USAGE_FIELDS})
                previous = self._reports.get(group, Counter())
                for scope in self._group_scopes[group]:
                    self._totals[scope].model.subtract(previous)
                    self._totals[scope].model.update(current)
                self._reports[group] = current
            self._usage_revision = page["revision"]
            if not page["more"]:
                break

    def usage(self, scope: str, *, resources: bool) -> Json:
        return self._totals[scope].usage(resources)
