"""Built-in Agent availability and idempotent identity seeding."""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.agents._types import (
    BUILTIN_AGENT_IDS,
    BUILTIN_AGENT_NAMES,
    LIBRARIAN_BUILTIN,
    LIVE_BACKEND_BUILTIN,
    LIVE_VOICE_BUILTIN,
    Agent,
    AgentError,
    BuiltinAgent,
    BuiltinAgentProblem,
    LibrarianProblem,
)
from core.tools.availability import TOOL_ACCESS_MODE_SELECTED, ToolAccess
from core.tools.live import LIVE_TOOL_NAMES
from core.tools.web_fetch import WEB_FETCH_TOOL_NAME
from core.tools.web_search import WEB_SEARCH_TOOL_NAME
from core.utils.file_status import exists_strict
from core.utils.logging import get_logger

if TYPE_CHECKING:
    from core.agents.agents import AgentStore


_LOGGER = get_logger("agents")


class BuiltinAgents:
    def __init__(self, owner: AgentStore) -> None:
        self._owner = owner

    def builtin_agent(self, builtin: BuiltinAgent) -> Agent | None:
        """Return a built-in Agent with defaults applied, ``None`` while unavailable."""
        if self.builtin_problem(builtin) is not None:
            return None
        try:
            return self._owner.get(BUILTIN_AGENT_IDS[builtin])
        except AgentError, OSError:
            return None

    def builtin_problem(self, builtin: BuiltinAgent) -> BuiltinAgentProblem | None:
        """Why a built-in Agent is unavailable, ``None`` while it is available.

        ``missing``: no Agent has its id yet (startup creates it); ``agent_id_taken``:
        an Agent of the user or an unfinished rename holds the id;
        ``invalid_config``: its ``agent.json`` cannot be loaded. Never raises.
        """
        agent_id = BUILTIN_AGENT_IDS[builtin]
        with self._owner._write_lock:
            try:
                agent_path = self._owner._stored_agent_path(agent_id)
                if agent_path is None:
                    if agent_id in self._owner._renames._pending_rename_ids() or exists_strict(
                        self._owner._agent_dir(agent_id)
                    ):
                        return "agent_id_taken"
                    return "missing"
                agent = self._owner._read_agent_config(agent_path)
            except AgentError, OSError:
                return "invalid_config"
            return None if agent.builtin == builtin else "agent_id_taken"

    def librarian(self) -> Agent | None:
        """Return the built-in Librarian with defaults applied, ``None`` while unavailable."""
        return self.builtin_agent(LIBRARIAN_BUILTIN)

    def librarian_problem(self) -> LibrarianProblem | None:
        """Why the built-in Librarian is unavailable (:meth:`builtin_problem`)."""
        return self.builtin_problem(LIBRARIAN_BUILTIN)

    def ensure_builtin_agents(self) -> None:
        """Create each built-in Agent that is missing.

        A new built-in Agent gets a new Agent's defaults, so it runs the default
        Model until the user picks one; the Agents of a Live call start with
        their default Tools. An Agent of the user that holds a reserved id and an
        ``agent.json`` that cannot be loaded stay as they are; the warning names
        the problem.
        """
        for builtin, agent_id in BUILTIN_AGENT_IDS.items():
            name = BUILTIN_AGENT_NAMES[builtin]
            with self._owner._snapshot_barrier.compound_mutation(), self._owner._change():
                problem = self.builtin_problem(builtin)
                if problem == "missing":
                    try:
                        self._owner._mutations._create(
                            agent_id,
                            name,
                            builtin=builtin,
                            tool_access=_BUILTIN_DEFAULT_TOOL_ACCESS.get(builtin),
                        )
                    except (AgentError, OSError) as error:
                        _LOGGER.warning("Could not create the built-in %s Agent: %s", name, error)
                        continue
                    _LOGGER.info("Built-in %s Agent created (agent=%s)", name, agent_id)
                elif problem is not None:
                    _LOGGER.warning(
                        "The built-in %s Agent is unavailable (agent=%s problem=%s)",
                        name,
                        agent_id,
                        problem,
                    )

    def ensure_bootstrap(self) -> Agent | None:
        """Create one valid bootstrap Agent when the store has none.

        Invalid Agent directories are preserved for diagnosis. If one already
        occupies ``main``, the bootstrap Agent uses the first free ``main-N`` id.
        """
        with self._owner._snapshot_barrier.compound_mutation(), self._owner._change():
            if self._owner.list():
                return None

            candidate = _BOOTSTRAP_AGENT_ID
            suffix = 2
            reserved = self._owner._renames._pending_rename_ids()
            while exists_strict(self._owner._agent_dir(candidate)) or candidate in reserved:
                candidate = f"{_BOOTSTRAP_AGENT_ID}-{suffix}"
                suffix += 1
            return self._owner.create(candidate, _BOOTSTRAP_AGENT_NAME)


_BOOTSTRAP_AGENT_ID = "main"


_BOOTSTRAP_AGENT_NAME = "Main"


# The Tools a new Agent of a Live call starts with: the Live Tools, and for the
# backend Agent also web research.
_BUILTIN_DEFAULT_TOOL_ACCESS: dict[str, ToolAccess] = {
    LIVE_VOICE_BUILTIN: ToolAccess(mode=TOOL_ACCESS_MODE_SELECTED, allowed=LIVE_TOOL_NAMES),
    LIVE_BACKEND_BUILTIN: ToolAccess(
        mode=TOOL_ACCESS_MODE_SELECTED,
        allowed=(*LIVE_TOOL_NAMES, WEB_SEARCH_TOOL_NAME, WEB_FETCH_TOOL_NAME),
    ),
}
