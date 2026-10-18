# Runtime Extension Host

The Runtime side of the Extension host: `ExtensionHostFactory` and its temporary execution groups, editor catalogs, `inspect_prompt`, Tool/Extension prompt-block contributions, Extension sampling and `resolve_tool_agent`. The Extension kernel and its API live in `extensions.md`.

## Host factory and execution groups

- Owner-bound Extension hosts receive temporary execution groups, safe editor catalogs and existing Statistics projections (over the Runtime's shared `statistics_index`) through injection. Whole-roster preflight uses ordinary Model, Tool/settings and Project-ceiling validators off the Event Loop, then rechecks the loaded registration before admission. `_extension_host.py::ExtensionHostFactory` owns these groups and projections with explicit services and live registry/Skill callbacks; it never receives the Runtime object. Source/tests: `_extension_host.py`, `tests/core/runtime/test_runtime_extension_host.py`.
- Extension-owned temporary execution groups receive `chat_loop` through the existing host operations seam, so their Runs expose the same in-flight output and request diagnostics as interactive Chat (`test_runtime_extension_host.py`).
- Editor catalogs project Model reasoning capabilities and context size, plus Tool family/activation metadata, from their existing registry owners so isolated editors can use the shared selectors without inventing capability defaults (`test_runtime_extension_host.py`).
- Editor catalogs also expose System Prompt block metadata. Owner-bound `inspect_prompt` resolves a prospective temporary Agent with ordinary Project ceilings, applies Chat's Model-specific Tool routing and the owner's private Tool grants, then returns the combined prompt, rendered block details and effective Tool definitions. Inspection creates no Session or Run and rechecks registration identity after asynchronous work (`test_runtime_extension_host.py`).

## Prompt blocks and sampling

The storage scope adapter and Tool/Extension Prompt-block contributions live together in `_prompt_blocks.py`; bootstrap and reload use the same projection.

Extension sampling (`_extension_sampling.py`) resolves the Agent, Model route and Adapter off the Event Loop. It retains the Adapter and UsageRecorder across awaits, so cancellation and readiness withdrawal still finish Usage accounting and close the acquired Adapter; it records its own exposed Adapter request, and it retains its injected recorder through readiness withdrawal during shutdown.

## Resolving the calling Agent

Extension Tool consumers resolve their calling Agent through `ExtensionHost.resolve_tool_agent(context)`. Runtime checks the canonical temporary Session binding against the execution owner before temporary resolution, otherwise uses normal Identity/Project resolution. A context whose Session does not exist is a call outside any Session (Extension management such as MCP `explore`/`invoke`): without an execution owner it resolves the addressed Agent with no Session overrides; with one it fails (`Temporary Tool invocation no longer owns this Session`). MCP policy and sampling share this seam (`tests/core/runtime/test_runtime_mcp_access.py`).
