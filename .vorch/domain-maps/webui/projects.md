# WebUI Projects

Read this reference only for the WebUI Projects management view, Project discovery/editing, scans, Team rows, or Project-level overrides. Enter the backend through `projects.md`; use `projects/configuration.md` for mutations and overrides, `projects/scanning.md` for discovery/findings, and `projects/resolution.md` for effective runtime values.

## Ownership and selection

`ProjectsView.svelte` owns the master-detail management surface; `createProjectsController()` in `projectsView.js` owns its loading, selected Project, form, scan, mutation, and removal state. The Project selected here is management state and is intentionally independent of Chat's selected Project context.

The view submits user intent through the Project wrappers in `api.js`. It can normalize form inputs and present scan results, but it does not discover files, resolve Agent inheritance, or decide backend conflict policy.

## Adding and editing Projects

- Add-project input is a user-selected `cwd` plus optional source format and import choices. Detection results are advisory presentation from the server; the browser must not probe the local filesystem itself.
- Supported source formats and detection findings are normalized by `projectsView.js` for rendering. The server remains authoritative for whether a path can be registered and what was imported.
- Manage-project payloads include only supported editable fields. Unchanged values do not need to be rewritten, and UI-only draft state must not leak into backend configuration.
- Re-pointing a Project changes its configured root through the dedicated update path, then refreshes Project and scan state. It does not move files on disk.
- Remove uses a confirmation surface and the backend's rooted-Agent handling choice. Removing a Project and deciding what happens to Project-rooted Agents are one explicit operation, not an implicit client-side cascade.

## Scan and Team projection

- Project scan/rescan is server work. The UI renders normalized findings, discovered Skills/Agents, source-format information, and actions returned by the Project domain.
- Team rows project effective settings. Model, temperature, and thinking effort preserve provenance (`override`, Agent value, Project default, global default, or null); compaction policy is currently a direct persisted override and has no `effective_config` provenance object.
- Editing a Team field either sets an explicit override or clears it to restore inheritance. The frontend must not replace inherited values with copied explicit values merely because that is what the row currently displays.
- Temperature, thinking-effort, and compaction-policy drafts preserve the distinction between no override and an explicit value. Normalization happens at the payload boundary.
- Tool and Skill controls use the shared catalogs but apply Project policy. Catalog membership, allowed lists, and effective runtime availability are related projections, not interchangeable state.

## Refresh and error behavior

- List refresh preserves the selected Project when it still exists and selects a valid fallback when it does not. Detail and scan responses are discarded if they belong to a no-longer-selected Project.
- Mutations refresh the authoritative Project detail or scan before clearing busy state. Errors stay attached to the operation that failed and do not silently discard the current management draft.
- `resource_changed` can request a Projects refresh through the app shell. The Projects controller decides when refreshed state can safely replace an active form or modal.

## Source and tests

- Controller, normalization, payload builders, Team provenance: `webui/src/lib/projectsView.js`
- Management surface: `webui/src/components/ProjectsView.svelte` and Project components under `webui/src/components/`
- Transport wrappers: Project methods in `webui/src/lib/api.js`
- Focused coverage: `webui/src/lib/__tests__/projectsView.test.js`, `projectsView.test.controller.test.js`, and Project component tests under `webui/src/components/__tests__/`
