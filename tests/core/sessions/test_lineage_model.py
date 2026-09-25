"""Fork lineage against an in-memory reference model.

Random appends, forks, edits and deletes run against the store and against a
model that keeps every Session's history as plain copied lists. After every
operation each live Session must show the model's current view and own audit,
and the search indexes must cover exactly the entries some view shows.

The model also tracks which stored entry each view shows: a fork shares its
origin's entries, and deleting a Session gives every Session that still shows
its entries a copy of its own. Search reports each stored entry once.

The narrow reads must agree with the model too: the status facts, the User
count and the newest prefixed Note read the current view, while the Session's
spend counts only the Assistant usage it wrote itself, superseded turns
included and a fork's inherited prefix excluded.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from core.chat import ChatMessage
from core.sessions import ChatSession, ChatSessionManager, SessionAddress, _store_fts

_STEPS = 45
_MAX_SESSIONS = 7
_NOTE_PREFIX = "[mark] "


@dataclass
class _Shown:
    """One Message a view shows, and the stored entry it is read from."""

    message: ChatMessage
    entry: int


@dataclass
class _Model:
    """What one Session must show: its current view and its own audit."""

    current: list[_Shown] = field(default_factory=list)
    audit: list[tuple[str, str | None]] = field(default_factory=list)
    # The Assistant usage this Session wrote: (turns, input tokens, output tokens).
    spend: tuple[int, int, int] = (0, 0, 0)


class _Scenario:
    def __init__(self, manager: ChatSessionManager, rng: random.Random) -> None:
        self.manager = manager
        self.rng = rng
        self.models: dict[SessionAddress, _Model] = {}
        self.holders: dict[int, SessionAddress] = {}
        self.counter = 0

    def store(self, address: SessionAddress, message: ChatMessage) -> _Shown:
        self.counter += 1
        self.holders[self.counter] = address
        return _Shown(message, self.counter)

    def token(self) -> str:
        self.counter += 1
        return f"tok{self.counter:04d}"

    def pick(self) -> SessionAddress:
        return self.rng.choice(sorted(self.models, key=lambda address: address.session_id))

    def create(self) -> str:
        session = self.manager.create("agent")
        self.models[session.address] = _Model()
        return f"create {session.id}"

    def append(self) -> str:
        address = self.pick()
        text = f"{self.token()} words"
        roll = self.rng.random()
        model = self.models[address]
        if roll < 0.5:
            message = ChatMessage.user(text)
        elif roll < 0.85:
            usage = {
                "input_tokens": self.rng.randint(1, 99),
                "output_tokens": self.rng.randint(1, 9),
            }
            message = ChatMessage.assistant(model="model", content=text, usage=usage)
            turns, input_tokens, output_tokens = model.spend
            model.spend = (
                turns + 1,
                input_tokens + usage["input_tokens"],
                output_tokens + usage["output_tokens"],
            )
        else:
            message = ChatMessage.note(_NOTE_PREFIX + text)
        self.manager.get(address).append(message)
        model.current.append(self.store(address, message))
        model.audit.append((message.role, message.id))
        return f"append {message.role} to {address.session_id}"

    def fork(self) -> str:
        address = self.pick()
        fork = asyncio.run(self.manager.fork(address))
        self.models[fork.address] = _Model(current=list(self.models[address].current))
        return f"fork {address.session_id} as {fork.id}"

    def edit(self) -> str | None:
        candidates = [
            (address, index)
            for address, model in self.models.items()
            for index, shown in enumerate(model.current)
            if shown.message.role == "user"
        ]
        if not candidates:
            return None
        address, index = self.rng.choice(
            sorted(candidates, key=lambda item: (item[0].session_id, item[1]))
        )
        model = self.models[address]
        target = model.current[index].message
        replacement = ChatMessage.user(f"{self.token()} replacement")
        self.manager.get(address).apply_edit(target.id, [replacement])
        model.current[index:] = [self.store(address, replacement)]
        model.audit.extend([("history_edit", None), ("user", replacement.id)])
        return f"edit {address.session_id} at {index}"

    def delete(self) -> str | None:
        if len(self.models) < 2:
            return None
        address = self.pick()
        self.manager.delete(address)
        del self.models[address]
        for other, model in self.models.items():
            model.current = [
                self.store(other, shown.message) if self.holders[shown.entry] == address else shown
                for shown in model.current
            ]
        return f"delete {address.session_id}"

    def step(self) -> str | None:
        if not self.models:
            return self.create()
        operations = [self.append] * 5 + [self.fork] * 2 + [self.edit] * 2 + [self.delete]
        if len(self.models) < _MAX_SESSIONS:
            operations.append(self.create)
        else:
            operations.remove(self.fork)
            operations.remove(self.fork)
        return self.rng.choice(operations)()

    def verify(self, trail: list[str]) -> None:
        for address, model in self.models.items():
            session = self.manager.get(address)
            current = [(message.id, message.content) for message in session.load_active()]
            expected = [(shown.message.id, shown.message.content) for shown in model.current]
            assert current == expected, trail
            audit = [
                (message.role, None if message.role == "history_edit" else message.id)
                for message in session.load()
            ]
            assert audit == model.audit, trail
            self.verify_reads(session, model, trail)
        coverage = self.manager._store._execute_write(_store_fts._fts_coverage_ok)
        assert coverage == (True, None), trail
        assert self.manager.fts_health().state == "healthy", trail

    def verify_reads(self, session: ChatSession, model: _Model, trail: list[str]) -> None:
        """The narrow reads agree with the current view and the own spend."""
        current = [shown.message for shown in model.current]
        users = [message for message in current if message.role == "user"]
        usages = [message.usage for message in current if message.role == "assistant"]
        notes = [message for message in current if message.role == "note"]
        status = session.status_snapshot()
        assert status.first_message_at == (current[0].timestamp if current else None), trail
        assert status.user_message_count == len(users), trail
        assert status.latest_assistant_usage == (usages[-1] if usages else None), trail
        turns, input_tokens, output_tokens = model.spend
        assert (
            status.session_usage["measured_turns"],
            status.session_usage["input_tokens"],
            status.session_usage["output_tokens"],
        ) == (turns, input_tokens, output_tokens), trail
        for limit in (1, 2, _STEPS):
            assert session.active_user_message_count(limit=limit) == min(len(users), limit), trail
        latest = session.latest_note(_NOTE_PREFIX)
        assert (latest.id if latest else None) == (notes[-1].id if notes else None), trail

    def verify_search(self, trail: list[str]) -> None:
        """Each shown entry is found once, for a Session that shows it."""
        shown_by: dict[str, dict[int, set[SessionAddress]]] = {}
        messages: dict[str, ChatMessage] = {}
        for address, model in self.models.items():
            for shown in model.current:
                if shown.message.role == "note":
                    continue
                messages[shown.message.id] = shown.message
                shown_by.setdefault(shown.message.id, {}).setdefault(shown.entry, set()).add(
                    address
                )
        for message_id, entries in shown_by.items():
            token = str(messages[message_id].content).split()[0]
            showing = set().union(*entries.values())
            # A Session that stores an entry and still shows it is reported for it.
            owners = {
                self.holders[entry]
                for entry, addresses in entries.items()
                if self.holders[entry] in addresses
            }
            for use_fts in (True, False):
                hits = self.manager.search_messages(
                    token, project_id=None, agent_id="agent", use_fts=use_fts
                ).hits
                assert [hit.message_id for hit in hits] == [message_id] * len(entries), trail
                reported = [hit.address for hit in hits]
                assert len(set(reported)) == len(reported), trail
                assert owners <= set(reported) <= showing, trail
            for address in showing:
                scoped = self.manager.search_messages(
                    token, project_id=None, agent_id="agent", session_id=address.session_id
                ).hits
                assert [(hit.address, hit.message_id) for hit in scoped] == [
                    (address, message_id)
                ], trail


@pytest.mark.parametrize("seed", range(12))
def test_lineage_views_match_a_reference_model(tmp_path: Path, seed: int) -> None:
    rng = random.Random(seed)
    manager = ChatSessionManager(tmp_path)
    scenario = _Scenario(manager, rng)
    trail: list[str] = []
    try:
        while len(trail) < _STEPS:
            action = scenario.step()
            if action is None:
                continue
            trail.append(action)
            scenario.verify(trail)
        scenario.verify_search(trail)
    finally:
        manager.close()
