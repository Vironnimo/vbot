"""Per-call refs, app catalogs, and target resolution for Live Tools.

Every Session and Terminal a Live result mentions gets a short ref (``s1``,
``t1``), assigned in order of first appearance and stable for the whole call, so
a voice Model can name it without reading ids aloud. Refs are a Live-scoped
alias table held by the call's executor; storage and RPCs keep exact ids.

A target names a Session, Terminal, Terminal group, Agent, or Project. A ref
(tolerant spellings such as ``S2``, ``s-2``, ``#s2``) decides. Otherwise each
kind the Tool accepts contributes its matches: its exact ids (a Session or
Terminal id, a group id, an Agent address, a Project id) if any match, else its
id prefixes (>= 4 characters, Sessions and Terminals) and names together. The
target resolves only when exactly one thing matches across all accepted kinds;
a Terminal and an Agent of the same name are ambiguous, never decided by kind
order. Names compare by :func:`core.tools._call_vocabulary.spelling`.
"""

from __future__ import annotations

import re
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any

from core.model_tasks.live import CODING_PROGRAMS, CodingProgram
from core.projects import format_agent_address
from core.tools._call_vocabulary import spelling
from server._live_context import (
    UI_ACTION_CONTEXT,
    JsonObject,
    LiveContext,
    LiveToolError,
    LiveUiError,
)

_EXECUTABLE_SUFFIX = re.compile(r"\.(exe|cmd|bat|ps1)$", re.IGNORECASE)
_REF = re.compile(r"^\s*#?\s*([st])\s*[-_.: ]?\s*(\d{1,6})\s*$", re.IGNORECASE)
_MIN_PREFIX_CHARS = 4
_SESSION_LIST_LIMIT = 100
_MAX_AGENT_BATCH = 100
_MAX_LISTED_CANDIDATES = 8
_MAX_LEGEND_REFS = 30
_MAX_LEGEND_NAME_CHARS = 60

SESSION = "session"
TERMINAL = "terminal"
GROUP = "group"
AGENT = "agent"
PROJECT = "project"

_KIND_WORDS = {
    SESSION: "Session",
    TERMINAL: "Terminal",
    GROUP: "Terminal group",
    AGENT: "Agent",
    PROJECT: "Project",
}


@dataclass(frozen=True)
class SessionKey:
    """One Chat Session by exact address: the Agent address and the Session id."""

    address: str
    session_id: str


@dataclass(frozen=True)
class LiveAgent:
    """An Agent Live can start Sessions at; ``project`` names a team Agent's Project."""

    address: str
    name: str
    project: str = ""

    @property
    def label(self) -> str:
        return f"{self.name} ({self.project} team)" if self.project else self.name


@dataclass(frozen=True)
class LiveProject:
    project_id: str
    name: str
    folder: str


@dataclass(frozen=True)
class Target:
    """A resolved target; exactly the field matching ``kind`` is set."""

    kind: str
    session: SessionKey | None = None
    terminal: JsonObject | None = None
    group: JsonObject | None = None
    agent: LiveAgent | None = None
    project: LiveProject | None = None


def parse_ref(text: str) -> str | None:
    """Return the canonical ref (``s2``) a tolerant spelling names, else ``None``."""
    match = _REF.match(text)
    if match is None:
        return None
    return f"{match.group(1).lower()}{int(match.group(2))}"


class LiveRefs:
    """The call's ref table plus the Sessions Live started or addressed.

    Each ref keeps the latest label a result gave it (``Session at Coder``,
    ``Codex Terminal``), so later delegations can see what the refs name
    without reading them again.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, SessionKey] = {}
        self._session_refs: dict[SessionKey, str] = {}
        self._terminals: dict[str, str] = {}
        self._terminal_refs: dict[str, str] = {}
        self._touched: OrderedDict[SessionKey, None] = OrderedDict()
        self._labels: OrderedDict[str, str] = OrderedDict()

    def session(self, key: SessionKey, label: str = "") -> str:
        """Return the Session's ref, assigning the next one on first appearance."""
        ref = self._session_refs.get(key)
        if ref is None:
            ref = f"s{len(self._sessions) + 1}"
            self._sessions[ref] = key
            self._session_refs[key] = ref
        self._label(ref, label, fallback=f"Session at {key.address}")
        return ref

    def terminal(self, terminal_id: str, label: str = "") -> str:
        """Return the Terminal's ref, assigning the next one on first appearance."""
        ref = self._terminal_refs.get(terminal_id)
        if ref is None:
            ref = f"t{len(self._terminals) + 1}"
            self._terminals[ref] = terminal_id
            self._terminal_refs[terminal_id] = ref
        self._label(ref, label, fallback="Terminal")
        return ref

    def touch(self, key: SessionKey, label: str = "") -> str:
        """Remember a Session Live started or addressed; returns its ref."""
        self._touched[key] = None
        self._touched.move_to_end(key)
        return self.session(key, label)

    def legend(self) -> str:
        """The refs named so far, most recently named last, one line each."""
        shown = list(self._labels.items())[-_MAX_LEGEND_REFS:]
        return "\n".join(f"- {ref}: {label}" for ref, label in shown)

    def _label(self, ref: str, label: str, *, fallback: str) -> None:
        """Keep the latest label a result gave; the fallback only until one does.

        A shorter form of the kept label (``Session at Coder`` for
        ``Session at Coder "Fix"``) keeps the longer one.
        """
        if label and not self._labels.get(ref, "").startswith(label):
            self._labels[ref] = label
        elif ref not in self._labels:
            self._labels[ref] = fallback
        self._labels.move_to_end(ref)

    def touched(self) -> list[SessionKey]:
        """Sessions Live started or addressed in this call, oldest first."""
        return list(self._touched)

    def is_touched(self, key: SessionKey) -> bool:
        return key in self._touched

    def known_sessions(self) -> list[SessionKey]:
        return list(self._sessions.values())

    def lookup(self, ref: str) -> SessionKey | str | None:
        """Return the Session key or Terminal id a canonical ref names."""
        if ref.startswith("s"):
            return self._sessions.get(ref)
        return self._terminals.get(ref)


class LiveCatalog:
    """App data one Tool execution reads, each loaded at most once.

    The app window's context supplies the selection, the identity Agents, the
    selected Project's team, and the Projects; without a window the canonical
    RPCs supply the Agents and Projects, without a team.
    """

    def __init__(self, ctx: LiveContext, refs: LiveRefs) -> None:
        self._ctx = ctx
        self._refs = refs
        self._selection: JsonObject | None = None
        self._selection_loaded = False
        self._agents: list[LiveAgent] | None = None
        self._projects: list[LiveProject] | None = None
        self._teams: dict[str, list[LiveAgent]] = {}
        self._sessions: list[JsonObject] | None = None
        self._own_sessions: dict[str, frozenset[SessionKey]] = {}
        self._terminals: tuple[list[JsonObject], list[JsonObject]] | None = None

    async def selection(self) -> JsonObject | None:
        """The app window's context, or ``None`` when no window answers."""
        if not self._selection_loaded:
            self._selection_loaded = True
            try:
                context = await self._ctx.ui(UI_ACTION_CONTEXT, {})
            except LiveUiError:
                context = None
            self._selection = context if isinstance(context, dict) else None
        return self._selection

    async def selected_project(self) -> LiveProject | None:
        selection = await self.selection()
        project_id = (selection or {}).get("selected_project_id")
        if not isinstance(project_id, str) or not project_id:
            return None
        return next((item for item in await self.projects() if item.project_id == project_id), None)

    async def agents(self) -> list[LiveAgent]:
        """Identity Agents, then the selected Project's team."""
        if self._agents is None:
            selection = await self.selection()
            if selection is not None and isinstance(selection.get("agents"), list):
                identity = [
                    LiveAgent(address=str(item["agent_id"]), name=_name(item, "agent_id"))
                    for item in _objects(selection["agents"])
                    if _text(item.get("agent_id"))
                ]
                project = await self.selected_project()
                team = [
                    LiveAgent(
                        address=str(item["agent_id"]),
                        name=_name(item, "agent_id"),
                        project=project.name if project else "",
                    )
                    for item in _objects(selection.get("selected_project_team"))
                    if _text(item.get("agent_id"))
                ]
                self._agents = identity + team
            else:
                listed = await self._ctx.call("agent.list", {})
                self._agents = [
                    LiveAgent(address=str(item["id"]), name=_name(item, "id"))
                    for item in _objects(listed.get("agents"))
                    if _text(item.get("id"))
                ]
        return self._agents

    async def projects(self) -> list[LiveProject]:
        if self._projects is None:
            selection = await self.selection()
            if selection is not None and isinstance(selection.get("projects"), list):
                items = _objects(selection["projects"])
            else:
                items = _objects((await self._ctx.call("project.list", {})).get("projects"))
            self._projects = [
                LiveProject(
                    project_id=str(item["project_id"]),
                    name=str(item.get("name") or item.get("display_name") or item["project_id"]),
                    folder=str(item.get("cwd") or ""),
                )
                for item in items
                if _text(item.get("project_id"))
            ]
        return self._projects

    async def team(self, project: LiveProject) -> list[LiveAgent]:
        """The Project's team Agents, addressed as ``agent_id@project_id``."""
        if project.project_id not in self._teams:
            shown = await self._ctx.call("project.show", {"project_id": project.project_id})
            scan = shown.get("scan")
            scan = scan if isinstance(scan, dict) else {}
            self._teams[project.project_id] = [
                LiveAgent(
                    address=format_agent_address(str(item["agent_id"]), project.project_id),
                    name=str(item.get("display_name") or item["agent_id"]),
                    project=project.name,
                )
                for item in _objects(scan.get("team"))
                if _text(item.get("agent_id"))
            ]
        return self._teams[project.project_id]

    async def agent_name(self, address: str) -> str:
        """The display name of an Agent address; the address when unknown."""
        for agent in await self.agents():
            if agent.address == address:
                return agent.name
        for team in self._teams.values():
            for agent in team:
                if agent.address == address:
                    return agent.name
        return address

    async def sessions(self) -> list[JsonObject]:
        """Top-level Sessions of the known Agents, most recently active first."""
        if self._sessions is None:
            addresses = list(
                dict.fromkeys(
                    [agent.address for agent in await self.agents()]
                    + [key.address for key in self._refs.touched()]
                )
            )[:_MAX_AGENT_BATCH]
            if not addresses:
                self._sessions = []
            else:
                listed = await self._ctx.call(
                    "session.list",
                    {
                        "agent_ids": addresses,
                        "limit": _SESSION_LIST_LIMIT,
                        "include_subagents": False,
                        "include_memory_reflections": False,
                        "include_skill_reflections": False,
                    },
                )
                self._sessions = [
                    item
                    for item in _objects(listed.get("sessions"))
                    if _text(item.get("id")) and _text(item.get("agent_address"))
                ]
        return self._sessions

    async def own_sessions(self, address: str) -> frozenset[SessionKey]:
        """The Agent's Sessions that no Cron job or Channel started, by the Session list."""
        if address not in self._own_sessions:
            listed = await self._ctx.call(
                "session.list",
                {
                    "agent_id": address,
                    "limit": _SESSION_LIST_LIMIT,
                    "include_subagents": False,
                    "include_memory_reflections": False,
                    "include_skill_reflections": False,
                    "include_cron": False,
                    "include_channels": False,
                },
            )
            self._own_sessions[address] = frozenset(
                session_key(item)
                for item in _objects(listed.get("sessions"))
                if _text(item.get("id")) and _text(item.get("agent_address"))
            )
        return self._own_sessions[address]

    async def terminals(self) -> list[JsonObject]:
        return (await self._terminal_catalog())[0]

    async def groups(self) -> list[JsonObject]:
        return (await self._terminal_catalog())[1]

    def forget_terminals(self) -> None:
        """Reload the Terminal catalog on next use, after a Terminal change."""
        self._terminals = None

    async def _terminal_catalog(self) -> tuple[list[JsonObject], list[JsonObject]]:
        if self._terminals is None:
            listed = await self._ctx.call("terminal.list", {})
            self._terminals = (
                [
                    item
                    for item in _objects(listed.get("terminals"))
                    if _text(item.get("terminal_id"))
                ],
                [item for item in _objects(listed.get("groups")) if _text(item.get("group_id"))],
            )
        return self._terminals


def session_key(item: JsonObject) -> SessionKey:
    return SessionKey(address=str(item["agent_address"]), session_id=str(item["id"]))


async def resolve_target(
    text: str,
    kinds: Iterable[str],
    *,
    tool: str,
    field: str,
    refs: LiveRefs,
    catalog: LiveCatalog,
    kind_hint: str = "",
) -> Target:
    """Resolve *text* to one target of the allowed *kinds* or raise a guiding failure.

    *kind_hint* tells the Model how to pick one kind when things of several
    kinds match; it follows the ambiguity failure.
    """
    allowed = frozenset(kinds)
    wanted = _kinds_phrase(allowed)
    ref = parse_ref(text)
    if ref is not None:
        return await _ref_target(ref, allowed, tool=tool, field=field, refs=refs, catalog=catalog)
    matches = await _matches(text, allowed, refs, catalog)
    if len(matches) == 1:
        return matches[0]
    if matches:
        listed = ", ".join(
            [await describe(item, refs, catalog) for item in matches[:_MAX_LISTED_CANDIDATES]]
        )
        several_kinds = len({item.kind for item in matches}) > 1
        raise LiveToolError(
            "ambiguous_target",
            f'"{text}" matches several: {listed}. Ask the user which one they mean, then call '
            f"{tool} again with its ref or id as {field}."
            + (f" {kind_hint}" if several_kinds and kind_hint else ""),
        )
    hint = ""
    if AGENT in allowed:
        names = ", ".join(agent.label for agent in (await catalog.agents())[:12])
        hint = f" Agents: {names}." if names else ""
    raise LiveToolError(
        "target_not_found",
        f'No {wanted} matches "{text}".{hint} Call overview to see the current refs and names, '
        f"then call {tool} again with one of them as {field}.",
    )


async def describe(target: Target, refs: LiveRefs, catalog: LiveCatalog) -> str:
    """A short label naming the target with its ref where it has one."""
    if target.session is not None:
        name = await catalog.agent_name(target.session.address)
        return f"{refs.session(target.session, session_title(name))} ({name})"
    if target.terminal is not None:
        ref = refs.terminal(str(target.terminal["terminal_id"]), terminal_title(target.terminal))
        return f"{ref} ({terminal_label(target.terminal)})"
    if target.group is not None:
        name = target.group.get("name") or target.group["group_id"]
        return f'group "{name}" (id {target.group["group_id"]})'
    if target.agent is not None:
        team = f"{target.agent.project} team, " if target.agent.project else ""
        return f"Agent {target.agent.name} ({team}id {target.agent.address})"
    if target.project is not None:
        return f"Project {target.project.name} (id {target.project.project_id})"
    return "?"


def terminal_label(item: JsonObject) -> str:
    """The Terminal's name, or the program it runs."""
    name = item.get("name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    return str(item.get("launch_command") or item.get("command") or "Terminal")


def coding_program(item: JsonObject) -> CodingProgram | None:
    """The coding program a Terminal was started with, if it is one Live knows."""
    command = str(item.get("launch_command") or item.get("command") or "").strip()
    base = _EXECUTABLE_SUFFIX.sub("", re.split(r"[\\/]", command)[-1]).lower()
    return next((program for program in CODING_PROGRAMS.values() if program.command == base), None)


def terminal_title(item: JsonObject) -> str:
    """What a Terminal is, as the call's ref legend names it: ``Codex Terminal "Build"``."""
    program = coding_program(item)
    command = str(item.get("launch_command") or item.get("command") or "").strip()
    kind = f"{program.label if program else command} Terminal".strip()
    name = _legend_name(item.get("name"))
    return f'{kind} "{name}"' if name else kind


def session_title(agent_name: str, title: str = "") -> str:
    """What a Session is, as the call's ref legend names it: ``Session at Coder "Fix"``."""
    name = _legend_name(title)
    return f'Session at {agent_name} "{name}"' if name else f"Session at {agent_name}"


def _legend_name(value: object) -> str:
    """A title or name on one short line, so each legend entry stays one line."""
    if not isinstance(value, str):
        return ""
    text = " ".join(value.split())
    return (
        text if len(text) <= _MAX_LEGEND_NAME_CHARS else text[: _MAX_LEGEND_NAME_CHARS - 3] + "..."
    )


async def _ref_target(
    ref: str,
    allowed: frozenset[str],
    *,
    tool: str,
    field: str,
    refs: LiveRefs,
    catalog: LiveCatalog,
) -> Target:
    found = refs.lookup(ref)
    if found is None:
        raise LiveToolError(
            "unknown_ref",
            f"There is no {ref} in this call. Call overview to see the current refs, then call "
            f"{tool} again with one of them as {field}.",
        )
    if isinstance(found, SessionKey):
        if SESSION in allowed:
            return Target(kind=SESSION, session=found)
        raise LiveToolError(
            "wrong_target",
            f"{ref} is a Session; {tool} needs a {_kinds_phrase(allowed)} as {field}. Call "
            f"{tool} again with a matching ref, or use send_message, read, or stop for {ref}.",
        )
    terminal = next(
        (item for item in await catalog.terminals() if item["terminal_id"] == found), None
    )
    if terminal is None:
        raise LiveToolError(
            "target_gone",
            f"{ref} no longer exists; it was closed. Call overview to see the current Terminals.",
        )
    if TERMINAL in allowed:
        return Target(kind=TERMINAL, terminal=terminal)
    raise LiveToolError(
        "wrong_target",
        f"{ref} is a Terminal; {tool} needs a {_kinds_phrase(allowed)} as {field}. Call {tool} "
        "again with a matching ref, or use terminal for Terminal actions.",
    )


Matcher = Callable[[str, LiveRefs, LiveCatalog], Awaitable[list["Target"]]]


async def _matches(
    text: str, allowed: frozenset[str], refs: LiveRefs, catalog: LiveCatalog
) -> list[Target]:
    """Every thing of the allowed kinds *text* names, per kind exact ids first."""
    found: list[Target] = []
    for kind in _KIND_WORDS:
        if kind not in allowed:
            continue
        exact, loose = _KIND_MATCHERS[kind]
        matches = await exact(text, refs, catalog)
        if not matches:
            matches = await loose(text, refs, catalog)
        found += matches
    return found


async def _exact_sessions(text: str, refs: LiveRefs, catalog: LiveCatalog) -> list[Target]:
    value = text.strip()
    return [
        Target(kind=SESSION, session=key)
        for key in await _session_keys(refs, catalog)
        if value in {key.session_id, f"{key.address}/{key.session_id}"}
    ]


async def _session_prefixes(text: str, refs: LiveRefs, catalog: LiveCatalog) -> list[Target]:
    prefix = _prefix(text)
    if prefix is None:
        return []
    return [
        Target(kind=SESSION, session=key)
        for key in await _session_keys(refs, catalog)
        if _id_prefix(key.session_id, prefix)
    ]


async def _exact_terminals(text: str, refs: LiveRefs, catalog: LiveCatalog) -> list[Target]:
    value = text.strip()
    return [
        Target(kind=TERMINAL, terminal=item)
        for item in await catalog.terminals()
        if item["terminal_id"] == value
    ]


async def _terminal_names_and_prefixes(
    text: str, refs: LiveRefs, catalog: LiveCatalog
) -> list[Target]:
    key = spelling(text)
    prefix = _prefix(text)
    return [
        Target(kind=TERMINAL, terminal=item)
        for item in await catalog.terminals()
        if (key and spelling(str(item.get("name") or "")) == key)
        or (prefix is not None and _id_prefix(str(item["terminal_id"]), prefix))
    ]


async def _exact_groups(text: str, refs: LiveRefs, catalog: LiveCatalog) -> list[Target]:
    value = text.strip()
    return [
        Target(kind=GROUP, group=item)
        for item in await catalog.groups()
        if item["group_id"] == value
    ]


async def _group_names(text: str, refs: LiveRefs, catalog: LiveCatalog) -> list[Target]:
    key = spelling(text)
    return [
        Target(kind=GROUP, group=item)
        for item in await catalog.groups()
        if key and spelling(str(item.get("name") or "")) == key
    ]


async def _exact_agents(text: str, refs: LiveRefs, catalog: LiveCatalog) -> list[Target]:
    value = text.strip()
    return [
        Target(kind=AGENT, agent=agent)
        for agent in await catalog.agents()
        if agent.address == value
    ]


async def _agent_names(text: str, refs: LiveRefs, catalog: LiveCatalog) -> list[Target]:
    key = spelling(text)
    return [
        Target(kind=AGENT, agent=agent)
        for agent in await catalog.agents()
        if key and key in agent_spellings(agent)
    ]


def agent_spellings(agent: LiveAgent) -> set[str]:
    """The Agent's name, address, and bare id (a team Agent's id without its Project)."""
    return {spelling(agent.name), spelling(agent.address), spelling(agent.address.split("@")[0])}


async def _exact_projects(text: str, refs: LiveRefs, catalog: LiveCatalog) -> list[Target]:
    value = text.strip()
    return [
        Target(kind=PROJECT, project=project)
        for project in await catalog.projects()
        if project.project_id == value
    ]


async def _project_names(text: str, refs: LiveRefs, catalog: LiveCatalog) -> list[Target]:
    key = spelling(text)
    return [
        Target(kind=PROJECT, project=project)
        for project in await catalog.projects()
        if key and key in {spelling(project.name), spelling(project.project_id)}
    ]


# Per kind: the exact-id matcher, then the matcher of names and id prefixes.
_KIND_MATCHERS: dict[str, tuple[Matcher, Matcher]] = {
    SESSION: (_exact_sessions, _session_prefixes),
    TERMINAL: (_exact_terminals, _terminal_names_and_prefixes),
    GROUP: (_exact_groups, _group_names),
    AGENT: (_exact_agents, _agent_names),
    PROJECT: (_exact_projects, _project_names),
}


async def _session_keys(refs: LiveRefs, catalog: LiveCatalog) -> list[SessionKey]:
    keys = dict.fromkeys(refs.known_sessions())
    for item in await catalog.sessions():
        keys[session_key(item)] = None
    return list(keys)


def _prefix(text: str) -> str | None:
    """The text as an id prefix, or ``None`` when it is too short to be one."""
    value = text.strip().lower()
    return value if len(spelling(value)) >= _MIN_PREFIX_CHARS else None


def _id_prefix(identifier: str, prefix: str) -> bool:
    """Match a prefix of the whole id or of its random part after the type prefix."""
    return identifier.startswith(prefix) or identifier.partition("_")[2].startswith(prefix)


def _kinds_phrase(kinds: frozenset[str]) -> str:
    words = [_KIND_WORDS[kind] for kind in _KIND_WORDS if kind in kinds]
    if len(words) <= 1:
        return words[0] if words else "target"
    return ", ".join(words[:-1]) + " or " + words[-1]


def _objects(value: Any) -> list[JsonObject]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _name(item: JsonObject, id_field: str) -> str:
    name = item.get("name") or item.get("display_name")
    return str(name).strip() if isinstance(name, str) and name.strip() else str(item[id_field])
