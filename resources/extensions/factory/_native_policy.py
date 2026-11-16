"""Hard Factory capability ceilings at every owned Tool admission boundary."""

from __future__ import annotations

from typing import Any

type Json = dict[str, Any]


async def tool_permitted(
    service: Any, boundaries: list[Json], tool_name: str, grants: set[str]
) -> bool:
    catalog: list[Json] | None = None
    for boundary in boundaries:
        denied = set(boundary.get("denied_tools", []))
        if tool_name in denied:
            return False
        if "allowed_tools" not in boundary:
            continue
        active = (set(boundary["allowed_tools"]) | grants) - denied
        if tool_name in active:
            continue
        if catalog is None:
            catalog = (await service.host.catalog())["tools"]
        while True:
            followed = set()
            for tool in catalog:
                source = tool.get("activation_source")
                sources = [source] if isinstance(source, str) else (source or [])
                if (
                    tool.get("activation") == "follows"
                    and tool["name"] not in denied
                    and any(name in active for name in sources)
                ):
                    followed.add(tool["name"])
            expanded = active | followed
            if expanded == active:
                break
            active = expanded
        if tool_name not in active:
            return False
    return True
