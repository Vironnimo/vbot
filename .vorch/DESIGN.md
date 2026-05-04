---
version: alpha
name: vBot Control Room
description: Minimal dark control-room interface for a local-first agent harness.
colors:
  background: "#15130F"
  panel: "#211D17"
  panelStrong: "#2D271F"
  text: "#F1EADF"
  muted: "#B8AA95"
  subtle: "#847762"
  accent: "#F0A43A"
  accentStrong: "#FFCA73"
  danger: "#FFB199"
typography:
  display:
    fontFamily: Georgia, Times New Roman, serif
    fontSize: 80px
    fontWeight: 700
    lineHeight: 0.95
  body:
    fontFamily: Trebuchet MS, Verdana, sans-serif
    fontSize: 16px
    fontWeight: 400
    lineHeight: 1.6
  labelCaps:
    fontFamily: Trebuchet MS, Verdana, sans-serif
    fontSize: 12px
    fontWeight: 700
    lineHeight: 1
    letterSpacing: 0.16em
rounded:
  md: 1rem
  lg: 1.5rem
spacing:
  xs: 0.35rem
  sm: 0.65rem
  md: 1rem
  lg: 1.5rem
  xl: 2rem
  2xl: 3rem
components:
  shell:
    sidebarWidth: 18rem
    borderColor: rgba(240, 164, 58, 0.22)
  panel:
    background: "{colors.panel}"
    borderRadius: "{rounded.lg}"
---

# Design System

## Overview

vBot should feel like a compact local control room: technical, calm, and
deliberate. The UI is dark and warm rather than generic blue/gray SaaS. It favors
high contrast, visible structure, and dense but readable controls for technical
users.

## Colors

- **Background (#15130F):** Warm near-black foundation for the whole app.
- **Panel (#211D17) / Panel strong (#2D271F):** Layered surfaces for cards,
  forms, and navigation selections.
- **Text (#F1EADF):** Warm light foreground for primary content.
- **Muted (#B8AA95) / Subtle (#847762):** Secondary copy, metadata, disabled
  states, and non-primary UI marks.
- **Accent (#F0A43A) / Accent strong (#FFCA73):** Gold interaction and focus
  color. Use sparingly for active state, headings, and primary actions.
- **Danger (#FFB199):** Destructive/error affordances.

## Typography

Display headings use Georgia/Times-style serif typography for a distinctive,
editorial command-center feel. Body text, labels, controls, and form copy use
Trebuchet/Verdana-style sans-serif for legibility. Technical labels use uppercase
letterspacing.

## Layout

The app shell is a two-pane layout: a fixed-width left navigation and a fluid
right content area. Content is contained in bordered panels with generous padding.
Responsive layouts collapse to a single column below tablet width.

## Elevation & Depth

Depth comes from warm tonal layers, thin gold-tinted borders, and large soft dark
shadows. Avoid heavy bright glows; reserve subtle accent glow for active nav
markers and focused/selected states.

## Shapes

Panels and inputs use rounded corners (`md` to `lg`). Circular marks may be used
for navigation sigils or brand accents.

## Components

Buttons and inputs use dark surfaces, gold-tinted borders, and warm text. Active
or selected cards use stronger panel backgrounds and accent borders. Error and
danger actions use the danger color while keeping the same dark-surface base.

## Do's and Don'ts

- Do keep the warm dark/gold palette consistent across new views.
- Do use the shared CSS custom properties from `webui/src/styles/app.css` for new
  component styling.
- Do keep forms compact but readable for technical users.
- Do not introduce generic bright-blue SaaS accents.
- Do not store user-facing strings directly in components; use i18n keys.
