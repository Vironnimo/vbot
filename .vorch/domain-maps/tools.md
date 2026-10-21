# Tools

Tool contracts and registry, Tool Access Policy resolution, provider definitions, dispatch with stable result envelopes, display metadata and scheduling.

## Overview

`core/tools/` owns contracts, registry metadata, Tool Access Policy resolution, dispatch, scheduling, display and the prompt-block seam; built-ins have per-Tool maps under `tools/`. Package exports load on first access. Chat pins and announces a Session's Tool list per prompt epoch (`chat/request-building.md`) and applies route-specific visibility filters after definition building (`tools/image.md`, `tools/edit.md`), preserving registration and permissions.

## Terms

Domain vocabulary. The core Tool term lives in `.vorch/GLOSSARY.md`.

### Readiness
**Definition:** A per-tool, cheap, I/O-free predicate (`Tool.ready`) evaluated at every prompt/tool-definition build; a Tool that is not ready stays out of Provider definitions, and dispatch answers `tool_not_ready` instead of running it (`tools/access-policy.md` -> Readiness).
**Not:** A permission - policy answers "may this Agent use it", readiness answers "can it run right now". Also not stored Extension state: the Extensions tab's waiting status is a derived display value.

### Session Grant
**Definition:** A capability Chat derives while building one Session's request state and carries through provider definitions and dispatch. Examples: an Extension's private Tools appear only in a Session its temporary binding grants them to (`extensions/host.md`); `update_goal` appears while the Session's goal is open (`tools/update_goal.md`).
**Not:** A Run-local input, Agent/Project configuration, catalog choice, or permission to address another Session.

### Definition Profile
**Definition:** A model-facing description + input schema selected from stable persisted configuration for an Agent, keyed by an immutable profile key; unchanged configuration must reproduce byte-identical provider definitions, transient state must never select a different profile.
**Not:** The canonical contract, a per-Run schema, readiness, or an execution-time fallback.

### On-demand Tool
**Definition:** A Tool an Agent may use whose definition its Tool list leaves out while the Agent's `tool_loading` switch is on and its route serves them (`agent/configuration.md` -> Fields): the System Prompt lists it by name and summary, and the Agent loads its definition with `load_tools` before the first call. `core/tools/on_demand.py::on_demand_tools` is the one classification Chat, `prompt.preview` and `scripts/tool_lab` share (`tools/load_tools.md`).
**Not:** A permission or a deferred Tool (`deferred=True`): an On-demand Tool is allowed and callable, only its definition is not shown until loaded.

### Failure modes F1-F6
**Definition:** The codes that rows of Agent-facing text tables cite for the failure a sentence prevents: F1 wrong choice (another Tool or the shell, or this Tool for the wrong job), F2 malformed call (wrong names or types, placeholders, missing fields), F3 wrong effect (wrong target, misread default or unit), F4 misread result (believes it is done, misses a truncation, polls, rereads to verify), F5 error loop (the error does not say how to fix the call), F6 context waste (long definitions, large or repeated results, extra round trips).
**Not:** The failure codes of Tool results, such as `invalid_arguments`.

## Contracts

- **Agent-facing purpose, clarity and cost:** Design around the intended user task, not preservation of the current interface. Definitions must teach a complete action and observable result; first-use guidance cannot be replaced by expected error recovery (Skills to load: `AGENTS.md` -> Agent-facing work). The instruments are `python -m scripts.tool_lab` (`definitions`, `probe`, `sessions`, `replay`).
- **Agent error tolerance:** Execute a call whose intended effect a competent reader would identify without asking - operation, target, values, scope and explicit constraints are clear - even when its expression does not match the schema: aliases, other Tool dialects, representation differences, copy artifacts, fields the selected action does not use. A round trip spent correcting such a call is a defect. Purely representational differences execute silently; an interpretation that involved judgment executes and is stated in the result (a timeout read as milliseconds, an invalid regex searched literally). Tolerance must produce the correct effect, not merely an accepted call. Never write to a guessed target, substitute an unavailable explicit target, rewrite valid application data, discard a requested effect, or resolve conflicting instructions by choosing one; a unique similar name, an existing alternative target, or fewer schema errors does not establish intent for a mutation or an explicit target. Plausible readings with different effects, or a missing decision, fail before side effects with the corrected call. General representation repair belongs to the shared contract (`tools/call-repair.md`); aliases, call wrappers, vocabulary formatting, inapplicable fields and empty-selection defaults require the owning Tool's explicit interpretation. Apply authorization and execution checks to the interpreted operation.
- **Result envelope:** `{ ok, error, data, artifacts }` - success uses `error: null`, failure `data: null` plus `error.code`/`error.message`. The top-level key set is exact (`is_tool_result_envelope()`); retry-signalling fields go *inside* `error`: optional `retryable: bool` and `attempts_made: int` tell the Model whether a failure is transient and how many attempts were already made (a Tool that exhausted its own retries sets `retryable=True` with the real count; validation/fatal failures omit it or set false). Sessions persist it as compact JSON; the Model reads a plain-text rendering that Provider wires produce (`providers/request-policy.md`), so `content`/`output` strings reach it verbatim (except neutralized look-alike `<system-reminder>` tags) and data field names are its only labels.
- **Failure codes follow the phase, not the exception type.** Whatever argument repair and validation raise refuses the call as `ToolContractError` (`invalid_arguments`), which a handler raises only before the call has any effect, like a Permission Rules refusal (`PermissionDenied`: `permission_denied`, `permissions.md`). Every other exception escaping a handler is `tool_execution_error` (effects unknown); an invalid result is `invalid_tool_result`. Same-turn failures are per-call envelopes, never Run failures. One mapping, `tool_failure_for_exception`, serves every caller (`tools/registry-dispatch.md` -> Failure codes).
- **Unknown fields:** a Tool's root `properties` are its complete parameter list (an omitted root `additionalProperties` counts as `false`; Extension and MCP schemas included). A root field outside it either runs without effect or refuses the call, by one rule for every Tool and action: it is dropped with a note in the result, unless ignoring it could give an effect the Agent did not ask for (a near-parameter name, a parameter this request left out, a mode, target or content word, a content value); then validation refuses the call before any effect. `note` is therefore a reserved data field, always a string. Handlers do not repeat unknown-argument checks; they own conditional rules such as fields one action requires (`tools/call-repair.md` -> Unknown fields).
- **Model Tool names:** the registry name identifies a Tool everywhere inside vBot (policy, history, dispatch, events, hooks, UI). `core/tools/model_names.py` may give a Tool a different name for the Model on this host (`bash` as `powershell` on Windows). Chat renames at the Provider boundary in both directions (`chat.md` -> `wire_shaping.py`); Model-facing text names a Tool by `model_tool_name` (`tools/registry-dispatch.md` -> Model Tool names).
- **Secret sinks:** a Tool names the root arguments where Agent Secret references become values (`secret_sinks`: argument -> sink class); dispatch substitutes after validation, on the handler's copy only, and an unusable reference fails `secret_unavailable` before the handler runs (`secrets/agent-secrets.md` -> References and sinks).
- **Called Tool names:** before Chat persists an Assistant turn, `called_tool_name` maps a called name that is neither offered nor registered to the offered Tool it clearly means; the name selects the operation, never a target, and an unresolved name fails `tool_not_found` without running any Tool (`tools/call-repair.md` -> Called Tool names).
- **Timing** is never inside the envelope: completed calls expose a sibling `timing {started_at, completed_at, duration_ms}` (monotonic, non-negative) on Run events and persisted Tool messages - never forwarded to adapters.
- **Display metadata** (`ToolDisplay`) is presentation-only per-call data recorded through `ToolContext`; it never enters provider definitions or result envelopes (`tools/display.md`).
- **ToolContext:** relative paths resolve from `effective_cwd`, the repo of the Run's Working Project, else the Workspace; absolute paths bypass it unless a Tool forbids them, and `memory` deliberately stays on the Workspace. `project_id` is Session/address ownership and stays `None` for Identity Sessions working in a Project, so bare Sub-Agent targets remain Identity targets; `skill_project_id` carries the Project serving Skill activation, and `working_project_id` the Run's Working Project, which a Sub-Agent with an Identity target inherits. A result names another Tool only when `can_call(tool_name)` is true; text choosing among Tools for one purpose uses `offers(tool_name)` (`tools/registry-dispatch.md` -> ToolContext).
- **Policy resolution:** `resolve_tool_access` is the single resolver behind prompt/provider definitions and dispatch: mode, automatic activation (followers, Memory mode, grants), constraints, then `denied` as absolute veto; mode `none` suppresses every activation path. A `requires_opt_in` Tool also needs its name in `tool_access.granted`, which All mode and Project ceilings never supply. Runtime-only `ToolAccess.fixed` makes a `selected` list the whole Tool set (followers still follow named Tools); only runtime-only `ToolAccess.live_call` resolves the Live Tools (`tools/access-policy.md`).
- **Concurrency:** same-turn sibling Calls run concurrently by default within bounded limits, preserving call order in results and persistence; only a registered Tool with `parallel_safe=False` is an ordering barrier, before and after itself (`tools/registry-dispatch.md` -> Concurrency and blocking work).
- **Prompt-block seam:** `ToolPromptBlockRegistry` lets a Tool declare a System Prompt block (`tool:<name>`), rendered only when the Tool is effectively allowlisted; the prompt domain imports no tool classes, and Chat pins a dynamic block's text per prompt epoch (`tools/registry-dispatch.md` -> Prompt blocks).
- **Tool changes in a Session:** Chat pins a Session's Tool list per prompt epoch and itself announces only which Tools exist (`chat/request-building.md` -> Tool catalog per prompt epoch). A changed definition of a known Tool is announced only for the parts the Tool names in `announce_changes` (`description`, `parameters`, `parameters.<name>`), since only the owning Tool knows which changes the Agent needs; otherwise the Session keeps its known definition, dispatch validates against it, and the handler answers what the current state no longer allows. Producers never announce Tool changes themselves (`tools/registry-dispatch.md` -> Tool changes).

## Conventions

- **Tool families** hand data to each other and change together with their guidance blocks and notifications: `read` -> `apply_patch` or `edit`/`write`; `search_files` -> `read`; `bash` -> `background_tasks` (commands that outlive their call) and `terminal` (programs that need input or a terminal); `subagent` -> `background_tasks` (Sub-Agent list, status and stop); `subagent` with its block and completion delivery, and `status`; `web_search` -> `web_fetch`; `memory`, `skill`/`skill_manage` with the Skills prompt section, and `session_search`.
- Renaming a Tool or one of its parameters reaches beyond the definition: inventory every place first and keep older call shapes interpreted (`tools/registry-dispatch.md` -> Renaming a Tool).

## Constraints & Gotchas

- Tool execution normally consumes the shared 500-slot semaphore (also the default per-batch limit). `subagent` and `secrets` opt out via `execution_slot_required=False`, so waiting for a child Run or the user holds no slot; `update_goal` too. Per-batch concurrency and Sub-Agent admission limits still apply.
- Chat alone derives Session Grants during request building; static restrictions and live denials stay dispatch-only and must never filter provider definitions or mutate prompt assembly. Disallowed normal Tools fail at dispatch (`tool_not_allowed`, no side effects) even if a provider asks for them.

## Agent-facing text

This domain owns the shared dispatch texts (unknown-Tool, validation and execution-failure messages, the unknown-field note) in `core/tools/tools.py`, `contracts.py` and `_tool_results.py`. Before you change, add or delete a sentence of them, read `tools/agent-facing-text.md`: it records why each sentence exists. Update its row for every sentence you change and add one for every sentence you add.

## References

Read these only when your task matches - not by default.

- Changing how a call is read: called-name resolution, unknown-Tool errors, argument and JSON quote repair, unknown fields, validation messages, `argument_normalizer`/`call_syntax.py`, `tool_lab` replay -> `tools/call-repair.md`
- Recording or rendering Tool display: detail blocks, media, file diffs, the rich-media artifact -> `tools/display.md`
- Changing policy resolution, followers, opt-in, `fixed`/`live_call`, readiness filtering, visibility flags or listing -> `tools/access-policy.md`
- Changing ToolRegistry, ToolExecutor or ToolContext internals: registration, dispatch steps, failure wording, batches, removed Tools, catalog publication, prompt blocks, Tool-change comparison -> `tools/registry-dispatch.md`
- Scratch working files of a Sub-Agent tree: retention, environment, announcement -> `tools/scratch.md`
- The retired `history` Tool and its restoration -> `tools/history.md`
- V4A patches, the shared change pipeline and edit engine, tolerant patch parsing, patch results -> `tools/apply_patch.md`
- The replacement dialect (`edit`, `write`), its follower activation, or the edit dialect per Model family -> `tools/edit.md`, `tools/write.md`
- Changing one concrete built-in Tool -> its per-tool spec (read only the one you change): `tools/read.md`, `tools/file_state.md` (read stamps, mutation locks, atomic replace, `resolve_path`), `tools/change_tracker.md` (run-delta statistics), `tools/search_files.md`, `tools/web_fetch.md`, `tools/web_search.md`, `tools/shell.md` (`bash`/`powershell`, `CommandManager`, `background_tasks`; its Sub-Agent branch: `tools/subagent.md`), `tools/terminal.md` (interactive PTY sessions), `tools/status.md`, `tools/memory.md`, `tools/image.md`, `tools/generated-media.md`, `tools/session_search.md`, `tools/project.md`, `tools/skill.md` (`skill_manage`: `skills/skill-manage.md`), `tools/subagent.md`, `tools/cron.md`, `tools/calendar.md`, `tools/channel_send.md`, `tools/speech.md`, `tools/show_widget.md`, `tools/portal.md`
- Launching or killing OS child processes -> `tools/os-processes.md`
- Classifying texts through the configured Decision Model (`classify`) -> `tools/classify.md`
- The `update_goal` Tool of a Session Goal: its grant, results and texts -> `tools/update_goal.md`
- On-demand Tools: the `load_tools` Tool, which Tools are on demand, loadable-Tool inputs, and the `summary` every loadable Tool must declare -> `tools/load_tools.md`
- Choosing how a Tool informs the Model beyond its own Result (notes, System Reminders, announcements) -> `model-communication.md`
