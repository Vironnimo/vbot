"""Fork lineage against an in-memory reference model.

Random appends, forks, edits and deletes run against the store and against a
model that keeps every Session's history as plain copied lists. After every
operation each live Session must show the model's current view and own audit,
and the search indexes must cover exactly the entries some view shows.

The model also tracks which stored entry each view shows: a fork shares its
origin's entries, and deleting a Session gives every Session that still shows
its entries a copy of its own. Search reports each stored entry once.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from core.chat import ChatMessage
from core.sessions import ChatSessionManager, SessionAddress, _store_fts

_STEPS = 45
_MAX_SESSIONS = 7


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
        message = (
            ChatMessage.user(text)
            if self.rng.random() < 0.6
            else ChatMessage.assistant(model="model", content=text)
        )
        self.manager.get(address).append(message)
        model = self.models[address]
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
        coverage = self.manager._store._execute_write(_store_fts._fts_coverage_ok)
        assert coverage == (True, None), trail
        assert self.manager.fts_health().state == "healthy", trail

    def verify_search(self, trail: list[str]) -> None:
        """Each shown entry is found once, for a Session that shows it."""
        shown_by: dict[str, dict[int, set[SessionAddress]]] = {}
        messages: dict[str, ChatMessage] = {}
        for address, model in self.models.items():
            for shown in model.current:
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
