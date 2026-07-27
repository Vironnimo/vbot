# Project Tool

The `project` Tool lets an Identity Agent explicitly load the current context of any registered Project without changing its execution identity or working Project.

## Overview

`core/tools/project.py` owns the Tool schema, handler, result framing, and dynamic `tool:project` System Prompt block. `ProjectStore` supplies the registered Project records, `SystemPromptManager` supplies the shared Project-file and Project-Skill rendering, Runtime supplies the Project-only Skill scan and shared `FileReadState`, and generic Tool availability owns model visibility plus dispatch permission. Chat has no special Project-load path: the normal persisted Tool result is the context carrier.

The Tool accepts one exact `project_id`. On success it returns status, Project id/display name/cwd, one model-facing `content` string containing the Project contract, readable auto-load files, and the Project Skill list, plus structured `loaded_files` and `skills` metadata. The persisted successful Tool Result is also the Session's source of truth for the latest explicitly loaded Project Skill context: Chat recovers it on later Runs, and the `skill` Tool uses that Project-aware registry without changing the Session-pinned System Prompt catalog. Missing auto-load files are skipped through the prompt renderer's existing fail-soft behavior; a missing Project, invalid id, unreachable cwd, or Project-load failure returns a stable non-retryable Tool failure.

## Invariants

- `project` is identity-only. `IDENTITY_ONLY_TOOLS` removes it from Config/Project Agents at both provider/prompt visibility and dispatch, the handler rejects a direct Config-Agent call defensively, and `project_tool_configurability_reason` keeps it out of the Project Tool Whitelist.
- Loading Project Context changes no cwd, Rooting, Workspace, Session ownership, permissions, addressing, configured Skill allowlist, Session-pinned Skill catalog, or persistent active-Project state. It does make the loaded Project's Skills visible and loadable through the ordinary `skill` Tool for that Identity Session; a later successful `project` call replaces this activation context. File Tools must use absolute paths. Every `bash` call targeting the Project must set `workdir` to the returned cwd again because each call is a one-shot shell and retains no cwd change from an earlier call.
- No file/path Tool call implicitly loads Project Context. An Identity Agent calls `project` explicitly before working in a registered Project that is not its admitted working Project; repeated calls intentionally reload the live Project configuration and files.
- The `project` call must be alone before dependent Tool Calls. `project` is explicitly parallel-safe for independent reads, so another parallel-safe sibling cannot depend on its result; this ordering requirement is stated in both the Tool description and its prompt block.
- Every readable auto-load file included in the result is stamped into the calling Session's shared `FileReadState`, preserving read-before-write. Missing or unreadable files are not stamped.
- Project Skills are listed in the Tool Result and load by name through the ordinary `skill` Tool. Chat switches only the Run-local activation registry after the successful Project Tool Result is persisted; the pinned prompt catalog remains byte-identical.

## Prompt Contract

The dynamic `tool:project` block renders only when `project` is in the Agent's effective Tool set. It lists every valid registered Project, sorted by id, with id, display name, absolute cwd, cwd reachability, and `active="true"` when the Identity Agent is Rooted in that Project. With no registered Projects it still explains the Tool contract and reports an empty catalog. The block is data/dynamic rather than editable text; `ToolPromptBlockRegistry` and the standard `tool:<name>` owner gate keep prompt presence aligned with provider visibility.

## Interfaces

- `register_project_tool(registry, projects, get_renderer, list_project_skills, file_state, prompt_blocks=None)` registers the Tool and, when supplied, its dynamic prompt block.
- `make_project_handler(projects, get_renderer, list_project_skills, file_state)` exposes the bound handler for focused tests.
- `SystemPromptManager.render_project_files(...)` and `render_project_skills(...)` are the shared presentation seam; the Tool must not duplicate Project auto-load or Skill formatting.

## Constraints & Gotchas

- The Project catalog is live at every System Prompt build; the Tool result is live at every call. Neither is Session-pinned, and there is no `visited_projects` metadata. The latest successful Project Tool Result in Session history, not a second metadata field, is the durable Skill-routing carrier.
- A registered Project whose cwd is currently missing remains visible with `available="false"` so the model can explain why it cannot load; invoking it returns `project_unavailable`.
- Display names and catalog attributes are XML-escaped/collapsed before entering the System Prompt. Project ids remain the stable invocation key; never route by display name or cwd equality.
