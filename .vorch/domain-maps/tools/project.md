# Project Tool

The `project` Tool lets an Identity Agent load a registered Project's current instructions, path, and Skill catalog before working there. It changes no execution identity or Working Project.

## Ownership and definition

`core/tools/project.py` owns the definition, handler, errors, result framing, and display. `ProjectStore` supplies Project records, `SystemPromptManager` renders files and Skills, and Runtime supplies effective Project Skills and shared `FileReadState`. Lookup, rendering, and read stamping run through the cancellation-safe Tool worker boundary. Chat uses the ordinary persisted Tool Result as the context carrier.

The open model schema has one required string, `project_id`. A `ToolDefinitionProfile` appends all valid registered ids and display names to its description, sorted by id, as `- id: Display Name` (only `- id` when equal). Display names collapse whitespace. The list has no paths, XML, reachability markers, or enum; a missing repository stays listed. Profile identity depends on the rendered choices, not cwd. An unreadable catalog keeps the Tool visible and says the list is unavailable; an empty valid catalog says no loadable Projects are available. Catalog failures and recovery log once per condition transition.

There is no `tool:project` System Prompt block or registration. The Tool description carries when to load and to wait for the result before dependent calls. Chat pins its definition and announces changes to `parameters.project_id` through the existing Tool-change mechanism, once per change; the pinned System Prompt and native Tool list stay unchanged until the next prompt epoch. Existing Sessions retain an old prompt block for their current epoch under the generic pin contract. Coverage: `test_project_choices_belong_to_the_tool_not_the_system_prompt`, `test_chat_prompt.py::test_dynamic_blocks_stay_pinned_and_each_change_is_announced_once`.

## Calls and results

- Common normalization accepts `project`, `id`, `name`, and `project_name` as field aliases; conflicting fields fail. Unknown fields follow the common rule (`tools.md`); meaningful switches such as `force` are refused before loading. Project ids are exact, including case. Similar names are suggestions only; loading instructions and activating Skills never uses a guessed target.
- Success retains `status: "loaded"`, `project_id`, `project_path`, and `content`. The content contains a short preamble, instruction files, and Skill names/descriptions, each once. All filesystem paths use shared Model-facing forward slashes. Shell guidance applies only to work in this Project and names the host's shell Tool only when callable; file guidance and `skill` guidance are also conditional on this Run's Tool access.
- When the Agent's `project_memory_enabled` and the loaded Project's `memory_enabled` are both on, `content` also includes its shared Project Memory and identifies the Project. This read does not require the `memory` Tool, respects the Memory prompt budget, and stamps only the fully included Memory file. Project disablement and anchor reads run under the Project owner's configuration guard.
- Every fully included instruction file is stamped read with the metadata captured before reading its bytes, never the later state at result assembly. Missing, unreadable, non-UTF-8, and oversized files are not stamped. `render_project_files(..., on_issue=...)` reports omissions; a successful partial load adds `data.note` naming each problem and its recovery. An absent seeded `AGENTS.md` is normal; other missing configured paths are reported. Oversized files keep the shared 128 KiB notice and conditional `read` hint, without duplicating that notice in the warning.
- Under Permission Rules a Project folder the rules refuse to read fails `permission_denied` before any status check; a refused instruction file is never opened and is reported in `data.note` (`Not included: <path> is refused by the permission rules. ...`). Loading never changes the rules or the working directory.
- Display shows `Project Context loaded.` with the path, any partial-load warning, and `data.content` by reference. Failures show their actionable error.
- Chat recovers the latest successful activation from `data.status == "loaded"` and `data.project_id` (`core/sessions/history.py::project_tool_context_id`). A partial success still activates the Project's effective Skill set; a failure loads no new Project Context and leaves the previous activation intact.

## Errors

All handler failures say no new Project Context was loaded, identify the input or Project, and give a next step. OS text, validator diagnostics, and internal exception details remain in logs.

- `invalid_arguments`: empty id; copy an id from the parameter's list. Missing/type/conflict/unknown-field errors use common dispatch validation.
- `project_not_found`: invalid or unknown id; up to three nearby id/name choices, otherwise sample choices and a pointer to the full parameter list. If listing fails, report unavailable choices rather than claiming no Projects exist.
- `project_identity_required`: direct calls from Config/Project Agents; use the Working Project context or a Session in the intended Project.
- `project_path_missing` / `project_path_not_directory`: restore the repository or correct its configured path.
- `project_configuration_invalid`: invalid or incompatible stored configuration; repair or restore it and load the same id.
- `project_access_denied`: restore the vBot server's read access.
- `project_io_error`: storage failure; known transient POSIX/Windows errors allow one retry after recovery, other failures ask to restore storage access.
- `project_load_failed`: unexpected internal failure; retry once, then report it for log inspection. Only transient I/O and unexpected internal failures are retryable.
- `project_memory_unavailable`: an enabled Project Memory could not be read; repair or restore it in the Project settings before loading again. No new Project Context is activated by this failure.

Coverage: `tests/core/tools/test_project.py` dispatches through the production registry and shared renderer, including partial reads, permission-sensitive guidance, strict ids, configuration failures, transient storage errors, and unavailable catalog recovery.

## Invariants and interfaces

- `identity_agent` is the declarative visibility/dispatch constraint; `project_tool_configurability_reason` excludes it from the Project Tool Whitelist. The handler also refuses direct Project-Agent calls.
- Loading changes no cwd, Working Project, default Project, Workspace, Session ownership/addressing, configured permissions/Skill allowlist, or Prompt-Epoch Catalog. It activates the Project's complete effective Skill set for the Identity Session, including when its personal Skill allowlist is empty; Project disables and shared-Skill opt-ins remain authoritative. A later successful load replaces that activation. Chat changes the Run-local registry only after persisting success, and later Runs recover it from history. Compaction's catalog still derives from the admitted Working Project/private scope.
- Calls may run in parallel with independent reads (`parallel_safe=True`); dependent calls must wait. No file or shell call implicitly loads Project Context. Repeated explicit loads refresh live configuration and instructions. File work uses absolute paths; each one-shot shell call targeting this Project sets `workdir` to `project_path`.
- `register_project_tool(registry, projects, get_renderer, list_project_skills, file_state, memory_service)` registers the Tool. `make_project_handler(...)` builds its handler. `SystemPromptManager.render_project_files` and `render_project_skills` own shared presentation; the Tool supplies `ToolContext.can_call` to both and read/issue observers to the file renderer. `MemoryService.read_prompt_scopes` supplies the bounded Project Memory rendering.

## Agent-facing text

| Sentence or start | Failure prevented and evidence |
| --- | --- |
| `Load a registered Project's instructions, path, and Skill catalog before working in it, unless it is already your Working Project.` | F1/F6: loading before repository work supplies its rules; the Working Project exception avoids reloading automatically supplied context. User-approved move from the dedicated prompt block (2026-10-14). |
| `Wait for the result before dependent Tool calls; your working directory stays unchanged.` | F3: a sibling call cannot use instructions/path before the result, and loading is not a directory switch. |
| `ID of the Project to load. Available Projects:` | F2/F6: supplies exact input values without a list call; ids/names replace the path-bearing System Prompt catalog (user decision 2026-10-14). Empty/unavailable alternatives distinguish absence from failed discovery. |
| `Load context for a registered Project.` | F1: the on-demand summary distinguishes context loading from Project creation or management. |
| `Project Context loaded for ... Follow the files below when working in this Project.` | F4: identifies success and instruction scope; the second sentence appears only with included instructions. |
| `For file work in this Project ... For shell work in this Project ...` | F3: returned paths do not change file resolution or one-shot shell cwd; wording stays scoped to this Project and callable Tools. |
| `Project Skills now resolve from this Project. This replaces the previous Project Skill context.` | F3: later Skill loads follow the last success; replacement is mentioned only when a previous Project differs. |
| `Skills from Project ... - load one by name with the skill Tool` | F2: the result supplies Skill names; loading guidance appears only when `skill` is callable. |
| `Some Project instruction files were not included. Not included: ...` | F4/F5: a partial success must not imply unreadable rules were seen. Dispatch tests cover missing, denied, non-file, encoding, I/O, and oversized inputs. Each notice names its recovery; oversized details remain in their shared file notice. |
| `Not included: <path> is refused by the permission rules. If the task needs it, tell the user.` | F4/F5: a partial load must not imply the refused rules were seen; reading the file elsewhere is refused too, so only the user can change it. |
| `No new Project Context was loaded. ...` plus the cause and next step | F4/F5: failures neither claim success nor imply unknown side effects. Distinct errors above direct id repair, configuration/path/access repair, or one retry followed by reporting the failure. |
