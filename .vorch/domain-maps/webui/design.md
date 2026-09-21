# WebUI Visual State

Task-specific companion to `../webui.md`: the implemented appearance and interaction patterns, with pointers to their owners.

## How to use this description

This is a description of the current implementation, not a design specification, quality endorsement, or template for new work. Existing colors, typography, dimensions, layouts, action placement, and information hierarchy are all open to revision within the user's requested scope. Changing them does not require an exception to this document. A previous implementation becoming documented does not make it an approved design constraint.

Read the relevant sections when the task needs visual context, such as changing shared styling, understanding an existing layout, or assessing a redesign. This companion is not an additional prerequisite for every WebUI task. Source code remains the authority for implemented behavior. Screenshots and browser checks establish what the current interface actually looks like; a passing component test does not establish design quality.

The parent map separately records engineering contracts such as server-owned state, accessibility, draft preservation, and shared-component integration. Those contracts do not prescribe a layout. Explicit user requirements remain distinguishable from implementation observations; record a new visual constraint only when the user establishes it as one. Update the affected observations after a change without turning the new arrangement into a requirement for other views.

Paths beginning `webui/`, `desktop/`, or `resources/` are repository-relative. Other map links are relative to this file. Short component/lib names identify their owners under `webui/src/`; `source-map.md` supplies the detailed source routing. The source and test references below are starting points, not a claim that every current screen has been visually approved.

## Shared appearance

### Tokens and assets

`webui/src/styles/app.css` is the ordered stylesheet entrypoint; its private `app/foundation.css` owns the implemented CSS tokens and bundled font declarations, while adjacent `app/` files own primitive styles and app-shell geometry. There is no separate machine-consumed token catalog in this map. `webui/src/styles/settings.css`, `projects.css`, and `cron.css`, plus component-local styles, specialize those foundations.

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

The bundled typefaces are IBM Plex Sans (`--font-ui`) and IBM Plex Mono (`--font-mono`). Sans carries prose and ordinary navigation; Mono appears in technical names, values, timestamps, and small section labels. The current `--fs-*` scale runs from 10.5px (`mono-xs`) to 22px (`display`); body roles are 12.5px, 13.5px, and 14px. Full values are in `styles/app/foundation.css`, rather than duplicated as a second token schema here.

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
| Inline feedback | `Banner.svelte` displays loading, errors, warnings, and notices using a surface with a semantic left stripe. Its compact appearance shares the quiet composer surface and tertiary action sizing between Queue rows and Chat context notices. `EmptyState.svelte` displays known absence with a quiet dashed treatment. |
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

`components/skills/SkillsView.svelte` currently pairs a collection sidebar with search, a status filter, and at most 50 results per page. Library, Agents, and Projects have emphasized headings with an adjacent separator. Skill locations is a normal navigation entry after Shared skills; there is no sidebar action footer. The main header has no actions. A plus immediately before search opens Install a skill, with link/upload tabs and a global/private destination. Source checking previews one package or offers a candidate picker; replacing different files requires an explicit switch. Secondary actions open Skill locations (focusing its existing new-directory draft) or custom creation.

Rows show the Skill name and labelled owner/source, Share/Delete icons where applicable, and a master enable switch. Descriptions use Quick tooltips, while exceptions can add a status marker. Selection opens an adjacent reading panel with management actions outside its content scroll; on tablet/mobile the detail replaces the list and Back restores focus. Instructions and original text remain separate representations. Creation, editing, and sharing use explicit dialogs; folder setup remains mounted after first visit. The view has automatic inventory reconciliation and no manual Refresh control.

Behavior, ownership, and source-package policy are in `settings.md` -> Skills manager and `../skills.md`. Rendered coverage: `components/skills/__tests__/skillsView.test.js`.

### Settings, Agents, Projects, and Schedules

Settings currently opens General and offers seven pages: General, Providers, Voice, Memory, Tools, Integrations, and System. The sidebar contains pages, while related settings appear together under section headings in one scrollport. Session titles sit with display preferences; speech Model choices sit with Voice, embeddings with Recall, and web search/reading with the other Tool settings. Providers has a contextual Shared defaults link. Sections use spacing and dividers without enclosing feature cards; simple rows align labels left and controls right, stacking on mobile. The content measure is 860px; below 960px a page picker replaces the sidebar. Search results identify their owning page and jump to the matching section. SettingsView.svelte and styles/settings/pages.css own this arrangement; settings.md records behavior and ownership.

Agents and Projects pair selection panes with one continuous, 860px configuration page. Their title and fields scroll together without detail tabs. Headings, spacing and compact rows replace nested section cards. Agents shows Identity, Model (temperature/fallback disclosure), Context & Memory, Tools, Skills, Sub-Agent targets and an advanced Workspace/ID disclosure. Projects shows Repository, Agent defaults, compact, single-line Team members with descriptions on hover/focus and locally expandable settings, auto-load files and Tool/Skill ceilings. Repository management is a local disclosure and Rescan sits beside Team. A tertiary Save ends each page. Agents retains its separate Shared defaults destination and selected Agent. Contracts remain in `settings.md` and `projects.md`.

`ToolCatalogEditor.svelte` supplies the same flat family groups and compact switch rows to Agent policy and Project ceilings. Groups use two columns on desktop and one on mobile. Search matches names, descriptions and family labels. Automatic/unavailable/opt-in states are visible; full descriptions and actionable readiness details appear on hover/focus. Both surfaces offer Select all / Deselect all actions rather than policy-mode tabs. `ToolAccessEditor.svelte` supplies exact Agent-policy updates; the Project owner supplies fixed-list selection and reset. These remain different permission scopes despite shared presentation.

Schedules currently uses a master-detail view. Each list row shows name, status, and next Run; details put operational summary above Task, Timing, and Session sections, with execution/technical detail in disclosures and Save below. `CronView.svelte`, `styles/cron.css`, and the Cron entry in `source-map.md` own the layout; schedule semantics remain in `../automation.md`.

### System Prompt

`SystemPromptView.svelte` has persistent Agent context and Prompt/Tools/Edit blocks tabs above one scrolling work area. Prompt displays readable Markdown or exact Original text; Tools provides a searchable list and one full definition; Edit blocks offers compact rows with independent inclusion switches and Edit/Inspect disclosures. A Refresh action currently sits with the Agent context. That is existing behavior, not a general header pattern.

Inherited and disabled instructions remain readable. The component-local `.sp-block` styles use a dashed border for disabled state rather than whole-card opacity; inheritance is labelled. Scope changes, draft saving, preview invalidation, reset, and exact text/definition semantics are documented in `settings.md`. `SystemPromptView.test.js` and `uiPrimitives.guard.test.js` cover the relevant behavior and surface distinctions.

### Chat and content inspection

Queued messages appear as slim single-line rows above the composer, without a heading, status badge, or separate action row. Steer is a quiet text button; edit/remove use icons. Full text appears in a scrollable floating hover/focus card (`QueuedMessages.svelte`). The Queue, Sub-Agent return notice, Provider/Model setup notices, and duplicate-Session composer notice share `Banner`'s compact appearance. Actionable notices use one bold sentence and a quiet text action; the Sub-Agent notice has no explanatory second paragraph. These surfaces wrap to the available Chat-area width and retain larger touch targets.

Chat's current layout supports up to two retained areas, each containing Chat or an HTML preview. The split control sits with Sessions/New Session; HTML links can open the preview in the other area. Closing or switching areas retains drafts and scroll state. The preview has its own compact file toolbar, bounded iframe, and live-reload control. Source owners and interaction contracts are in `chat.md`.

The context ring shows usage through a passive quick tooltip on hover or keyboard focus.

User messages use right-aligned cards with an accent rail, capped to 75% of the reading measure. Assistant prose flows without a card. The composer overlays the bottom of the timeline on its own semantic surface; the timeline reserves corresponding space. A floating Jump to latest control restores follow mode. The collapsed Sessions bar and expanded drawer share surface, border, typography, button geometry, and hover styling, keeping their controls in place when toggled. Their buttons have subtly rounded hover surfaces, a uniform 3px inset from the container border, and 2px gaps. Both sit 6px from the top and left of the Chat surface (8px on mobile). The Sessions drawer uses a 290px desktop overlay, one shared controls/filter header, and compact flat rows with a selection rail, an unread dot, metadata icons, and a contextual action menu. Its All agents icon toggle is directly accessible; Channels are opt-in through the filter menu. Mobile keeps wider touch targets. Chat Agent selection uses an underline independently of its running/unread/idle status dot.

Session information opens from a compact floating right-edge toggle. Its scrollable panel places parent navigation and stats above Subagent Runs (with task previews), Reflections, and a Bash disclosure. Each category keeps running work first; Bash starts collapsed when other categories are present and retains a visible running count. Whitespace separates groups without per-row rules; running work uses a small amber dot. Sub-Agent rows can navigate and cancel; Bash rows expose process cancellation; Reflection rows navigate without a cancel control. These differences express server-owned lifecycles, not interchangeable decorative row variants. The owners are `ChatActivityPanel.svelte`, `chatState.js`, and `chat.md`.

Tool calls appear as compact dot/text lines with semantic summary facts and actions. Expanding reveals Args/Stdout/Stderr/Result on a thin rail, with complete values and Copy access. Value shortening, retained exact text, status, and cancellation behavior belong to the timeline helpers and `chat.md`. Tool media previews open the existing image lightbox and retain an unavailable placeholder on load failure. Thinking is a separate disclosure; compact Working mode groups contiguous internal work while visible Assistant prose remains outside it. Completed Compaction dividers disclose exact checkpoint text; running dividers remain status rows.

Slash-command outputs have their own chat-local toast/transient-card presentation, separate from app-wide ToastStack. Markdown code blocks include Copy; error messages use a restrained red treatment. The same `MarkdownContent.svelte` presentation is reused by read-only instruction and Swarm content. Detailed timeline, composer, attachment, command, and speech behavior is in `chat.md`, rather than duplicated as a second implementation specification here.

### Terminals

`TerminalsView.svelte` uses a horizontal group bar and a grid of native terminal tiles. Group/terminal creation is represented by compact plus controls, including a narrow append tile after the last terminal. A shared dialog edits command, working directory, name/group, and recent launch setup. Tile bars contain identity and local controls; dictation pastes into the native terminal. Finished entries stay inspectable as read-only history. Desktop/mobile changes affect available presentation, not the meaning of a Terminal Session.

The distinction between the Agent's compact observation and the operator's available screen space is a separate product requirement. The current FitAddon-driven implementation can resize the live PTY when the UI changes size; this is a documented limitation, not a desirable behavior inferred from the design. `../tools/terminal.md` records that requirement, implementation limits, PTY fidelity, lifetime, input, and verification contracts.

### Statistics, Logs, and server feedback

Statistics combines shared content tabs and time-range controls with summary metrics, charts, and detailed tables. Overview leads with measured tokens, Provider-reported cost, estimated API value and cache hit rate, followed by a shared token trend, coverage diagnostics, leading Models and a Compaction summary. Usage & costs keeps measured/estimated tokens and pricing evidence separate, with Model/day/Session cost breakdowns and expandable recent calls. Compactions emphasizes remaining Context, reduction, recurrence and recent before/after records; older records with missing evidence show dashes. Labels use Sans and numeric values use tabular Mono. Responsive rules stack panels and preserve local table scrolling. Owners: `StatisticsView.svelte`, `components/statistics/`, `lib/statisticsView.js`, and their tests.

Logs uses compact Mono rows with timestamp, level, logger, and message, plus semantic left borders. Row-local copy-button sizing overrides the shared minimum height so the hidden control does not stretch desktop rows. File/level/order/search controls sit above the list. It loads and follows data automatically and exposes Retry on failure, without a generic header Refresh control. `LogsView.svelte`, `logsView.js`, and `../logs.md` own this surface; live-log behavior is separate from ordinary app invalidation.

The AppShell server-availability popup is a global floating surface with connection state, details, and Retry; Desktop can also offer Switch server. Navigation stays available while server-dependent content is inert. A short grace period avoids transient flashes, and recovery briefly displays success. Timing, suppression of duplicate errors, reconnect, and Desktop capability behavior belong to `app-shell.md` and `settings.md`.

### Debug

The Debug inspector pairs a filterable trace list with a full-height reading pane. List entries give the Model prominence and separate Provider/method and time/duration; code status is colored without claiming stream success. Request opens first, with Response and Metadata beside it. Endpoint, timestamp, status, duration and capture errors remain above the tabs; headers are disclosed separately. JSON has lazy expandable fields and readable multiline strings, plus Formatted JSON and exact Raw alternatives. Body search, line wrapping, raw Copy and complete-trace Copy/download sit near their content. Capture/storage and Model Probe stay in secondary disclosures; narrow screens show either list or detail with Back and focus restoration. On narrow screens the selected detail also hides the page introduction and storage toolbar to preserve reading space. Owners and raw-data contracts: `../debug.md`.

### Swarms

The bundled Swarm page currently uses a Secondary pane for Swarms and grouped Runs. The Swarm editor has Overview/System Prompt/Tools & Skills/Communication topics; Run setup selects Swarm, working directory, and goal. Saved editors autosave and retain a small in-flow manual Save action. Operational views have their own refresh behavior.

Run inspection shows the snapshot Swarm name and state. The Board contains the collapsible user request, working directory, and compact discussion-specific participants with status dots and hover details. Posts use subtle stable author tints, compact author/time headers, identity-colored initial avatars, Markdown prose, and a modal for writing a post; Activity provides context usage and participant continuation. Results are shared on the Board rather than a separate Results tab. This is Extension-owned presentation in `resources/extensions/swarm/ui/`, not a layout imposed by the host WebUI. Host bridge contracts are in `../extensions.md`; feature ownership is in `../extensions/swarm.md`.
