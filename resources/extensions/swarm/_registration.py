"""Bind the bundled Swarm service to Extension declarations."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from core.extensions import (
    ExtensionAPI,
)

from ._extension_values import (
    Json,
)
from ._operation_schemas import (
    _OPERATION_SCHEMAS,
)
from .agent_text import (
    BOARD_DESCRIPTION,
    BOARD_PARAMETERS,
    INBOX_DESCRIPTION,
    INBOX_PARAMETERS,
    STATE_DESCRIPTION,
    STATE_PARAMETERS,
)

if TYPE_CHECKING:
    from .extension import SwarmExtension


def register(api: ExtensionAPI) -> None:
    from .extension import SwarmExtension

    service = SwarmExtension(api)
    api.operations.startup.append(service.start)
    api.on_shutdown(service.close)
    api.register_session_tool("swarm_board", BOARD_DESCRIPTION, BOARD_PARAMETERS, service.board)
    api.register_session_tool("swarm_inbox", INBOX_DESCRIPTION, INBOX_PARAMETERS, service.inbox)
    api.register_session_tool(
        "swarm_state", STATE_DESCRIPTION, STATE_PARAMETERS, service.state, parallel_safe=False
    )
    api.register_session_runtime(
        before_request=service._before_request,
        run_finished=service._run_finished,
        quiesce=service._quiesce,
        acknowledge_delivery=service._acknowledge_delivery,
        reconcile_tool_batch=service._reconcile_tool_batch,
    )
    api.register_page("swarms", "Swarms", "web/page.html")
    api.register_command(
        "swarm",
        "Start a Swarm from a profile and goal.",
        service.command,
        argument="optional",
        execution_mode="immediate",
    )
    descriptions = {
        "catalog": "Read selectable Models, Tools, Skills, Projects, and prompt defaults.",
        "profiles.preview": "Preview an unsaved profile's complete prompt without starting a Run.",
        "profiles.list": "List saved profiles with pagination; use profiles.get for full content.",
        "profiles.get": "Read one complete saved profile and its revision before editing.",
        "profiles.save": "Create or replace a profile; updating requires its expected_revision.",
        "profiles.delete": "Delete a saved profile at its expected_revision.",
        "swarms.list": "List retained Swarm executions with pagination.",
        "swarms.get": "Read one Swarm's state, saved profile snapshot, and participants.",
        "swarms.events": "Read a page of Swarm lifecycle events after the supplied cursor.",
        "swarms.settings": (
            "Update delivery settings at expected_revision; preserve request_id on retry."
        ),
        "swarms.start": "Start a profile with a goal; reuse request_id only for the same request.",
        "swarms.stop": "Stop a Swarm's active Runs while retaining its Sessions and Board.",
        "swarms.delete": "Delete a stopped Swarm and its owned Sessions and Board.",
        "swarms.resume": (
            "Resume inactive participants in a retained Swarm, optionally selecting one."
        ),
        "swarms.usage": "Read Swarm usage, optionally restricted to one participant.",
        "board.list": "List a Swarm's discussions with pagination.",
        "board.read": "Read Board posts or one exact message; follow the returned cursor for more.",
        "board.post": (
            "Post to the Board; reuse request_id only for the same content and recipients."
        ),
    }
    for name, schema in _OPERATION_SCHEMAS.items():
        api.operations.register(
            name,
            descriptions[name],
            schema,
            _operation_handler(service, name),
        )


def _operation_handler(service: SwarmExtension, name: str) -> Callable[[Json], Awaitable[Json]]:
    async def handler(arguments: Json) -> Json:
        return await service.operation(name, arguments)

    return handler
