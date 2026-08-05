---
version: alpha
name: vBot — Toasted
description: >
  Warm charcoal-brown dark UI for a local-first AI agent harness.
  Dense, technical, and deliberate — a control room that runs hot.
colors:
  bg:          "#221A12"
  secondary-surface: "#271E15"
  surface:     "#2B2217"
  surface-2:   "#33291D"
  surface-3:   "#3D3124"
  border:      "#4A3928"
  border-2:    "#5D4A35"
  text-hi:     "#EEE7DC"
  text-med:    "#9A8C7E"
  text-lo:     "#5E4C38"
  accent:      "#E8870A"
  green:       "#4ADE80"
  amber:       "#F59E0B"
  blue:        "#60A5FA"
  red:         "#FC8181"
surfaceRoles:
  field:       "{colors.surface-2}"
  composer:    "{colors.surface}"
  promptHeader: "{colors.surface-2}"
  promptContent: "{colors.surface}"
  preview:     "{colors.surface}"
typography:
  display:
    fontFamily: IBM Plex Sans
    fontSize: 22px
    fontWeight: 600
    lineHeight: 1.2
    letterSpacing: -0.03em
  heading-lg:
    fontFamily: IBM Plex Sans
    fontSize: 20px
    fontWeight: 600
    lineHeight: 1.2
    letterSpacing: -0.02em
  heading-md:
    fontFamily: IBM Plex Sans
    fontSize: 18px
    fontWeight: 600
    lineHeight: 1.2
    letterSpacing: -0.02em
  heading-sm:
    fontFamily: IBM Plex Sans
    fontSize: 15px
    fontWeight: 600
    lineHeight: 1.3
    letterSpacing: -0.01em
  body-lg:
    fontFamily: IBM Plex Sans
    fontSize: 14px
    fontWeight: 400
    lineHeight: 1.65
  body-md:
    fontFamily: IBM Plex Sans
    fontSize: 13.5px
    fontWeight: 400
    lineHeight: 1.5
  body-sm:
    fontFamily: IBM Plex Sans
    fontSize: 12.5px
    fontWeight: 400
    lineHeight: 1.4
  label-md:
    fontFamily: IBM Plex Sans
    fontSize: 13px
    fontWeight: 500
    lineHeight: 1
  label-sm:
    fontFamily: IBM Plex Sans
    fontSize: 12px
    fontWeight: 600
    lineHeight: 1
    letterSpacing: 0.02em
  mono-body:
    fontFamily: IBM Plex Mono
    fontSize: 12px
    fontWeight: 400
    lineHeight: 1.5
  mono-sm:
    fontFamily: IBM Plex Mono
    fontSize: 11.5px
    fontWeight: 500
    lineHeight: 1
  mono-xs:
    fontFamily: IBM Plex Mono
    fontSize: 10.5px
    fontWeight: 500
    lineHeight: 1
    letterSpacing: 0.07em
rounded:
  sm: 3px
  md: 6px
  lg: 10px
spacing:
  xs: 4px
  sm: 8px
  md: 14px
  lg: 20px
  xl: 28px
  sidebar: 210px
  secondary-sidebar: 216px
components:
  button-primary:
    backgroundColor: "rgba(232,135,10,0.10)"
    borderColor: "rgba(232,135,10,0.22)"
    textColor: "{colors.accent}"
    rounded: "{rounded.md}"
    padding: 6px 14px
  button-primary-hover:
    backgroundColor: "rgba(232,135,10,0.18)"
    borderColor: "rgba(232,135,10,0.38)"
  button-secondary:
    backgroundColor: "transparent"
    borderColor: "{colors.border-2}"
    textColor: "{colors.text-med}"
    rounded: "{rounded.md}"
    padding: 6px 13px
  button-secondary-hover:
    borderColor: "{colors.accent}"
    textColor: "{colors.accent}"
    backgroundColor: "rgba(232,135,10,0.08)"
  button-danger-hover:
    borderColor: "{colors.red}"
    textColor: "{colors.red}"
    backgroundColor: "rgba(252,129,129,0.07)"
  input-default:
    backgroundColor: "{surfaceRoles.field}"
    borderColor: "{colors.border-2}"
    textColor: "{colors.text-hi}"
    typography: "{typography.mono-body}"
    rounded: "{rounded.md}"
    padding: 7px 11px
  input-default-focus:
    borderColor: "rgba(232,135,10,0.40)"
    boxShadow: "0 0 0 3px rgba(232,135,10,0.06)"
  input-composer:
    backgroundColor: "{surfaceRoles.composer}"
    borderColor: "{colors.border-2}"
    textColor: "{colors.text-hi}"
    typography: "{typography.body-lg}"
    rounded: "{rounded.lg}"
    padding: 11px 14px
  toggle-lg:
    width: 38px
    height: 22px
    backgroundColor: "{colors.surface-3}"
    borderColor: "{colors.border-2}"
  toggle-lg-on:
    backgroundColor: "{colors.accent}"
    borderColor: "{colors.accent}"
  toggle-sm:
    width: 30px
    height: 17px
    backgroundColor: "{colors.surface-3}"
    borderColor: "{colors.border-2}"
  toggle-sm-on:
    backgroundColor: "{colors.accent}"
    borderColor: "{colors.accent}"
  chip:
    rounded: 12px
    typography: "{typography.mono-sm}"
    padding: 3px 9px
  device-flow-inline:
    backgroundColor: "{colors.surface-2}"
    borderColor: "{colors.border-2}"
    rounded: "{rounded.lg}"
    padding: "{spacing.md}"
  device-flow-code:
    backgroundColor: "{colors.bg}"
    borderColor: "rgba(232,135,10,0.30)"
    textColor: "{colors.text-hi}"
    typography: "{typography.mono-body}"
    rounded: "{rounded.md}"
  tooltip:
    backgroundColor: "{colors.surface-3}"
    borderColor: "{colors.border-2}"
    textColor: "{colors.text-hi}"
    typography: "{typography.body-sm}"
    rounded: "{rounded.sm}"
    padding: 4px 9px
  nav-item:
    typography: "{typography.label-md}"
    rounded: "{rounded.md}"
    padding: 9px 10px
  nav-item-active:
    backgroundColor: "rgba(232,135,10,0.12)"
    textColor: "{colors.accent}"
---

# vBot — Toasted

> **Reference prototype:** `.vorch/design.html` is the canonical single-file HTML
> prototype of the current design. It contains all implemented components with
> exact markup, CSS, and interaction behaviour. Read it when you need details this
> file cannot express — specific DOM structure, component markup patterns, live
> spacing values, or anything visual that token names alone don't fully describe.
>
> The Svelte WebUI implementation lives in `webui/src/` and mirrors this
> prototype with backend-backed data where it already exists. Prototype content is
> illustrative only; controls without backend support remain placeholder-only or
> disabled.

## Overview

vBot should feel like a personal control room running late at night — warm, dense, precise. The palette descends from charcoal brown rather than neutral gray, giving every surface a scorched-wood temperature that makes the amber accent feel native rather than imposed.

The type system is split between **IBM Plex Sans** for all UI prose (navigation, labels, message bodies) and **IBM Plex Mono** for everything technical: model names, tool calls, timestamps, code, section labels. This split is load-bearing — mixing the fonts in the wrong place immediately breaks the voice. When something is a human phrase, use Sans; when it names a system artifact, use Mono.

Interaction is calm. Hover states are warm tint shifts, not bright flashes. The accent appears only where a human makes a decision: the active nav item, the send button, the accent border on user messages. It never decorates.

## Colors

The palette is organized around five layers of warm dark surface and three semantic status colors.

- **Bg (#221A12):** The page foundation — darkest, used behind content areas, the chat message stream, and the unbordered composer area. Warm near-black with a distinct brown cast.
- **Secondary-surface (#271E15):** The narrow intermediate layer used only by Secondary bars. It sits exactly between the main navigation and content backgrounds so the app descends from light on the left to dark on the right without adding elevation.
- **Surface (#2B2217):** Primary panel surface — Main menu, section cards, the interactive Chat composer, and read-only preview bodies. One step up from the Secondary bar.
- **Surface-2 (#33291D):** Elevated cards, dropdown backgrounds, user message bubbles. Used whenever a component needs to sit above its container.
- **Surface-3 (#3D3124):** Tertiary highlight layer — toggle tracks, code block backgrounds, hover surfaces for dropdowns.
- **Border (#4A3928) / Border-2 (#5D4A35):** Two border strengths. `border` for structural dividers (sidebar edge, section separators). `border-2` for interactive element outlines (inputs, buttons, dropdowns).
- **Text-hi (#EEE7DC):** Warm near-white. All primary content — message prose, headings, input values.
- **Text-med (#9A8C7E):** Secondary copy — assistant author label, tool event text, description rows, metadata.
- **Text-lo (#5E4C38):** Muted — timestamps, section labels, placeholder text, inactive nav items, toggle-list item names.
- **Accent (#E8870A):** The single interaction color — a saturated amber-orange. Active nav, focused borders, primary buttons, user message accent border. Reserve it; never use it for decoration.
- **Green (#4ADE80):** Success and running-healthy state (tool call done, server status dot).
- **Amber (#F59E0B):** In-progress / warning state (running tool call indicator, blinking dot animation).
- **Red (#FC8181):** Error state (failed tool call, destructive button hover).

Accent tints (fills, borders, hover states) come only from the tint ramp tokens `--accent-06 … --accent-40` in `webui/src/styles/app.css` — never hand-write `rgba(232, 135, 10, …)`. If a needed step is missing, extend the ramp there rather than inlining a literal. `--accent-dim` / `--accent-pale` are semantic aliases onto the ramp (08 / 12). The input focus glow is the single `--focus-ring` token.

Semantic surface roles prevent unrelated controls from being coupled merely because they currently share a palette step. `--field-surface` owns ordinary editable fields (`surface-2`); `--composer-surface` owns only the Chat composer (`surface`, preserving its established appearance); `--prompt-header-surface` and `--prompt-content-surface` enforce one System Prompt block hierarchy (`surface-2` title bar over `surface` content) for editable and generated blocks alike; `--preview-surface` owns read-only preview content (`surface`). Change a role rather than a raw palette token when one interaction category needs more or less contrast.

## Typography

Two typefaces carry the entire system. Never introduce a third.

**IBM Plex Sans** is the UI voice — it handles all prose, navigation labels, headings, button text, and conversational message bodies. Its optical warmth and subtle ink traps suit the brown-dark palette. Weights in use: 400 (body), 500 (nav items, labels), 600 (headings, button labels).

**IBM Plex Mono** is the technical voice — it handles everything that names a system artifact: model identifiers, tool function names and arguments, timestamps, code blocks, section title labels, API key inputs, token counts. Its presence signals "this is machine territory". Weights in use: 400 (code, values), 500 (labels, section caps).

Section headers in the Components tab and pane titles use Mono in all-caps with `letter-spacing: 0.07–0.08em` at 10–10.5px. This is the system's loudest use of mono — it signals structure at a glance.

- **display** — Agent detail heading. 22px / 600 / -0.03em letter spacing.
- **heading-lg** — Settings panel title. 20px / 600 / -0.02em.
- **heading-md** — Section heading (Components tab header). 18px / 600.
- **heading-sm** — Modal title. 15px / 600.
- **body-lg** — Conversation prose (both user and assistant messages). 14px / 400 / 1.65lh.
- **body-md** — Base UI default. 13.5px / 400. Nav items, settings rows.
- **body-sm** — Description text, panel subtitles. 12.5px / 400 / 1.4lh.
- **label-md** — Nav items, agent names, tab labels. 13px / 500.
- **label-sm** — Author names (YOU, ASSISTANT), button text. 12px / 600 / 0.02em.
- **mono-body** — Tool call names, code content, settings inputs, dropdown values. 12px / 400 / 1.5lh.
- **mono-sm** — Timestamps, chip text, toast labels, token badge. 11.5px / 500.
- **mono-xs** — Section labels (TOOLS, SKILLS, ARGS, RESULT), pane titles. 10.5px / 500 / 0.07em uppercase.

Every role's size is a CSS token (`--fs-<role>` in `webui/src/styles/app.css`, e.g. `--fs-body-sm`, `--fs-mono-xs`). New or touched styles use the token, never a px literal; legacy literals migrate opportunistically as their files are touched.

## Layout

The app shell is a fixed sidebar (210px) plus a fluid main content area. The sidebar never shrinks or grows. The main area holds views that each fill the full remaining width and height.

Sidebar navigation is grouped by usage cadence into Work / Configure / Insights, each group introduced by a mono-xs uppercase section label (`--text-lo`, no divider lines). Interactive inactive entries use `--text-med`, hover brightens them, and the active entry uses the accent treatment. On mobile (≤640px) the groups collapse to a single scrolling top-bar row, the labels hide, and a direct link or tab change scrolls the active entry into view.

Within views, every Secondary bar is 216px wide from tablet upward: the Agents, Projects, and Cron selection lists and the Settings index all use the shared `.secondary-pane` contract (`--secondary-sidebar-width`, `--secondary-surface`, a thin `border` divider, a muted mono-xs uppercase pane title, and contained scrolling). Their content differs by purpose — Settings alone has search and grouped navigation — but their width, depth, title hierarchy, and breakpoint do not drift. Agents, Projects, and Schedules additionally share `.secondary-list` / `.secondary-list__item`: list content, including empty states, sits inside a 12px inset on every edge, and the active surface mirrors the Settings index with a 2px left accent rail, `--accent-dim` fill, and `md` rounding only on the free right edge; feature styles own only the row's inner text and metadata layout. Master-detail content remains fluid beside it through tablet widths and stacks only on mobile (≤640px); the detail form may still collapse its own field layout at the tablet breakpoint.

Schedules uses the control-desk variant of that master-detail pattern. A master row contains exactly the schedule name, its `StatusChip`, and the server-projected next Run; it never previews the prompt, repeats the Agent target, or makes the raw Cron expression carry identity. Names may wrap to two lines and expose their complete value through the shared Quick tooltip. The selected detail starts with schedule kind, name, readable target context, StatusChip, and Enabled control, followed by one four-cell operational summary for next Run, cadence, last result, and target/Session. Full execution timestamps, Run id, failure count, schedule id, and Delete remain reachable through restrained Execution details and Technical details disclosures rather than competing with the primary workflow. The editor is three bounded `surface` cards: Task owns Name, Agent, and Prompt; Timing owns the Recurring/Once choice, readable preset, and exact Cron/Once value; Session owns the optional fixed Session. Ordinary fields use `FormField`, the prompt receives the large editing area, and Save stays in the sticky bottom footer. At tablet width the operational summary becomes two columns and the editor cards stack; at mobile the Secondary bar stacks above the detail and the summary becomes one column.

Terminals uses the same control-desk master-detail shell but treats the selected PTY as a native monitoring, history, and direct-control surface rather than a form. A primary New terminal action opens the shared `Modal`/`FormField` pattern for optional command, one-exact-argument-per-line input, and working directory; blanks clearly select the server user's default shell and home directory. The Secondary list contains live Terminal Sessions plus temporarily retained finished sessions and identifies each by command, readable Agent/Project owner or Manual origin, abbreviated Session where applicable, start time, and state. The detail header keeps state and stream health visible alongside PID, dimensions, and working directory. A newly created manual session starts with xterm focused in direct-control mode; a live Agent-owned session starts in observe mode, but a primary click anywhere in the terminal both takes control and focuses native xterm keyboard/paste input. The Take control toggle remains the visible state and explicit way to return to Observe; finished sessions expose only a read-only retained snapshot, and there is no secondary text composer. The bounded xterm.js stage is the dominant dark technical surface and reconstructs the server-retained scrollback plus current screen after mount, navigation, selection, or reconnect while preserving current-screen ANSI colors, cursor, TUI layout, mouse-wheel/trackpad scrollback, an always-visible draggable scrollbar, and a Jump to latest action while reading history; it owns its scrolling and resizes only a live underlying PTY to its fitted rows/columns without clipping the final row. Stop is a danger action behind `ConfirmDialog`, and empty, reconnect, start-error, and stream-error states use the shared `EmptyState`/`Banner`/`StatusChip` language. At the mobile breakpoint the terminal-session list stacks above the detail while the TUI keeps horizontal integrity inside its own viewport.

Standalone views use the shared `.view-frame` / `.view-header` / `.view-toolbar` chrome from `webui/src/styles/app.css`. The header is a plain identity block — mono-xs uppercase eyebrow, 20px title, `text-med` subtitle, and only compact status/actions at the trailing edge — while local navigation, filters, scope selection, and refresh or mutation actions belong in the bordered toolbar immediately below it. Toolbars choose the smallest fitting layout modifier: `--split` for one context/action row, `--stack` for dense filters plus metadata, and `--tabs` for a `TabList` plus its contextual actions. At ≤640px the view inset drops to 16px, header actions stack below the identity copy, and tab toolbars become a horizontally scrolling tab row followed by a separate action row; overflow stays inside the local control rather than widening the page. Statistics, Logs, Debug, and System Prompt are the reference consumers.

Settings is the one-document pattern, built on the shared left-to-right depth sequence: the Main menu sits on `--surface`, the Settings index on `--secondary-surface`, the document scroll area on `--bg`, and each section card lifts back to `--surface` (`--content-max-narrow` measure, `lg` radius, `border` outline) headed by a 15px/600 title and a `text-med` subtitle above a divider. The Secondary bar starts with the common muted mono-xs pane title, then a search input and grouped table of contents; index entries are 12.5px/500 `text-med` rows with a 2px left accent rail as the active marker (accent text, `--accent-dim` tint, radius only on the free right edge), hover brightens to `text-hi`, and non-matching entries dim to 35% during a search. Connect / Models / Behavior / System are the shared server-backed groups; Voice remains under Behavior because its transcription-audio controls are server-wide, while the Desktop accessor conditionally inserts Desktop app between Behavior and System for local Connection controls and reveals Wakeword rows inside Voice. On mobile the full index is replaced by the same search plus a compact current-section dropdown; the document remains the sole scrolling content region directly below it, and selecting an option uses the same scroll-to-section behavior as the desktop index.

Settings navigation uses one reading line at 32% of the document scrollport height. Scrollspy highlights the section crossing that line, index and mobile-picker activation place the chosen section there with smooth motion (instant when reduced motion is requested), and a trailing 68%-of-scrollport quiet zone lets the last section reach the same position instead of remaining pinned to the bottom edge. A normal switch to another main view preserves the section-relative reading anchor while a deliberate Settings deep link replaces it.

Dense detail inside settings rows (whole provider cards with their connections and accounts, per-target task-model options, extension config forms) collapses behind a disclosure: a 22px bordered chevron square (`▸`, `text-med`, rotates 90° when open, accent on hover) at the row's trailing edge, opening an indented sub-block carried by a 2px `border-2` left rail — the same visual language as expanded tool calls in chat. The collapsed sub stays in the DOM (`hidden` attribute) so settings search still matches its text.

In scrollable edit surfaces, the primary save action belongs at the bottom of the scroll region in a sticky footer, not in the panel header. This keeps the action visually near the edited fields and available after long scrolls.

### Content width / reading measure

No view stretches edge-to-edge on a wide monitor. Each scroll area fills its full width (scrollbar at the edge), but its **inner content column is capped and centered** so prose keeps an ergonomic line length. The caps are CSS custom properties in `:root` (`webui/src/styles/app.css`):

- `--chat-measure-comfortable: 780px` (~72ch, the default) · `--chat-measure-wide: 1100px` — the chat reading column.
- `--chat-measure` is the live chat measure. It defaults to `--chat-measure-comfortable` and is overridden by `[data-chat-width]` on `.chat-view`: `wide` → `--chat-measure-wide`, `full` → `none` (edge-to-edge, the old behavior). The active value is user-selectable in **Appearance** settings (`appearance.chat_width`, persisted) and applied app-wide.
- `--content-max-narrow: 920px` — settings/forms and label↔control rows. `--content-max-wide: 1180px` — detail/wide panels (Agents detail, System Prompt). These are fixed, not user-configurable.

Wide data tables and log rows (Logs, Debug) are **not** force-capped — they keep their own horizontal scroll rather than squishing.

### Breakpoints

CSS media queries cannot read custom properties, so the breakpoint set is a documented convention rather than a token. Use these thresholds; only deviate where a specific layout genuinely regresses:

- **mobile** ≤ 640px · **tablet** 641–960px · **desktop** ≥ 961px · **wide** ≥ 1280px.

On mobile (≤640px) the sidebar collapses to a top bar, two-pane splits stack, and interactive targets grow to ≥40px hit area.

### Chat messages

Chat messages use 28px horizontal padding as the column gutter. User message bubbles are right-aligned at 75% max-width *of the capped reading measure*. Assistant prose flows free (no bounding box) but within the same centered, capped measure. The composer, notice stack, and session banner align to the same center axis as the messages.

The Chat Activity surface is a deliberately quiet, free-floating control near the Chat's right edge, not another permanent navigation sidebar. Collapsed, it is a 26px rail inset by 8px, vertically centered, and approximately 30% of the Chat height; it carries only a centered left chevron, plus one small amber dot while background work is active. Opening grows the same surface leftward to a compact 276px panel and approximately 48% of Chat height, reverses the chevron, and reveals the single title “Background tasks” above a contained scrolling list. The list separates running work under a small Mono-caps “Active” heading from completed, failed, and cancelled work under “Finished”; either empty group disappears rather than leaving a placeholder. Every task occupies one borderless compact row with a semantic status symbol (animated amber working ring, green completion check, muted cancellation cross, or red failure warning), and its accessible label names the state. Active rows additionally end in a 24px red danger icon with an × glyph, accessible label, Quick tooltip, and disabled in-flight state; finished rows never show it. An automatic-delivery Sub-Agent row keeps the Agent address as the Child Session navigation control while its separate cancel icon stops the exact Child Run or queued item without navigating. A handed-off Bash row keeps a muted `$` marker plus the compact one-line command and no Session/navigation affordance; its separate cancel icon terminates the owned Process Session directly. Fine dividers separate rows and groups. At ≤640px the rail grows to a 32px touch target and the expanded panel stays within the Chat width. Escape or the chevron closes it, and reduced-motion users receive no growth, entrance, arrow, or working-ring animation.

When the active Session is above the live tail, a 36px circular secondary `Button` floats centered over the bottom of the Chat timeline, immediately above the footer/composer stack. It uses a downward arrow, the shared Quick tooltip, `surface-2` fill, `border-2` outline, and the shared `--floating-elevation`; activating it jumps to the latest content, resumes follow mode, and hides the control. The mobile target grows to 40px.

Completed Compaction dividers preserve the same muted mono divider at rest but are native disclosures: a small low-opacity chevron rotates when open, hover/focus brightens the existing label, and keyboard focus uses the standard accent ring. The expanded exact checkpoint text is not a card; it sits below the divider on a 2px `border-2` left rail, uses Mono body text with whitespace preserved, scrolls within a viewport-relative height cap, and reveals the shared Copy action on hover/focus. Running dividers stay amber status rows and never open.

The spacing scale — five named anchors plus documented intermediates for this dense, technical UI:

- `xs` (4px) — tight gaps between related elements within a component
- `sm` (8px) — gaps between inline components, icon-label pairs
- `md` (14px) — intra-panel padding, row spacing
- `lg` (20px) — panel edge padding
- `xl` (28px) — section separation, message stream padding

**6, 10, 12, and 16px are sanctioned intermediate steps** — component-internal paddings and gaps in a UI this dense legitimately need them (the button/input paddings under `components:` above use 6/7/9/11/13px deliberately). Values on neither list are off-scale; don't introduce new ones. Legacy off-scale values migrate opportunistically as their files are touched, not in bulk.

## Elevation & Depth

Depth is purely tonal — no shadows except for floating elements. The four surface layers (bg → surface → surface-2 → surface-3) create hierarchy through color alone. Borders are always warm (brown-tinted), never neutral gray.

The only shadow uses are:
- Dropdowns and modals: `0 8px 24px rgba(0,0,0,0.4–0.5)` — enough to lift off the surface without competing with the color depth.
- Modal overlay: `0 24px 60px rgba(0,0,0,0.5)` with `backdrop-filter: blur(2px)`.
- Toast notifications: `0 6px 24px rgba(0,0,0,0.45)`.

Focus rings use the accent color at low opacity: `0 0 0 3px rgba(232,135,10,0.06)`.

## Shapes

The radius scale has three steps:

- **3px (sm):** Tight corners for micro-elements — avatars, icon buttons, kbd glyphs, token badges, tl-btn, pane-action. Feels engineered, not soft.
- **6px (md):** The default radius for interactive elements — buttons, inputs, dropdowns, modals, toggles, chips. Most of the UI lives here.
- **10px (lg):** Containers and card borders — detail-group cards, the message composer input wrap. Noticeably softer, used to demarcate bounded regions.

Circular elements (pulse dot, toggle knob) always use `border-radius: 50%`. Never use `border-radius: 9999px` on rectangular elements except pill-shaped status chips.

## Components

### Buttons

**Every button is the shared `Button` component (`webui/src/components/ui/Button.svelte`).** Views never hand-assemble the global `btn-*` classes; they pass a `variant` (`primary` | `secondary` | `tertiary` | `danger`), an optional `icon` boolean for the square icon footprint, and standard props (`type`, `disabled`, `loading`, `ariaLabel`, `onClick`, label/icon as the `children` snippet). A Vitest guard scan (`webui/src/lib/__tests__/uiPrimitives.guard.test.js`) fails the build if any raw `<button>` reintroduces a primitive class, so the levels below cannot drift back into hand-built markup.

Each variant emits exactly **one** canonical class (the historical aliases `btn-new`, `btn-outline`, `modal-btn-confirm`/`modal-btn-cancel`, `send-btn`, `icon-btn`, `tl-btn`, `pane-action`, `btn-dang` were collapsed into these):

1. **Primary (`btn-primary`)** — Accent ghost: `rgba(accent, 0.10)` fill, `rgba(accent, 0.22)` border, accent text. Hover deepens fill to `0.18`, border to `0.38`. Used for the single most important action per panel. With `icon`, becomes the 32px square send-style button.

2. **Secondary (`btn-secondary`)** — Neutral ghost: no fill, `border-2` border, `text-med` color. Hover shifts to accent border and tint. Used for supporting actions (and modal confirm/cancel pairs).

3. **Tertiary (`btn-tertiary`)** — Smallest footprint. `border` border, `text-lo` color, 3px radius. Hover becomes accent. With `icon`, becomes the 30px borderless square icon button (composer mic/attach); the engaged state adds the `btn-icon--active` accent tint.

4. **Danger (`btn-danger`)** — Destructive actions (Archive, Delete, Remove). Same neutral-ghost base as secondary, but hover shifts to `red` border and tint. With `icon`, becomes the 32px square stop-style button (composer run cancel) and is red **at rest** (`rgba(red, 0.07)` fill, `rgba(red, 0.22)` border, red icon; hover deepens) — an icon-only danger button has no label, so color must carry the destructive meaning.

Primary save buttons inside long editor panels stay enabled even when the form is already clean. When nothing changed, the interaction should confirm trust via lightweight success feedback instead of disabling the control.

**Save model for settings/config surfaces:** every settings-style panel auto-saves changes with a short debounce (800ms) *and* keeps an explicit Save button at the bottom of the panel for users who do not trust auto-save. Clicking Save on a clean form shows an "Already saved" success toast. Persisted Agent and Project configuration editors follow the same scheme; entity creation and deliberate confirmation flows (including channels, cron jobs, secrets, workspace-copy decisions, rename, reset, and removal) remain explicit because half-typed entities or consequential operations must not persist implicitly. The Extensions panel follows this scheme for non-secret config (schema form or raw-JSON config); secrets are never autosaved and keep an explicit per-secret Save flow.

**Autosave transition boundary:** a user-initiated context change inside the app that would replace an autosave editor first flushes its pending debounce and any in-flight request. If the user edited again during that request, the newest snapshot is saved in another pass before navigation continues. The destination appears only after success. Validation or persistence failure keeps the current editor visible and opens a blocking modal with Retry and Discard and continue; unchanged failed snapshots are not retried silently in the background. Browser tab visibility is deliberately outside this boundary, and explicit operations do not become autosaves merely because they navigate afterward.

### Copy button

**Copy-to-clipboard actions use the shared `CopyButton` component (`webui/src/components/ui/CopyButton.svelte`).** It wraps the tertiary icon `Button` and owns the clipboard write plus the transient confirmation: the clipboard icon swaps to a check and the label flips to "Copied" for ~1.5s, then reverts. Callers pass `text` (the exact string to copy) and may override `label`/`copiedLabel` (default to `common.copy`/`common.copied`), `variant`, `class`, and `onCopied`. The copy is best-effort — a blocked clipboard fails silently without disrupting the view. Placement and any reveal-on-hover are the caller's concern (e.g. log rows reveal it on row hover via a passed-in class and shrink it from the default 30px icon footprint); the component itself stays visibility-agnostic so it fits a dense table row or a chat message equally.

### Inputs

**Every ordinary labeled control is wrapped by the shared `FormField` component (`webui/src/components/ui/FormField.svelte`).** The caller keeps the value and validation state; `FormField` owns the visible Mono-caps label, optional required marker, help text, error text, spacing, and the generated ids that connect help/error copy to the control through its snippet contract. Optional `actions` render after feedback, and `full` spans a two-column form grid. Agents, Projects, provider connection, Channels, Extensions, and Specialized Models use this shell around `TextField`, `TextArea`, `Dropdown`, `SearchableDropdown`, and `Toggle`; toolbar filters, Chat Composer, and other specialized controls remain outside it. The guard rejects raw canonical classes and the retired modal/settings/agent field-shell classes.

**Every single-line text field is the shared `TextField` component (`webui/src/components/ui/TextField.svelte`).** It uses the callback-prop pattern (`value` in, `onInput(next, event)` out — never `bind:`) and takes `type`, `variant`, `readonly`, `invalid`, `disabled`, `inputmode`, `placeholder`, `ariaLabel`. The guard scan fails the build if a raw `<input class="s-input">`/`modal-input` or a raw `s-value-box` appears outside the component.

**Default input (`variant="default"` → `s-input`)** — Mono font at 12.5px, `--field-surface` background, `border-2` border, 6px radius. Focus: accent border + glow ring. `invalid` adds a red border (`s-input--invalid`) that wins over the focus glow.

**Modal input (`variant="modal"` → `modal-input`)** — Same as default but uses the deepest `bg` as background for contrast against the `surface` modal backdrop.

**Read-only value (`readonly` → `s-value-box`)** — Renders a non-interactive `<div>` with the same geometry and mono type as the default input but a structural `border` (not `border-2`), transparent background, and `text-med` color. Read-only facts (server host, data directory, default skill directory) must never wear the editable input chrome.

**Every ordinary multi-line form field is the shared `TextArea` component (`webui/src/components/ui/TextArea.svelte`).** It follows the same `value` / `onInput(next, event)` callback contract as `TextField` and owns the default, inset, code, invalid, disabled, and read-only states. `variant="default"` is the bordered `--field-surface` field used by Cron and Settings; `variant="inset"` is the borderless `--prompt-content-surface` editor integrated into a bounded System Prompt block. Its darker content surface sits below the shared lighter `--prompt-header-surface`, matching generated and editable blocks instead of reversing their hierarchy. `code` preserves whitespace, enables horizontal overflow, and raises the minimum height for JSON editors; `invalid` keeps a red border/ring and sets `aria-invalid`. The guard allows raw textareas only in the specialized Chat Composer and queued-message editor and rejects every retired form-textarea class.

**System Prompt block state** — Editable and generated blocks share the same lighter-title/darker-content hierarchy, but enabled state remains independent from block type. A disabled block keeps that structural orientation while the complete card is dimmed to 55% opacity and its Toggle is off, so it cannot read as active.

**Chat composer** — Its full-width outer area continues the Chat `bg` without a structural border, so the composer sits directly in the conversation surface. The interactive composer is a `--composer-surface`-filled rounded rectangle (10px radius) with a `border-2` border; that semantic token currently resolves to `surface` and is deliberately independent from ordinary input/editor colors. Its specialized auto-resizing textarea (max 182px, hidden scrollbar) and action buttons sit flush to the bottom-right; it is deliberately not `TextArea`. Focus applies the accent border + glow. Slash/Skill and Files autocomplete panels open above the composer, share its centered `--chat-measure` width and responsive horizontal inset, and scroll internally within their viewport-relative height cap; the `full` Chat width preference expands both panels with the composer. Slash/Skill rows use a compact shared name track followed by the description track, keeping descriptions aligned without leaving a large dead zone after short command names; mobile rows stack both tracks. The queued-message editor is the other deliberate raw-textarea exception because it owns inline queue-edit behavior.

**Composer focus after Chat navigation** — A user-triggered New Session action focuses the composer on every viewport, including when the current Session is already empty and no new Session is created. User-triggered Agent, Project Agent, Project, Session, and return-to-parent/current changes focus the composer after the destination history is ready on desktop, using `preventScroll`; mobile Agent/Session navigation stays in reading mode and does not summon the keyboard. Mount restoration, browser-history navigation, reconnects, invalidations, and other passive state changes never steal focus.

**Prompt preview** — The read-only System Prompt preview is one bounded content surface, not an input. Its body uses `--preview-surface`, its header lifts to `surface-2`, and both the outer outline and header divider use `border-2`. This tonal hierarchy must remain visible against the page `bg`; a border alone is not sufficient separation.

### Toggles

**Every switch toggle is the shared `Toggle` component (`webui/src/components/ui/Toggle.svelte`).** It renders the `role="switch"` button + knob; callers pass `checked`, `onChange(next)`, `size`, `disabled`, and `ariaLabel`. The guard scan fails the build if a raw `<button class="toggle">`/`tl-toggle` — or any raw `<input type="checkbox">` — appears outside the component. (The `stats-toggle` segmented control is a distinct control, not this switch.)

Two sizes, same visual language:
- **Large (`size="lg"`, 38×22px):** Used in settings rows alongside label-value pairs.
- **Small (`size="sm"`, 30×17px):** Used in tool/skill toggle lists inside agent detail.

Both: `surface-3` off-state, full `accent` on-state. White knob. Smooth 0.2s `left` transition.

### Dropdowns

Two shared components, both **flat in `webui/src/components/`** — there is **no** `ui/Dropdown.svelte`, so don't go looking in `components/ui/` for them:
- **Simple — `Dropdown.svelte`** — Portaled, fixed-positioned list anchored to the trigger. Uses `surface-2` + `border-2`. No filter; use for short option sets.
- **Searchable — `SearchableDropdown.svelte`** — Fixed-positioned panel (escapes any `overflow: hidden` ancestor). Has a filter input header with a search icon; use for long lists (e.g. the model picker). Panel border uses accent tint `rgba(accent, 0.3)` to signal "elevated and interactive".

Both share the same callback-prop contract (`value` in, `onValueChange(value, option)` out; `options` as strings or `{ value, label, disabled, secondaryLabel }`, plus `id`/`placeholder`/`disabled`/`ariaLabel`/`triggerClass`) and position through `lib/dropdownPanel.js`. **Both fit the viewport**: `computePanelPosition` flips the panel above the trigger when it would not fit below (and there is more room above), and caps its height to the room actually available on the chosen side (`optionsMaxHeight`, list scrolls internally) — so a picker near the bottom edge opens upward instead of spilling off-screen. This is a primitive-level behavior, not a per-call-site fix; the simple `Dropdown` additionally feeds its measured content height in so the flip triggers on actual fit. Model dropdowns build their `options` from `lib/modelSelection.js` (`buildModelSelectOptions`), so a model picker is `SearchableDropdown` + that helper, not a bespoke control.

### Modals

**Every modal is the shared `Modal` component (`webui/src/components/ui/Modal.svelte`).** The shell owns the dialog chrome: the dimmed `modal-overlay` (blur + dark scrim), overlay-click-to-close, Escape-to-close, `role="dialog"` / `aria-modal`, the `modal-header` with `modal-title` + the `modal-close` (×) button, and moving keyboard focus into the dialog on open. Callers pass `title`, `labelledById`, `onClose`, an optional `closeDisabled` (blocks all close paths and disables ×), an optional `class` on the box, and supply a `body` snippet (their own `modal-body` content) plus an optional `footer` snippet (the shell wraps it in `modal-footer`). Forms that span body + footer keep their `<form>` inside the `body` snippet; footer submit buttons associate by `form="…"` id. The guard scan fails the build if a raw element reintroduces `modal-overlay`/`modal-header`/`modal-title`/`modal-close` outside the shell.

### Confirm dialogs

Destructive or irreversible actions always confirm through the shared `ConfirmDialog` component (`webui/src/components/ui/ConfirmDialog.svelte`), never the native `window.confirm`. It builds on the `Modal` shell (overlay, Escape, ×, focus, aria) and adds only the confirm/cancel decision: a `title` naming the entity, a one-paragraph `body` stating the consequence, and a confirm/cancel button pair in the footer. The body states the consequence honestly — archived/restorable (projects, sessions, agents) versus permanent (cron jobs, skills, channels, prompt-block resets) — so the user is never surprised. The confirm button carries the action verb (Delete/Remove/Reset) and renders in the `danger` variant for irreversible actions (`primary` when `danger={false}`); cancel is always `secondary`, and overlay-click, Escape, and × all route to cancel. Callers own the open state and conditionally render the dialog, the same pattern as `Modal`. The guard scan (`webui/src/lib/__tests__/uiPrimitives.guard.test.js`) fails the build if a native `confirm(` call reappears in any component.

### Modal pick lists

Choice steps inside modals (e.g. provider/connection selection in the provider
connect modal) render as a vertical list of full-width button cards: `bg` fill,
`border-2` border, 6px radius, name in Sans (`text-hi`) with detail line in
mono (`text-lo`). Hover/focus shifts to accent border + `accent-dim` tint, the
same interaction language as dropdown options.

### Inline SVG icons

If we use inline SVGs without explicit `width` and `height`, they can suddenly render far too large because size falls back to browser/default layout behavior. To prevent that, always set SVG icon dimensions explicitly.

### Status chips

**Every status chip is the shared `StatusChip` component (`webui/src/components/ui/StatusChip.svelte`).** It is a pill (12px border-radius, mono 11.5px) carrying one semantic color; callers pass a `variant` and the already-translated label as `children`. The component emits the canonical `chip <variant>` classes — the color-named aliases (`chip-green`/`chip-amber`/`chip-orange`/`chip-red`) were collapsed into the semantic names, and the guard scan fails the build if a raw element reintroduces `chip` outside the component. Variants:
- `success`: `green-dim` bg + `green` text
- `warn`: amber tint bg + `amber` text
- `info`: `accent-dim` bg + `accent` text
- `error`: red tint bg + `red` text
- `neutral`: `surface-3` bg + `text-med` text

(Scoped `*-chip` labels like the System Prompt preview `sp-scope-chip` are distinct controls with their own classes, not this component. Small metadata tags are the separate `Badge` component — see below.)

### Badges

**Every metadata tag is the shared `Badge` component (`webui/src/components/ui/Badge.svelte`).** It is the counterpart to `StatusChip`: a small pill (999px border-radius, 1px border, mono `--fs-mono-xs`, padding 2px 7px, 4px icon/text gap) carrying a metadata marker — a kind, origin, version, or scope — rather than a status. Callers pass a `variant` and the already-translated label (optionally preceded by an inline icon whose SVG keeps explicit `width`/`height`) as `children`; the component emits the canonical `badge badge--<variant>` classes, and the guard scan fails the build if a raw element reintroduces `badge` (or a retired bespoke pill class) outside the component. Variants reuse `StatusChip`'s names and tints: `neutral` (`surface-3` bg + `text-med`), `info` (`accent-08` bg + `accent` text, `accent-30` border), `success` (green tint), `warn` (amber tint), `error` (red tint); default `neutral`.

**The rule: metadata tags (kind markers, origins, versions, scopes) use `Badge`; statuses use `StatusChip`.** A `use:tooltip` hint goes on a wrapping `<span class="tooltip-anchor" use:tooltip={…}>` at the call site, since Svelte actions cannot be applied to a component.

### Server availability popup

Server unavailability is represented once, globally, by the fixed non-modal popup owned by AppShell — never by repeating the same transport error inside each mounted view. It is horizontally centered at `24vh` (`18vh` on phone widths), 520px wide at most, uses `surface-3` with a red 1px outline plus 4px left edge, `lg` radius, the strong dropdown elevation, and the dedicated `--z-notice` layer between modals/toasts and portaled floating controls. A pulsing red status dot, Mono-caps connection label, clear heading, short automatic-retry message, optional Details disclosure, and a shared secondary Retry button provide the hierarchy; reduced-motion users get no entrance or pulse animation.

The popup has no backdrop and never blocks the main navigation, so the user can move between Chat, Agents, Projects, and the other views while it remains in exactly the same viewport position. The active content stays visible and scrollable but is inert while disconnected; dependent inline errors and error toasts are suppressed because they are symptoms of the same outage. Retry is always present. When the Desktop bridge advertises server selection, a primary Switch server action hides the popup and opens the shared Desktop server picker in a modal outside the inert content; browser accessors never show it. After recovery the popup switches to green long enough to confirm that the active view refreshed, then disappears. A one-second grace period prevents ordinary short reconnects from flashing the popup.

### Inline banners

**Every persistent in-flow feedback box is the shared `Banner` component (`webui/src/components/ui/Banner.svelte`).** Use it for loading feedback, form or RPC errors, warnings, and non-blocking notices that belong inside a view; a known absence of content uses `EmptyState`, and transient app-wide feedback belongs in `ToastStack`. Callers pass `variant` (`neutral` / `info` / `success` / `warn` / `error`), already-translated `children`, and accessibility attributes such as `role="alert"` or `aria-live` when the state changes dynamically. The component owns the canonical `banner banner--<variant>` classes and the guard rejects both raw primitive classes and the retired view-specific feedback classes.

The banner uses `surface-2`, a `border-2` outline, and a 2px semantic left stripe; semantic color stays in the stripe and low-opacity tint while the text remains `text-med`. Content is a space-between flex row so a trailing Retry or Review action sits at the right, then stacks on mobile. Feature surfaces may pass a layout class for placement or geometry (the full-width first-run strip, the capped Chat footer banner, compact assistant-run notices), but they do not recreate the semantic variants.

### Empty states

**Every known absence of content is the shared `EmptyState` component (`webui/src/components/ui/EmptyState.svelte`).** It takes already-translated `title` and `description` text plus optional `icon` and `actions` snippets. `density="default"` is the full-view or master-list surface; `density="compact"` is the same hierarchy inside a card, table, drawer, or settings section; `fill` consumes the remaining flex space. The floating Chat Activity micro-list is the deliberate exception: because its entire expanded body is only a terse task index, absence is one muted line rather than a nested empty-state surface. Loading, failure, and warning states are not empty and stay with `Banner`.

The surface uses a low-contrast `surface-2` tint, dashed `border`, `lg` radius (`md` when compact), centered Sans text, and no accent color. Default empty states have at least 160px of height; compact states remove that floor and reduce padding to 14px. Feature classes may set only placement such as pane margin or reading-column width. The guard rejects raw canonical classes and all retired view-specific empty-state classes.

### Content tabs

**Every local content view switch uses the shared `TabList` component (`webui/src/components/ui/TabList.svelte`).** Callers pass already-translated `{ id, label }` items, the active `value`, `ariaLabel`, and an `onChange` callback; they keep ownership of panel content and link each panel back to the generated tab id. The component owns `role="tablist"` / `role="tab"`, selected state, one-tab stop, panel ids, and automatic Arrow Left/Right, Home, and End activation. The guard rejects both raw canonical classes and the retired Statistics/Debug tab classes.

`appearance="underline"` is the default section-level treatment: transparent Sans tabs on the shared bottom rule, with the active tab marked only by accent text and a 2px accent underline. `appearance="segmented"` is the compact alternate-representation treatment used for Raw/Parsed content: a `surface-2` bounded group with an `accent-12` active tint. `density="compact"` switches both treatments to the smaller Mono technical voice. Long tab sets scroll horizontally without exposing a scrollbar; every tab uses the global focus ring. Period selectors, filters, and the Chat agent navigation are not content tabs and remain distinct controls.

**Chat Agent activity navigation** — Identity and Project Team Agent bars use a 7px status dot independently from selection. Neutral `text-lo` means idle, amber means a Run is active (a restrained pulse, disabled under reduced motion), and blue means a durable unread terminal result. State priority is running over unread over idle. The selected Agent is identified only by accent text plus the 2px accent underline; selection never recolors the status dot. Every Agent button exposes the same state in its accessible label and quick tooltip. The Session drawer repeats blue as a compact dot-text marker on the concrete unread Session rather than inventing a status chip.

### Log viewer

- The Logs tab uses the standard field/dropdown styling (`--field-surface` for editable inputs, `surface-2` for dropdowns, `border-2`, mono text) for file selection, level filtering, sort order, and search. Use the shared **simple** dropdown style for the file, level, and order controls.
- Live connection state uses the shared `StatusChip` (it is a status, not a metadata tag): `neutral` by default, `success` for connected, `warn` for reconnecting, `error` for stream errors.
- Log entries render as dense single-row list items, not roomy stacked cards. Each row keeps timestamp, level, logger, and message on one line on normal desktop widths, with truncation acceptable for long content.
- Rows keep a 3px semantic left border: accent for info, amber for warn, red for error, neutral `border-2` for unknown/other levels. Warn treatment should be visibly stronger than info, not just a near-match.
- Log metadata and message preview stay monospace and compact. Full multiline continuation text may still be exposed through tooltip/title or responsive fallback behavior, but the default desktop presentation is one visible row per entry.

### Toasts

Slide in from the right (bottom-right stack). `surface` background, `border-2` border, 6px radius, dark shadow. Left border only — 2px colored stripe indicates type: green (success), red (error), amber (warn), accent (info). No icons — the stripe carries all semantic weight.

Error toasts persist until the user manually dismisses them (via the × button) — a transport/server failure the user must acknowledge; success, info, and warn toasts auto-dismiss after a short delay. A caller may override this per toast, but the default is derived from the variant.

### Command output (toast & transient card)

Built-in slash command replies render at the **bottom** of the chat, not the top notice stack. Two surfaces, chosen by the command's `output` channel (see `.vorch/domain-maps/chat.md`):

- **Bottom command toast** (`toast` channel, e.g. `/stop`, `/compact`): a chat-local confirmation floating just above the composer (`bottom: calc(100% + 10px)`, centered on `--chat-measure`). `surface` background, `border-2` border with a 2px accent left stripe, dark shadow, `font-ui` `text-med` body with `pre-wrap`. Auto-dismisses after 5s. This is the composer-local toast pattern — **not** the app-wide bottom-right `ToastStack`.
- **Transient card** (`transient` channel, e.g. `/status`, `/help`): a non-persisted card in the chat stream. `surface-2` fill, `border-2` border with a 2px accent left stripe, 10px radius. A `mono-sm` uppercase label tag sits above a monospace (`font-mono`, 12px) `pre-wrap` body for the key/value lines, so it reads as a diagnostic snapshot distinct from real chat bubbles. Cards **stack** (no dedup, no dismiss) — successive snapshots sit underneath each other for visual comparison — and disappear on session switch or reload.

### Tooltips & info hints

Two tiers, one rule: the browser-native `title` tooltip is never used (unstyled, delayed, no touch support — a Vitest guard bans it).

**Quick tooltip** (`use:tooltip` action from `lib/tooltip.js`, or the `tooltip` prop on `Button`/`CopyButton`) — the short hover label: what a button does, the untruncated value, a data point. One shared floating element per app, portaled to `<body>`: `surface-3` fill, `border-2` border, 3px radius, `text-hi` at 11.5px `font-ui`, 4px 9px padding. Appears after 150ms above the anchor (falls below near the top edge), keeps line breaks for structured hover text (token badge), and clips oversized content. Purely presentational — icon-only buttons still carry `aria-label`.

**Structured hover card** (`use:floatingHoverCard` action from `lib/tooltip.js`) — rich or interactive hover content that cannot be flattened into a quick tooltip, such as a copyable complete Tool value, Tool/Skill descriptions with warnings and actions, or Attachment image previews. The card is fixed-positioned and portaled to `<body>`, uses the shared `--z-floating` layer, prefers the space above its anchor, falls below when needed, and clamps to the viewport; never implement one as an absolute child of a card or scroll container. Cards open on hover, keyboard focus, or touch, keep a short pointer-crossing grace period, toggle closed on a second touch, and close on outside touch/click, Escape, resize, or ancestor scroll. Descriptive cards link through `aria-describedby`; decorative previews remain hidden from the accessibility tree.

**Info hint** (`ui/InfoHint.svelte`) — the "?" dot for explanations: a 15px circular `border-2` outline button with a mono "?" in `text-lo`, turning accent on hover/focus/open. Its popover is an elevated card (`surface-2`, `border-2`, 6px radius, dropdown shadow, `body-sm` in `text-med`, max-width 320px) that opens on hover, pins on click/tap for touch, and closes on Escape or outside click. Use it wherever an explanation would otherwise sit in the UI as permanent grey text; keep permanently visible inline hints only for must-see state (inherited values, "this model does not support reasoning") and warnings.

### Tool call events

Inline dot-text lines within assistant messages. No box or card: one line orders the colored `●` dot and monospace Tool name, a flexible semantic primary group, fixed result facts, status/live-or-final duration, and compact actions. Primary values have a 64-character deterministic limit before responsive overflow: paths keep the suffix and at most two parent directories, prose keeps the prefix, URLs/ids preserve both ends; fixed facts, timing, and actions never shrink. Complete non-copyable values use the Quick tooltip, while copyable full values use the Structured hover card and shared Copy Button. The expandable body (indented, `border-2` left border) retains ARGS and RESULT in mono-xs labels + mono-body values. Preparing previews use the neutral/running bucket without elapsed time; dispatched running Tools use an amber blinking dot and live elapsed time; done is green, and failures are red. Background Bash and Sub-Agent cancellation use shared 24px danger icon Buttons; the Sub-Agent Session action is a neutral 24px Session/arrow icon Button and remains after completion.

### Chat messages

User messages: right-aligned, `surface-2` card with `border-left: 3px solid accent`. Max-width 75% of the capped reading measure (see Layout → Content width). Header reversed (avatar on right).

Assistant messages: no card — prose flows free within the centered, capped reading measure (`--chat-measure`). Tool events, thinking blocks, and code snippets appear inline between prose paragraphs.

Error messages: standalone timeline entries, not assistant bubbles. Use the red semantic token sparingly: red avatar tint, red author label, and a compact text block with a red left border plus low-opacity red background.

Thinking blocks: collapsible, italic `text-med` body, `border-2` left border, `font-ui` (not mono). Collapsed by default in production; open in prototypes.

Code blocks: `bg` fill, `border` border, `surface-2` header bar with language label + copy button. Mono 12px / `text-med`.

## Do's and Don'ts

- Do use sentence case for buttons, modal titles, action labels, and table headers ("Create agent", "New session") — title case is reserved for nav/view names that are proper nouns of the app (Chat, Agents, System Prompt). Mono-caps section labels are uppercased by CSS, not in the string.
- Do use IBM Plex Sans for all human-readable UI text.
- Do use IBM Plex Mono for anything that names a system artifact: model IDs, tool names, timestamps, section labels, code.
- Do use the accent color sparingly — active state, primary action, focus ring only.
- Do use the tonal layer system for depth; never add arbitrary shadows.
- Do not mix warm brown borders with neutral gray — all borders must come from the `border` / `border-2` tokens.
- Do not use `border-radius: 9999px` on rectangular interactive elements.
- Do not introduce a third typeface.
- Do not use `rgba(accent, x)` for decorative gradients or fills on large areas — only for interactive states and rings.
- Do maintain WCAG AA contrast for all `text-hi` text on `bg` and `surface` backgrounds.
