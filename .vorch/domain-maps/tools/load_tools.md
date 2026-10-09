# load_tools Tool

`core/tools/load_tools.py` registers the internal Tool `load_tools(names)`. It returns the full definitions of On-demand Tools (`tools.md` -> Terms) as readable text, so an Agent whose `tool_loading` switch is on can call them correctly. The classification of On-demand Tools is `core/tools/on_demand.py`; how a load joins the prompt epoch is Chat's (`chat/request-building.md`).

## Interfaces

- Registration: `register_load_tools_tool(registry, prompt_blocks=None)`, called once at Runtime bootstrap (`core/runtime/_bootstrap.py`) with the Runtime's `ToolPromptBlockRegistry`; given one, it also registers the System Prompt block `tool:load_tools`: static, user-editable text (`LOAD_TOOLS_BLOCK_TEXT`) around `{generated:on_demand_tool_list}`, with `requires_generated` so it renders nothing while no On-demand Tool is listed (`prompts.md`). `internal=True`: it is never part of a Tool Access Policy, `tool.list` or the WebUI picker, and is callable only where the request lists it. It is offered only while the Agent has a Tool to load: the prompt definitions append it while one of their Tools is on demand (`prompts.md`), and Chat's prompt epoch lists or announces it only with an On-demand Tool (`chat/request-building.md`).
- Inputs from the caller: `ToolExecutionConfig.loadable_tools` maps registry names of the On-demand Tools the call may load to their Model request definitions (after per-Model routing); those Tools are also in `input_contracts`, so an On-demand Tool is callable before it is loaded. `ToolExecutionConfig.unloadable_tools` names the On-demand Tools the Model was told it can load whose definition cannot be loaded now (Chat: `RequestState.unloadable_tools`, the epoch's Tools known by name only that the primary route does not offer now: not ready, or route-gated). The executor sets `ToolContext.offered_tools` to the listed Tools only (`input_contracts` minus `loadable_tools`), `ToolContext.loadable_tools` to the map and `ToolContext.unloadable_tools` to the names. A loadable Tool the Run's allowlist refuses (`context.can_call`) counts as unloadable. The handler reads readiness hints from the registry it was registered with.
- Recording a load: the handler calls `context.record_loaded_tools(names)`; the call's owned effects (committed only for an `ok` result) pass `(tool_call_id, registry names)` to `ToolExecutionConfig.tool_load_registrar`. A failed call records nothing.
- Unknown-Tool failures list loadable Tools and listed internal Tools among the available Tools (`ToolRegistry._unknown_tool_message`).
- Chat: the Tool pin, the silent `loaded` note a successful call persists with its Tool Result, the `on_demand` note for a Tool that becomes loadable mid-epoch, and Compaction's handling -> `chat/request-building.md` -> Tool catalog per prompt epoch.

## Result

Success content is text: the status lines, then one section per loaded Tool, then a closing line, separated by blank lines.

- One status line per requested name in call order, duplicates (after name matching) dropped: `- <name>: loaded`, `- <name>: already available; call it directly` (a Tool the request lists, including one loaded earlier in the epoch once Chat lists it as known), `- <name>: listed, but cannot be used right now; continue without it` (an unloadable Tool; a Tool that is not ready and has a readiness hint gets ` (<hint without its final period>)` before the `;`), `- <raw name>: not available to load` (a name that matches no listed Tool). When some names were unknown and loadable Tools remain, the line `Tools you can load: <names>.` follows (Model-facing names, sorted).
- Section per loaded Tool: `Tool: <model name>`, `Description: <full description>`, `Parameters (JSON Schema): <compact JSON>`.
- Closing line when something was loaded: `Call the loaded Tools by name with normal Tool calls.`
- When no name was loaded or already available and at least one is unloadable, the call fails with `tool_unavailable` (nothing recorded): `Nothing was loaded.`, then the status lines as above.
- Refusals (`invalid_arguments`, nothing recorded): no name given -> `Nothing was loaded: the call named no Tool.`; every name unknown -> `Nothing was loaded: no Tool named "a" or "b" can be loaded.` Each continues with `Tools you can load: <names>. Call load_tools again with "names" set to the Tools the task needs from that list, for example {"names": ["<first>"]}.`; with nothing loadable now but unloadable Tools listed, with `No listed Tool can be loaded right now; continue with the Tools you can already call.`; with no listed Tool left to load, with `Every Tool you can use is already available; call it directly by name.`

## Argument Reading

Name matching uses `called_tool_name` against the loadable and listed Tools: registry or Model-facing name, case and separators ignored, namespace prefixes (`functions.`), other harnesses' names (`WebSearch`), one-edit typos. The argument normalizer accepts a bare list or text as `names`; the aliases `name`, `tool`, `tools`, `tool_name`, `tool_names`, `query`, `select`; text holding several names separated by commas, spaces or line breaks; Claude Code's ToolSearch form `select:a,b`; a JSON array written as text; quotes or backticks around a name; objects with `name`/`tool`. A call without names keeps `names: []` and gets the refusal that lists the loadable Tools.

## Conventions

Display: one identifier part listing the requested Model-facing names. Tests: `tests/core/tools/test_load_tools.py` (production dispatch: loaded sections, outcomes in call order, unloadable versus unknown names, tolerated call forms, refusals), `test_chat_loop_tool_definitions.py::test_a_restricted_run_keeps_load_tools_and_loads_only_the_tools_it_may_call` (Chat supplies unloadable Tools: refused by the Run, not ready) and `test_tools_execution.py::test_unknown_or_disallowed_tool_becomes_a_failed_result` (loadable and internal Tools in the unknown-Tool list).

## Agent-facing text

| Text | Reason |
|---|---|
| `Load the full definitions of Tools that your instructions list as loadable on demand, so you can call them.` | Names the job and points at the System Prompt list the names come from, so the Agent does not guess names or search for Tools. Does not quote the block heading, which the user can edit. |
| `Pass every Tool the current task needs in one call.` | One call per task keeps the extra Model turns low; weak Models otherwise load one Tool per call. |
| `names`: `Names of the Tools to load, exactly as listed.` | Pins the source of valid values to the listed names. |
| `- <name>: loaded` / `already available; call it directly` / `not available to load` | Batch result: each name's outcome by position, so the Agent knows which calls it can now make and does not reload a listed Tool. |
| `- <name>: listed, but cannot be used right now (<hint>); continue without it` | A listed Tool that is not ready, not offered on this route or refused by the Run: says the name was right, so the Agent does not retry other spellings, and gives the next action; the readiness hint names the cause the user can fix. |
| `Nothing was loaded.` (failure `tool_unavailable`) | Without it a call that loaded nothing would read as `ok`. |
| `No listed Tool can be loaded right now; continue with the Tools you can already call.` | Replaces the `Every Tool ... already available` sentence while listed Tools remain that cannot be loaded now, which that sentence would deny. |
| `Tools you can load: <names>.` | After an unknown name, the valid values for a follow-up call. |
| `Tool: ...`, `Description: ...`, `Parameters (JSON Schema): ...` | The full definition in the order a Tool list shows it, readable without escaped JSON. |
| `Call the loaded Tools by name with normal Tool calls.` | Prevents calls routed through `load_tools` or another wrapper. |
| `Nothing was loaded: the call named no Tool.` / `Nothing was loaded: no Tool named "<name>" can be loaded.` | States that nothing changed and why, with the rejected value. |
| `Call load_tools again with "names" set to the Tools the task needs from that list, for example {"names": [...]}.` | The next call in copyable form. |
| `Every Tool you can use is already available; call it directly by name.` | Stops retries when no listed Tool remains to load. |
| Block `## Tools Loaded on Demand` | A heading the Agent can find the list under again. |
| Block `You can also use the Tools listed below, but their definitions are not in your Tool list.` | Says the listed Tools are usable, so the Agent does not treat them as unavailable. |
| Block ``Before you call one of them for the first time, load its definition with `load_tools`.`` | The required next action, and only once per Tool. |
| Block `Load all Tools a task needs in one call.` | Keeps extra Model turns low. |
| Block entries `- <name>: <summary>` | The Model-facing name to call and one sentence to decide whether the task needs the Tool. The sentence is the Tool's own `summary` (`tools.md` -> Conventions), because a description's opening sentence assumes the Agent already has the Tool and often omits when to use it. |
| `on_demand` note ``Tool <name> is now available: <summary> Load its definition with `load_tools` before you call it.`` | A Tool enabled mid-epoch is not in the pinned block; the note gives the same facts and next action. |
