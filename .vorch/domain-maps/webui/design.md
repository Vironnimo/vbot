# WebUI Visual State

Task-specific companion to `../webui.md`: the implemented appearance and interaction patterns, with pointers to their owners.

## How to use this description

This is a description of the current implementation, not a design specification, quality endorsement, or template for new work. Existing colors, typography, dimensions, layouts, action placement, and information hierarchy are all open to revision within the user's requested scope. Changing them does not require an exception to this document. A previous implementation becoming documented does not make it an approved design constraint.

Read the relevant sections when the task needs visual context, such as changing shared styling, understanding an existing layout, or assessing a redesign. This companion is not an additional prerequisite for every WebUI task. Source code remains the authority for implemented behavior. Screenshots and browser checks establish what the current interface actually looks like; a passing component test does not establish design quality.

The parent map separately records engineering contracts such as server-owned state, accessibility, draft preservation, and shared-component integration. Those contracts do not prescribe a layout. Explicit user requirements remain distinguishable from implementation observations; record a new visual constraint only when the user establishes it as one. Update the affected observations after a change without turning the new arrangement into a requirement for other views.

Paths beginning `webui/`, `desktop/`, or `resources/` are repository-relative. Other map links are relative to this file. Short component/lib names identify their owners under `webui/src/`; `source-map.md` supplies the detailed source routing. The source and test references below are starting points, not a claim that every current screen has been visually approved.

## Shared appearance

### Tokens and assets

`webui/src/styles/app.css` owns the implemented CSS tokens, bundled font declarations, primitive styles, and app-shell geometry. There is no separate machine-consumed token catalog in this map. `webui/src/styles/settings.css`, `projects.css`, and `cron.css`, plus component-local styles, specialize those foundations.

The current palette is warm brown with orange accents. The main surface roles are:

| Role | Current implementation |
|---|---|
| Page background | `--bg`, `#221A12` |
| Secondary navigation | `--secondary-surface`, `#271E15` |
| Surface steps | `--surface`, `--surface-2`, `--surface-3`: `#2B2217`, `#33291D`, `#3D3124` |
| Structural and control borders | `--border`, `--border-2`: `#4A3928`, `#5D4A35` |
| Text levels | `--text-hi`, `--text-med`, `--text-lo`: `#EEE7DC`, `#9A8C7E`, `#5E4C38` |
| Interaction accent | `--accent`, `#E8870A`, with named opacity steps in the same stylesheet |
| Status colors | Green for success, amber for running/warning, red for failure/destructive actions; blue also identifies unread Chat results |

Semantic aliases allow controls to be restyled independently: `--field-surface` currently uses `surface-2`; `--composer-surface`, `--prompt-content-surface`, and `--preview-surface` use `surface`; `--prompt-header-surface` uses `surface-2`; `--terminal-surface` is `#0E0D0B`. Changing a semantic role has a narrower effect than changing the underlying shared surface token. Startup background continuity across the HTML entrypoint, CSS, and Desktop is an engineering concern recorded in the parent map.

The metallic `V_` assets live under `webui/public/brand/`. `vbot-mark-transparent.png` is used with the app name; `vbot-icon.png` supplies the square treatment. Browser derivatives live in `webui/public/`, and Desktop icons in `desktop/`. `AppShell.svelte` owns the in-app presentation. These are existing assets rather than instructions to redraw the mark from text.

### Typography, spacing, and depth

The bundled typefaces are IBM Plex Sans (`--font-ui`) and IBM Plex Mono (`--font-mono`). Sans carries prose and ordinary navigation; Mono appears in technical names, values, timestamps, and small section labels. The current `--fs-*` scale runs from 10.5px (`mono-xs`) to 22px (`display`); body roles are 12.5px, 13.5px, and 14px. Full values are in `app.css`, rather than duplicated as a second token schema here.

Typography is not uniform across every control. The base `.form-field__label` uses muted uppercase Mono, while scoped rules for Agents, Projects, and Settings replace it with readable sentence-case Sans. `FormField.svelte` owns the associated label/help/error structure, not a universal visual label style. Inspect the complete CSS cascade before changing one of these surfaces. The existing variation is not a recommendation for new forms.

Named spacing anchors are 4/8/14/20/28px; component styles also use intermediate values. Rectangular corner tokens are 3/6/10px. Toggle knobs and some status/metadata markers use circular or pill treatments. Tonal surface steps and warm borders provide most depth; floating controls use elevation and explicit stacking layers. Floating panels are portaled above dialogs so a picker inside a dialog stays usable.

### Geometry and responsive behavior

The Main menu is currently 210px wide, with a 64px compact desktop mode. Secondary panes use the shared 216px token. `AppShell.svelte`, `.secondary-pane`, and `.secondary-list*` own the common geometry; feature components own their row contents and responsive alternatives.

Many editors use centered inner measures: `--content-max-narrow` is 920px and `--content-max-wide` is 1180px. Chat has a user-selectable comfortable/wide/full measure; the first two use 780px and 1100px. These are existing choices, not a ban on full-width work areas: Terminals and wide data surfaces have different needs and layouts.

Common breakpoints are mobile at <=640px, tablet through 960px, desktop from 961px, and wide at 1280px. Some components respond to their own available width instead: split Chat areas stack below 760px of workspace width, and the AudioPlayer rearranges its controls at narrower component widths. Actual media/container queries and scroll owners live with the component. Mobile controls commonly grow to 40px touch targets.

## Shared controls and feedback

`source-map.md` records the shared primitive ownership, integration contracts, and guard tests. The table below describes their current presentation, without requiring a view to adopt a particular arrangement of them.

| Surface | Current presentation and owner |
|---|---|
| Buttons | `components/ui/Button.svelte` has primary, secondary, tertiary, and danger variants plus an icon footprint. Primary uses a subtle orange tint; tertiary is quiet; secondary has a border. Danger icons are red at rest, while text danger buttons start neutral. |
| Copy | `CopyButton.svelte` wraps a compact icon Button, changes to a check briefly after success, and silently tolerates blocked clipboard access. Placement and hover reveal belong to its caller. |
| Fields | `TextField.svelte` renders ordinary single-line inputs or a read-only value box. `TextArea.svelte` has bordered/inset and code treatments. Chat and queued-message composers retain specialized textareas. |
| Toggles | `Toggle.svelte` renders the shared switch with an orange on-state and two size variants. The caller owns the value and mutation. |
| Pickers | `components/Dropdown.svelte` and `SearchableDropdown.svelte` render fixed, portaled panels; the latter includes search. Their positioning helper flips and bounds the panel to fit the viewport. Model options come from `lib/modelSelection.js`. |
| Dialogs | `Modal.svelte` supplies overlay, visible title, close control, and body/footer slots. `ConfirmDialog.svelte` adds the explicit consequence and confirm/cancel choice. Form geometry belongs to each caller. Modal choice lists currently use full-width bordered rows/cards. |
| Tabs | `TabList.svelte` has underline and segmented treatments, with a compact density option. Period filters and Chat Agent navigation are separate controls. |
| State and metadata | `StatusChip.svelte` represents status; `Badge.svelte` represents kind/origin/version/scope. Most are text pills; Session drawer metadata uses compact accessible icon badges. |
| Inline feedback | `Banner.svelte` displays loading, errors, warnings, and notices using a surface with a semantic left stripe. `EmptyState.svelte` displays known absence with a quiet dashed treatment. |
| Toasts | `ToastStack.svelte` displays a bottom-right stack with colored left stripes. Error toasts persist by default; other variants usually expire. Caller overrides and lifecycle belong to `lib/toastState.js`. |
| Tooltips and hints | `lib/tooltip.js` owns Quick tooltips and interactive Structured hover cards. `InfoHint.svelte` is the small question-mark explanation control. These use the shared floating layer, not native browser title bubbles. |
| Audio | `AudioPlayer.svelte` provides a bounded playback surface with Play/Pause, progress/time, speed, mute, volume, and download. Secondary controls wrap as the component narrows; inline failure leaves playback retry reachable. |

Quick tooltips currently appear after 150ms and expose short labels or full truncated values. Structured hover cards can include Copy or other actions and remain open during pointer travel. They also support focus and touch; presentation-only labels do not replace accessible names. The hover-card and dropdown integration details, including viewport bounds and cleanup, are in `source-map.md`.

Current configuration editors commonly combine debounced autosave and an explicit Save action. Entity creation, destructive confirmations, and write-only secrets use explicit operations. This is existing behavior, not a demand to add a redundant Save button to every new form. Draft preservation and navigation flushing are behavioral contracts: their owner and failure handling are in `settings.md` and `app-shell.md`.

## Current view patterns

### Navigation and action placement

The Main menu groups Work, Configure, and Insights. Its expanded headings are muted small Mono labels; the compact mode hides text and separates later groups with small hairlines. A compact toggle changes desktop presentation, and mobile uses a scrolling navigation row. The optional Live voice control sits above the connection/microphone indicators. `AppShell.svelte`, `LiveVoice.svelte`, and `app.css` own these surfaces; lifecycle details are in `app-shell.md` and `../model_tasks/live.md`.

Standalone views use `.view-frame`, `.view-header*`, and `.view-toolbar*` in different combinations. Existing shared action-group styles align controls right, and `.view-toolbar--split` separates groups with space-between. Settings list heads likewise currently place counts and actions at opposite ends. These styles explain the existing appearance; they are not prescriptions for a new or redesigned view.

The user's expressed direction is to avoid page-level action clusters at the top right and unnecessary permanently labelled buttons. The Skills change applies that direction with a plus next to search and compact row actions, and removes its Refresh button completely. Other existing placements have not thereby been approved or scheduled for an automatic redesign. A Refresh control's presence elsewhere is not evidence that a new view needs one; inspect its actual loading and invalidation behavior.

### Skills

`components/skills/SkillsView.svelte` currently pairs a collection sidebar with search, a status filter, and at most 50 results per page. Library, Agents, and Projects have emphasized headings with an adjacent separator. Skill locations is a normal navigation entry after Shared skills; there is no sidebar action footer. The main header has no actions. A plus immediately before search opens Skill locations and focuses the new-directory field, preserving an existing draft.

Rows show the Skill name and labelled owner/source, Share/Delete icons where applicable, and a master enable switch. Descriptions use Quick tooltips, while exceptions can add a status marker. Selection opens an adjacent reading panel with management actions outside its content scroll; on tablet/mobile the detail replaces the list and Back restores focus. Instructions and original text remain separate representations. Creation, editing, and sharing use explicit dialogs; folder setup remains mounted after first visit. The view has automatic inventory reconciliation and no manual Refresh control.

Behavior, ownership, and source-package policy are in `settings.md` -> Skills manager and `../skills.md`. Rendered coverage: `components/skills/__tests__/skillsView.test.js`.

### Settings, Agents, Projects, and Schedules

Settings currently has five category destinations: General, Sessions & Memory, Tools & Media, Connections, and System. General directly exposes Appearance and Region/setup; the other categories show feature cards. Search and deep links select the existing editor, whose mounted state preserves drafts. The tablet/mobile picker replaces the category sidebar. This implementation is described in `settings.md`; `SettingsView.svelte` and `styles/settings.css` own the current arrangement.

Agents and Projects use selection panes, a management header, content tabs, and a save footer outside the topic scroll. Agent tabs are Overview, Behavior, Tools & Skills, and Details; Project tabs are Overview, Team, Context, and Tools & Skills. Agents also has a Shared defaults destination that retains the selected Agent. Shared defaults puts Model/Thinking and Compaction in one scrolling page. Compaction exposes Classic/With tail before its conditional fields. These arrangements are current, revisable layouts; inheritance, policy, and persistence meanings stay with `settings.md` and `projects.md`.

The Tool Access editor presents All/Choose/None, search, family cards with switches, and wrapping name chips. Readiness and explanatory detail appear through hints; automatic Tools have a dashed treatment. Identity/Project Sub-Agent selectors are separate groups. `ToolAccessEditor.svelte`, `AgentEditor.svelte`, and the contracts in `settings.md` own the distinction between what is displayed and what changing it means.

Schedules currently uses a master-detail view. Each list row shows name, status, and next Run; details put operational summary above Task, Timing, and Session sections, with execution/technical detail in disclosures and Save below. `CronView.svelte`, `styles/cron.css`, and the Cron entry in `source-map.md` own the layout; schedule semantics remain in `../automation.md`.

### System Prompt

`SystemPromptView.svelte` has persistent Agent context and Prompt/Tools/Edit blocks tabs above one scrolling work area. Prompt displays readable Markdown or exact Original text; Tools provides a searchable list and one full definition; Edit blocks offers compact rows with independent inclusion switches and Edit/Inspect disclosures. A Refresh action currently sits with the Agent context. That is existing behavior, not a general header pattern.

Inherited and disabled instructions remain readable. The component-local `.sp-block` styles use a dashed border for disabled state rather than whole-card opacity; inheritance is labelled. Scope changes, draft saving, preview invalidation, reset, and exact text/definition semantics are documented in `settings.md`. `SystemPromptView.test.js` and `uiPrimitives.guard.test.js` cover the relevant behavior and surface distinctions.

### Chat and content inspection

Chat's current layout supports up to two retained areas, each containing Chat or an HTML preview. The split control sits with Sessions/New Session; HTML links can open the preview in the other area. Closing or switching areas retains drafts and scroll state. The preview has its own compact file toolbar, bounded iframe, and live-reload control. Source owners and interaction contracts are in `chat.md`.

User messages use right-aligned cards with an accent rail, capped to 75% of the reading measure. Assistant prose flows without a card. The composer overlays the bottom of the timeline on its own semantic surface; the timeline reserves corresponding space. A floating Jump to latest control restores follow mode. The Sessions drawer uses compact flat rows with a selection rail, an unread dot, metadata icons, and a contextual action menu. Chat Agent selection uses an underline independently of its running/unread/idle status dot.

Session information opens from a small floating right-edge rail. It includes parent navigation where resolvable, stats, and grouped background work. Sub-Agent rows can navigate and cancel; Bash rows expose process cancellation; Reflection rows navigate without a cancel control. These differences express server-owned lifecycles, not interchangeable decorative row variants. The owners are `ChatActivityPanel.svelte`, `chatState.js`, and `chat.md`.

Tool calls appear as compact dot/text lines with semantic summary facts and actions. Expanding reveals Args/Stdout/Stderr/Result on a thin rail, with complete values and Copy access. Value shortening, retained exact text, status, and cancellation behavior belong to the timeline helpers and `chat.md`. Tool media previews open the existing image lightbox and retain an unavailable placeholder on load failure. Thinking is a separate disclosure; compact Working mode groups contiguous internal work while visible Assistant prose remains outside it. Completed Compaction dividers disclose exact checkpoint text; running dividers remain status rows.

Slash-command outputs have their own chat-local toast/transient-card presentation, separate from app-wide ToastStack. Markdown code blocks include Copy; error messages use a restrained red treatment. The same `MarkdownContent.svelte` presentation is reused by read-only instruction and Swarm content. Detailed timeline, composer, attachment, command, and speech behavior is in `chat.md`, rather than duplicated as a second implementation specification here.

### Terminals

`TerminalsView.svelte` uses a horizontal group bar and a grid of native terminal tiles. Group/terminal creation is represented by compact plus controls, including a narrow append tile after the last terminal. A shared dialog edits command, working directory, name/group, and recent launch setup. Tile bars contain identity and local controls; dictation pastes into the native terminal. Finished entries stay inspectable as read-only history. Desktop/mobile changes affect available presentation, not the meaning of a Terminal Session.

The distinction between the Agent's compact observation and the operator's available screen space is a separate product requirement. The current FitAddon-driven implementation can resize the live PTY when the UI changes size; this is a documented limitation, not a desirable behavior inferred from the design. `../tools/terminal.md` records that requirement, implementation limits, PTY fidelity, lifetime, input, and verification contracts.

### Statistics, Logs, and server feedback

Statistics combines shared content tabs and time-range controls with summary metrics, charts, and detailed tables. Overview has four leading metrics; Usage separates measured and estimated data; technical records use disclosures. Labels use Sans and numeric values use tabular Mono. Responsive rules stack panels and preserve local table scrolling. Owners: `StatisticsView.svelte`, `components/statistics/`, `lib/statisticsView.js`, and their tests.

Logs uses compact Mono rows with timestamp, level, logger, and message, plus semantic left borders. File/level/order/search controls sit above the list. It loads and follows data automatically and exposes Retry on failure, without a generic header Refresh control. `LogsView.svelte`, `logsView.js`, and `../logs.md` own this surface; live-log behavior is separate from ordinary app invalidation.

The AppShell server-availability popup is a global floating surface with connection state, details, and Retry; Desktop can also offer Switch server. Navigation stays available while server-dependent content is inert. A short grace period avoids transient flashes, and recovery briefly displays success. Timing, suppression of duplicate errors, reconnect, and Desktop capability behavior belong to `app-shell.md` and `settings.md`.

### Swarms

The bundled Swarm page currently uses a Secondary pane for Swarms and grouped Runs. The Swarm editor has Overview/System Prompt/Tools & Skills/Communication topics; Run setup selects Swarm, working directory, and goal. Saved editors autosave and retain an explicit Save footer. Operational views have their own refresh behavior.

Run inspection shows the user prompt and participant views. The Board uses compact author/time headers, identity-colored initial avatars, Markdown prose, and a modal for writing a post; Activity provides context usage and participant continuation. Results are shared on the Board rather than a separate Results tab. This is Extension-owned presentation in `resources/extensions/swarm/ui/`, not a layout imposed by the host WebUI. Host bridge contracts are in `../extensions.md`; feature ownership is in `../extensions/swarm.md`.
