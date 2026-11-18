"""Observe the first Agent's saved configuration for the shared setup owner."""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.chat.errors import ChatError
from core.chat.model_resolution import parse_model_with_connection
from core.settings.setup_state import SetupState, SetupStep
from core.utils.errors import StorageError
from core.utils.log_conditions import LoggedConditions
from core.utils.logging import get_logger

if TYPE_CHECKING:
    from core.agents import Agent
    from core.runtime.interfaces import RuntimeServices

_LOGGER = get_logger("runtime.setup")
_CONDITIONS = LoggedConditions()


def setup_agent(runtime: RuntimeServices) -> Agent | None:
    """Read the initial Chat Agent selected during setup, or the first remaining one."""
    previous = runtime.storage.setup.read()
    agents = [agent for agent in runtime.agents.personal_roster().agents if agent.placement is None]
    return next(
        (agent for agent in agents if agent.id == previous.agent_id), next(iter(agents), None)
    )


def update_setup(
    runtime: RuntimeServices,
    *,
    step: SetupStep | None = None,
    finish: bool = False,
    actor: str = "rpc",
) -> tuple[SetupState, bool]:
    """Blocking: resolve the target from the ordinary roster and save progress.

    A fresh Agent's Model is empty. A well-formed saved Provider/Model binding,
    including an explicit shared default, therefore records a user's selection.
    Neither catalog membership nor live credentials can undo that historical fact.
    """
    target = setup_agent(runtime)
    configured = False
    if target is not None:
        try:
            parse_model_with_connection(target.model)
        except ChatError:
            pass
        else:
            configured = True
    return runtime.storage.setup.update(
        configured=configured,
        agent_id=target.id if target is not None else None,
        step=step,
        finish=finish,
        actor=actor,
    )


def observe_setup(runtime: RuntimeServices, *, actor: str) -> bool:
    """Latch manual configuration without failing the operation that already saved it."""
    key = str(runtime.storage.data_dir)
    try:
        if runtime.storage.setup.read().status == "pending":
            _state, changed = update_setup(runtime, actor=actor)
        else:
            changed = False
    except StorageError:
        if _CONDITIONS.started(key):
            _LOGGER.warning("First setup progress unavailable; existing configuration kept")
        return False
    if _CONDITIONS.ended(key):
        _LOGGER.info("First setup progress available again")
    return changed
