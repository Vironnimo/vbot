# WebUI Autosave

Read before adding or changing editable configuration, an existing-record editor, save controls, or navigation that can replace an editor, including Extension-owned pages. This is the shared UI persistence contract; backend validation and storage remain with the owning domain. Source and test paths below are repository-relative; map pointers resolve under `.vorch/domain-maps/`.

## Scope and ownership

- Use the shared autosave mechanism for editable settings and existing configuration records. Extend the existing editor/controller for payload validation and reconciliation; timing, serialization, pending-state tracking, and transition flushing belong to `webui/src/lib/autosave.js`.
- Creation, credentials, destructive actions, and operational commands retain their explicit submission semantics. A text field alone does not imply autosave: sending a Chat message or starting a Run is an action, not configuration persistence.
- Keep a small tertiary manual Save action after the fields, right-aligned inside the content scrollport. It uses the same tracked save operation and gives reassurance when already saved; it does not require a sticky or permanently reserved footer.

## Integrating an editor

- Prefer `createDebouncedAutosave({ getSnapshot, hasChanges, save })`. Schedule from the reactive draft effect with `scheduleRun()` and cancel through `cancelPendingTimer()` on cleanup. The helper reads the full snapshot in that effect so every edit restarts the timer; observing only a dirty boolean misses later keystrokes.
- Collection editors or immediate discrete controls can use `createAutosaveParticipant`; collection timers use the shared `scheduleAutosave`. Do not introduce a separate timeout, save queue, or navigation-flush implementation per tab. Checkboxes and pickers use the same tracked operation without waiting for text completion.
- Snapshots must include all edited values and the target identity when an editor can switch records. `hasChanges` compares the draft with its persisted baseline. `save(reason)` validates and submits a captured draft, returning `true` only on success and `false` on validation or persistence failure.
- Register the participant with `useAutosaveContext().register(...)`, unregister on destruction, and route editor-replacing selection/navigation through `requestTransition(...)`. Manual Save uses the same participant; use `runSave('manual', { force: true })` when the save callback also owns the already-saved acknowledgement.
- Extension pages register with `extensionPageClient.registerAutosave(participant)` and publish changed pending state with `notifyAutosave()`. The host joins the App coordinator; the Extension still owns its domain draft and payload. See `resources/extensions/swarm/ui/ProfileEditor.svelte` for this integration.

## Input continuity and failure behavior

- The common idle interval is 800 ms from the latest edit. Focused number/decimal controls wait for blur; IME composition waits for completion and the idle interval. Explicit Save and transition flushes do not wait for the timer. Shared `TextField` and `TextArea` report composition through `autosaveInput`; custom native editing controls need equivalent integration.
- Keep controls mounted and editable during automatic saves. Reconcile the response into the persisted baseline without replacing newer draft values, stealing focus, or remounting the editor through a loading state. Normalize numbers at the payload boundary, preserving incomplete input while it is edited.
- The participant serializes writes. Transition flushing waits for in-flight work and saves remaining newer snapshots before leaving. Failed unchanged snapshots do not retry in a background loop; retain the draft and visible error. App-level navigation failure and Retry / Discard behavior belong to `webui/app-shell.md`.
- The helper does not replace the editor's reconciliation or backend validation. A successful response for an older draft must not mark a newer draft saved.

## Evidence and regression coverage

- Shared scheduling, serialization, failure, and flush behavior: `webui/src/lib/autosave.js`, `webui/src/lib/__tests__/autosave.test.js`, and `webui/src/lib/__tests__/autosave.input.test.js`.
- Rendered input continuity and late-response regressions: `webui/src/components/__tests__/SettingsView.test.subagents-and-voice.test.js` and `webui/src/components/__tests__/SettingsExtensionsPanel.test.js`.
- Target identity and Project overrides: `webui/src/components/__tests__/ProjectsView.test.js`; navigation and browser-close protection: `webui/src/__tests__/App.test.navigation-history.test.js`; isolated pages: `webui/src/lib/__tests__/extensionPageClient.test.js` and `webui/src/components/__tests__/SwarmPage.test.js`.
- When changing an editor, cover the relevant gaps in its existing tests: repeated edits, focus/number completion, newer input during a slow save, failure recovery, and switching records or pages with pending changes. Helper tests alone do not catch an editor that remounts or overwrites its draft.
