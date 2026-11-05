# call_tool

The fixed transport Tool for Models that call only names in their request's
Tool list. `core/tools/call_tool.py` owns its definition and delegates to the
existing `ToolContext.call_tool`; Chat owns route selection, the prompt epoch,
child dispatch and persistence (`chat/request-building.md`, `tools/registry-dispatch.md`).

## Contract

- Internal, catalog-hidden and never on demand. A route with native
  `request.on_demand_tools: false` pins `load_tools` and `call_tool` from the
  first request, including when no normal Tool is selected yet. Tool access
  `none` offers neither. The Agent's Tool loading choice and always-loaded
  selection still decide which target definitions are listed directly.
- `name` and object `arguments` identify one target call. Only `name` is
  required: `arguments` may be omitted, and `null` or `""` count as omitted.
  Shared repair accepts `tool`/`tool_name` and `args`/`parameters`/`input`. Fields
  beside `arguments` are target parameters and move into it; one sent both ways
  is refused. Target arguments then pass into the ordinary target-specific
  validation and repair.
- The proxy grants no authority, so no Run denial covers it or `load_tools`
  (`TOOL_TRANSPORT_NAMES`, `ToolContext.tool_denial`): a Reflection Run or Channel
  member reaches exactly its allowed Tools, each checked on its own. Targets
  retain contracts, Session Grants,
  live denials, Permission Rules, Extension hooks, cancellation and durable
  Results. The proxy keeps the normal bounded Model projection of MCP results, rather than Python raw data. It returns the target envelope directly; request media is
  re-correlated to its outer Result. History also retains the named target as
  child activity, outside Model Context, as for Python calls.
- The parent schedules like its target (`scheduling_target`,
  `ToolRegistry.is_parallel_safe`): calls to parallel-safe targets overlap, a
  serial target stays a barrier, and a call naming no known target stays serial.
  The parent occupies no global execution slot, so its target gets normal slots.
  Repetition advice observes the target, skipping the wrapper.
- `parent_tool_name` is a host-authored child execution fact. Python may start
  through the proxy, but Python cannot recursively execute Python; a nested
  proxy call is refused. No separate Python process implements the proxy.
- Stop of the proxy cancels its active target as a per-Tool stop, including before child creation or callback registration; it never becomes a Run cancel.
- Skill bodies use the existing typed activation note once. The outer proxy Result keeps activation metadata only, so Skill instructions expire normally at Compaction. A Project load outside a Project keeps its body in its note the same way.
- The Run timeline shows one row per reached target, marked `proxied`; the parent row remains only for a call that reached no target (`chat/timeline.md`).
- A load routed through the proxy records definitions only with the visible
  successful outer Result. Ordinary Python loads remain unannounced to the Model.
- Proxy mode stays with its epoch across route changes. A listed-only primary
  route gives an epoch without it `call_tool` and `load_tools` through Tool-change
  notes once a Tool exists that the pin does not list; a fully listed epoch keeps
  its `tools[]` until then (`chat/request-building.md` -> Fixed proxy transport).
  Target additions never expand a proxy epoch's `tools[]`.

## Agent-facing text

Approved 2026-11-05. The complete definition and parameters total about 62
text tokens (characters / 4); this transport replaces listing every target.

| Text | Reason |
|---|---|
| `Call an available Tool using its name and arguments.` | States the actual operation without requiring a program or another loader call. |
| `Use this for Tools loaded with load_tools or announced in this Session.` | Covers both on-demand definitions and a Session Grant such as update_goal arriving after creation. |
| `The Tool name from its definition.` | Names the source of a valid target rather than inviting invented operations. |
| `The arguments for that Tool, as an object. Omit for a Tool without parameters.` | Keeps target values structured, following the loaded schema, instead of a Python program or stringified JSON; the omit rule covers a Tool without parameters, which a required field refused. |
| `<field> appears both inside and beside arguments; send it once, inside arguments.` | Two values for one parameter: guessing could run the wrong call. |
| `Nothing was run.` | A refused nested proxy or missing Run causes no target effects. |
| `Set name to the target Tool, not call_tool.` | Repairs recursion without another recursive example. |
| `Inside Python, call the target with tools.<name>(...).` | A Python caller already has the direct client; another proxy adds no capability. |
| `The user stopped this Tool call.` | An early parent stop prevents creating or executing a target. |
| `call_tool needs an active conversation Run.` | Explains the missing dispatch context for callers outside Chat. |

Reading a search definition means `name=web_search` with its query object;
finishing a granted goal means `name=update_goal` with the checked status.
An unavailable or refused target retains its normal failure and changes nothing.
Loader result wording belongs to `tools/load_tools.md`; added/changed Tool
reminders belong to `chat/request-building.md`.

## Verification

`tests/core/chat/test_tool_proxy.py` covers target authority, validation, request
media, repetition, Python and recursion through production dispatch.
`test_chat_loop_tool_definitions.py` covers load/call with fixed request Tools;
`test_chat_goal_session.py` covers Agent creation and a manual goal in an
already-running epoch without creation access.
