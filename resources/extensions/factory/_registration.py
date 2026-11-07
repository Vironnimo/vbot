"""Declarations for the bundled Factory; registration acquires no resources."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from core.extensions import ExtensionAPI
from core.tools import ToolContext, ToolDisplay

from ._values import OPERATION_DESCRIPTIONS, OPERATION_SCHEMAS
from .agent_text import PRIVATE_TOOLS, PUBLIC_DESCRIPTION, PUBLIC_PARAMETERS

type Json = dict[str, Any]

if TYPE_CHECKING:
    from .extension import FactoryExtension


def register(api: ExtensionAPI) -> None:
    from .extension import FactoryExtension

    service = FactoryExtension(api)
    api.operations.startup.append(service.start)
    api.on_shutdown(service.close)
    api.on_config_change(service.config_changed)
    api.register_settings(
        [
            {
                "key": "wip_limit",
                "type": "number",
                "label": "WIP limit",
                "description": "Maximum Cards working at once.",
                "default": 2,
            },
            {
                "key": "max_waiting",
                "type": "number",
                "label": "Maximum waiting",
                "description": "Stop starting Cards while this many wait for you. "
                "0 means unlimited.",
                "default": 0,
            },
        ]
    )
    api.register_tool_family("factory", "Software Factory")
    api.register_tool(
        "factory_card",
        PUBLIC_DESCRIPTION,
        PUBLIC_PARAMETERS,
        service.public_tool,
        summary="Create proposed work and read Software Factory Cards.",
        family="factory",
        display=ToolDisplay(summary_fields=("action", "title", "id")),
    )
    for name, (description, parameters) in PRIVATE_TOOLS.items():
        api.register_session_tool(
            name, description, parameters, _tool_handler(service, name), parallel_safe=False
        )
    api.register_session_runtime(
        before_request=service.execution.before_request,
        acknowledge_delivery=service.execution.acknowledge_delivery,
        run_finished=service.execution.run_finished,
        quiesce=service.execution.quiesce,
    )
    api.register_page("board", "Software Factory", "web/page.html", icon="factory")
    for name, parameters in OPERATION_SCHEMAS.items():
        api.operations.register(
            name,
            OPERATION_DESCRIPTIONS.get(name, name.replace(".", " ")),
            parameters,
            _operation_handler(service, name),
        )


def _operation_handler(service: FactoryExtension, name: str) -> Callable[[Json], Awaitable[Json]]:
    async def handler(arguments: Json) -> Json:
        return await service.management.operation(name, arguments)

    return handler


def _tool_handler(service: FactoryExtension, name: str) -> Callable[..., Awaitable[Json]]:
    async def handler(context: ToolContext, arguments: Json) -> Json:
        return await service.private_tool(name, context, arguments)

    return handler
