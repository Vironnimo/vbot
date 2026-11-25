"""Declarations for the bundled Factory; registration acquires no resources."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from core.extensions import ExtensionAPI
from core.tools import ToolContext, ToolDisplay

from ._job_agent_text import JOB_PRIVATE_TOOLS
from ._job_management import (
    JOB_OPERATION_DESCRIPTIONS,
    JOB_OPERATION_SCHEMAS,
    JOB_SECRET_OPERATIONS,
)
from ._native_agent_text import PRIVATE_TOOLS as NATIVE_TOOLS
from ._native_values import OPERATION_DESCRIPTIONS as NATIVE_DESCRIPTIONS
from ._native_values import OPERATION_SCHEMAS as NATIVE_SCHEMAS
from ._public_tool import PUBLIC_DESCRIPTION, PUBLIC_PARAMETERS, PUBLIC_TOOL_NAME

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
                "label": "Max concurrent runs",
                "description": (
                    "Maximum Factory runs working at once across all Factories. "
                    "Set to 0 for unlimited."
                ),
                "default": 0,
            },
        ]
    )
    api.register_tool_family("factory", "Software Factory")
    api.register_tool(
        PUBLIC_TOOL_NAME,
        PUBLIC_DESCRIPTION,
        PUBLIC_PARAMETERS,
        service.public_tool,
        summary="Manage Software Factories, specialist Agents, and Jobs across Projects.",
        family="factory",
        display=ToolDisplay(summary_fields=("action", "name", "factory_id", "job_id")),
    )
    for name, (description, parameters) in (JOB_PRIVATE_TOOLS | NATIVE_TOOLS).items():
        api.register_session_tool(
            name, description, parameters, _tool_handler(service, name), parallel_safe=False
        )
    api.register_session_runtime(
        before_request=service.before_request,
        before_tool_call=service.before_tool_call,
        before_model_request=service.before_model_request,
        acknowledge_delivery=service.acknowledge_delivery,
        run_finished=service.run_finished,
        quiesce=service.quiesce,
    )
    api.register_page("board", "Software Factory", "web/page.html", icon="factory")
    schemas: dict[str, Json] = {
        "catalog": {"type": "object", "properties": {}, "additionalProperties": False},
        **NATIVE_SCHEMAS,
        **JOB_OPERATION_SCHEMAS,
    }
    descriptions: dict[str, str] = {
        "catalog": "Read available Projects, Agents, Models, permission profiles, and Factories.",
        **NATIVE_DESCRIPTIONS,
        **JOB_OPERATION_DESCRIPTIONS,
    }
    for name, parameters in schemas.items():
        api.operations.register(
            name,
            descriptions.get(name, name.replace(".", " ")),
            parameters,
            _operation_handler(service, name),
            secret=name in JOB_SECRET_OPERATIONS,
        )


def _operation_handler(service: FactoryExtension, name: str) -> Callable[[Json], Awaitable[Json]]:
    async def handler(arguments: Json) -> Json:
        return await service.management.operation(name, arguments)

    return handler


def _tool_handler(service: FactoryExtension, name: str) -> Callable[..., Awaitable[Json]]:
    async def handler(context: ToolContext, arguments: Json) -> Json:
        return await service.private_tool(name, context, arguments)

    return handler
