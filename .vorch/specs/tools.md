# Tools

Tool metadata registry, allowlist filtering, provider definitions, and async dispatch.

## Overview

`core/tools/` owns the registry of callable tools available to an agentic loop. The default registry is empty; built-in host tools are not part of Phase 2. The same allowlist filtering controls prompt-visible tools and official provider API tool definitions.

## Data Model

- `Tool`: `name`, `description`, `parameters`, `handler`.
- `parameters` is a JSON Schema object for provider tool definitions.
- `handler` receives a JSON object of arguments and returns a JSON object, synchronously or asynchronously.

## Interfaces

- `ToolRegistry.register(name, description, parameters, handler) -> Tool`
- `get(name) -> Tool`
- `list_tools(allowed_tools=None) -> list[Tool]`
- `provider_definitions(allowed_tools=None) -> list[dict]` — name, description, JSON Schema.
- `prompt_definitions(allowed_tools=None) -> list[dict]` — name and description only.
- `dispatch(name, arguments, allowed_tools=None) -> dict` — executes through an async interface.

## Conventions

- `allowed_tools=None` and `['*']` mean all registered tools.
- `allowed_tools=[]` means no tools.
- Explicit allowlists match exact tool names; unknown names are ignored for listing and fail if dispatched.

## Constraints & Gotchas

- Tool results must be JSON objects. Non-object results are rejected.
- Disallowed tools are blocked at dispatch time even if a provider asks for them.
