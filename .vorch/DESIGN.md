---
version: alpha
name: vBot — Toasted
description: >
  Warm charcoal-brown dark UI for a local-first AI agent harness.
  Dense, technical, and deliberate — a control room that runs hot.
colors:
  bg:          "#221A12"
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
  red:         "#FC8181"
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
    fontSize: 11px
    fontWeight: 500
    lineHeight: 1
  mono-xs:
    fontFamily: IBM Plex Mono
    fontSize: 10px
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
    backgroundColor: "{colors.surface-2}"
    borderColor: "{colors.border-2}"
    textColor: "{colors.text-hi}"
    typography: "{typography.mono-body}"
    rounded: "{rounded.md}"
    padding: 7px 11px
  input-default-focus:
    borderColor: "rgba(232,135,10,0.40)"
    boxShadow: "0 0 0 3px rgba(232,135,10,0.06)"
  input-composer:
    backgroundColor: "{colors.bg}"
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

The palette is organized around four layers of warm dark surface and three semantic status colors.

- **Bg (#221A12):** The page foundation — darkest, used behind the sidebar and the chat message stream. Warm near-black with a distinct brown cast.
- **Surface (#2B2217):** Primary panel surface — sidebar, input bar, agent list. One step up from the background.
- **Surface-2 (#33291D):** Elevated cards, dropdown backgrounds, user message bubbles. Used whenever a component needs to sit above its container.
- **Surface-3 (#3D3124):** Tertiary highlight layer — toggle tracks, code block backgrounds, hover surfaces for dropdowns.
- **Border (#4A3928) / Border-2 (#5D4A35):** Two border strengths. `border` for structural dividers (sidebar edge, section separators). `border-2` for interactive element outlines (inputs, buttons, dropdowns).
- **Text-hi (#EEE7DC):** Warm near-white. All primary content — message prose, headings, input values.
- **Text-med (#9A8C7E):** Secondary copy — assistant author label, tool event text, description rows, metadata.
- **Text-lo (#5E4C38):** Muted — timestamps, section labels, placeholder text, inactive nav items, toggle-list item names.
- **Accent (#E8870A):** The single interaction color — a saturated amber-orange. Active nav, focused borders, primary buttons, user message accent border. Reserve it; never use it for decoration.
- **Green (#4ADE80):** Success and running-healthy state (tool call done, server status pulse dot).
- **Amber (#F59E0B):** In-progress / warning state (running tool call indicator, blinking dot animation).
- **Red (#FC8181):** Error state (failed tool call, destructive button hover).

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
- **mono-sm** — Timestamps, chip text, toast labels, token badge. 11–11.5px / 500.
- **mono-xs** — Section labels (TOOLS, SKILLS, ARGS, RESULT), pane titles. 10–10.5px / 500 / 0.07em uppercase.

## Layout

The app shell is a fixed sidebar (210px) plus a fluid main content area. The sidebar never shrinks or grows. The main area holds views that each fill the full remaining width and height.

Within views, two-pane splits (Agents: 240px list + fluid detail; Settings: 168px nav + fluid panel) use a thin `border` divider with no gap. Padding inside panels is 20–32px depending on context.

In scrollable edit surfaces, the primary save action belongs at the bottom of the scroll region in a sticky footer, not in the panel header. This keeps the action visually near the edited fields and available after long scrolls.

### Content width / reading measure

No view stretches edge-to-edge on a wide monitor. Each scroll area fills its full width (scrollbar at the edge), but its **inner content column is capped and centered** so prose keeps an ergonomic line length. The caps are CSS custom properties in `:root` (`webui/src/styles/app.css`):

- `--chat-measure-comfortable: 780px` (~72ch, the default) · `--chat-measure-wide: 1100px` — the chat reading column.
- `--chat-measure` is the live chat measure. It defaults to `--chat-measure-comfortable` and is overridden by `[data-chat-width]` on `.chat-view`: `wide` → `--chat-measure-wide`, `full` → `none` (edge-to-edge, the old behavior). The active value is user-selectable in **Appearance** settings (`appearance.chat_width`, persisted) and applied app-wide.
- `--content-max-narrow: 920px` — settings/forms and label↔control rows. `--content-max-wide: 1180px` — detail/wide panels (Agents detail, System Prompt). These are fixed, not user-configurable.

Wide data tables and log rows (Logs, Cron, Debug) are **not** force-capped — they keep their own horizontal scroll rather than squishing.

### Breakpoints

CSS media queries cannot read custom properties, so the breakpoint set is a documented convention rather than a token. Use these thresholds; only deviate where a specific layout genuinely regresses:

- **mobile** ≤ 640px · **tablet** 641–960px · **desktop** ≥ 961px · **wide** ≥ 1280px.

On mobile (≤640px) the sidebar collapses to a top bar, two-pane splits stack, and interactive targets grow to ≥40px hit area.

### Chat messages

Chat messages use 28px horizontal padding as the column gutter. User message bubbles are right-aligned at 75% max-width *of the capped reading measure*. Assistant prose flows free (no bounding box) but within the same centered, capped measure. The composer, notice stack, and session banner align to the same center axis as the messages.

The spacing scale:

- `xs` (4px) — tight gaps between related elements within a component
- `sm` (8px) — gaps between inline components, icon-label pairs
- `md` (14px) — intra-panel padding, row spacing
- `lg` (20px) — panel edge padding
- `xl` (28px) — section separation, message stream padding

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

4. **Danger (`btn-danger`)** — Destructive actions (Archive, Delete, Remove). Same neutral-ghost base as secondary, but hover shifts to `red` border and tint.

Primary save buttons inside long editor panels stay enabled even when the form is already clean. When nothing changed, the interaction should confirm trust via lightweight success feedback instead of disabling the control.

**Save model for settings/config surfaces:** every settings-style panel auto-saves changes with a short debounce (800ms) *and* keeps an explicit Save button at the bottom of the panel for users who do not trust auto-save. Clicking Save on a clean form shows an "Already saved" success toast. This is the general scheme for all settings/config surfaces in the app; only entity create/edit forms (agents, channels, cron jobs) stay explicit-save-only, because half-typed entities must not persist.

### Inputs

**Default input (`s-input`)** — Mono font at 12.5px, `surface-2` background, `border-2` border, 6px radius. Focus: accent border + glow ring.

**Modal input** — Same as default but uses the deepest `bg` as background for contrast against the `surface` modal backdrop.

**Read-only value (`s-value-box`)** — Same geometry and mono type as the default input, but visually non-interactive: structural `border` (not `border-2`), transparent background, `text-med` color. Read-only facts (server host, data directory, default skill directory) must never wear the editable input chrome.

**Chat composer** — A `bg`-filled rounded rectangle (10px radius) with `border-2` border. Contains an auto-resizing textarea (max 182px, hidden scrollbar) and action buttons flush to the bottom-right. Focus applies the accent border + glow.

### Toggles

**Every switch toggle is the shared `Toggle` component (`webui/src/components/ui/Toggle.svelte`).** It renders the `role="switch"` button + knob; callers pass `checked`, `onChange(next)`, `size`, `disabled`, and `ariaLabel`. The guard scan fails the build if a raw `<button class="toggle">`/`tl-toggle` appears outside the component. (The `voice-toggle` checkbox-slider in the Desktop Voice panel and the `stats-toggle` segmented control are distinct controls, not this switch.)

Two sizes, same visual language:
- **Large (`size="lg"`, 38×22px):** Used in settings rows alongside label-value pairs.
- **Small (`size="sm"`, 30×17px):** Used in tool/skill toggle lists inside agent detail.

Both: `surface-3` off-state, full `accent` on-state. White knob. Smooth 0.2s `left` transition.

### Dropdowns

Two types:
- **Simple** — Absolute-positioned list below trigger. Uses `surface-2` + `border-2`.
- **Searchable** — Fixed-positioned panel (escapes any `overflow: hidden` ancestor). Has a filter input header with a search icon. Panel border uses accent tint `rgba(accent, 0.3)` to signal "elevated and interactive".

### Modals

**Every modal is the shared `Modal` component (`webui/src/components/ui/Modal.svelte`).** The shell owns the dialog chrome: the dimmed `modal-overlay` (blur + dark scrim), overlay-click-to-close, Escape-to-close, `role="dialog"` / `aria-modal`, the `modal-header` with `modal-title` + the `modal-close` (×) button, and moving keyboard focus into the dialog on open. Callers pass `title`, `labelledById`, `onClose`, an optional `closeDisabled` (blocks all close paths and disables ×), an optional `class` on the box, and supply a `body` snippet (their own `modal-body` content) plus an optional `footer` snippet (the shell wraps it in `modal-footer`). Forms that span body + footer keep their `<form>` inside the `body` snippet; footer submit buttons associate by `form="…"` id. The guard scan fails the build if a raw element reintroduces `modal-overlay`/`modal-header`/`modal-title`/`modal-close` outside the shell.

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

(The Logs connection chip and other scoped `*-chip` labels are distinct controls with their own classes, not this component.)

### Log viewer

- The Logs tab uses the standard input/dropdown styling (`surface-2`, `border-2`, mono text) for file selection, level filtering, sort order, and search. Use the shared **simple** dropdown style for the file, level, and order controls.
- Live connection state uses a pill-shaped mono status chip: neutral by default, green for connected, amber for reconnecting, red for stream errors.
- Log entries render as dense single-row list items, not roomy stacked cards. Each row keeps timestamp, level, logger, and message on one line on normal desktop widths, with truncation acceptable for long content.
- Rows keep a 3px semantic left border: accent for info, amber for warn, red for error, neutral `border-2` for unknown/other levels. Warn treatment should be visibly stronger than info, not just a near-match.
- Log metadata and message preview stay monospace and compact. Full multiline continuation text may still be exposed through tooltip/title or responsive fallback behavior, but the default desktop presentation is one visible row per entry.

### Toasts

Slide in from the right (bottom-right stack). `surface` background, `border-2` border, 6px radius, dark shadow. Left border only — 2px colored stripe indicates type: green (success), red (error), amber (warn), accent (info). No icons — the stripe carries all semantic weight.

### Command output (toast & transient card)

Built-in slash command replies render at the **bottom** of the chat, not the top notice stack. Two surfaces, chosen by the command's `output` channel (see `.vorch/domain-maps/chat.md`):

- **Bottom command toast** (`toast` channel, e.g. `/stop`, `/compact`): a chat-local confirmation floating just above the composer (`bottom: calc(100% + 10px)`, centered on `--chat-measure`). `surface` background, `border-2` border with a 2px accent left stripe, dark shadow, `font-ui` `text-med` body with `pre-wrap`. Auto-dismisses after 5s. This is the composer-local toast pattern — **not** the app-wide bottom-right `ToastStack`.
- **Transient card** (`transient` channel, e.g. `/status`, `/help`): a non-persisted card in the chat stream. `surface-2` fill, `border-2` border with a 2px accent left stripe, 10px radius. A `mono-sm` uppercase label tag sits above a monospace (`font-mono`, 12px) `pre-wrap` body for the key/value lines, so it reads as a diagnostic snapshot distinct from real chat bubbles. Cards **stack** (no dedup, no dismiss) — successive snapshots sit underneath each other for visual comparison — and disappear on session switch or reload.

### Tool call events

Inline dot-text lines within assistant messages. No box or card — just a colored `●` dot + monospace function name + args + timing on one line. Expandable body (indented, `border-2` left border) shows ARGS and RESULT in mono-xs labels + mono-body values. Three states: done (green dot), running (amber blinking dot), error (red dot, red timing text, red result text).

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
