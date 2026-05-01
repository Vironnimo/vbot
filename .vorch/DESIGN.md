---
version: alpha
name: [Project name]
# colors:
#   primary: "#000000"
#   secondary: "#000000"
# typography:
#   h1:
#     fontFamily: ...
#     fontSize: 48px
#     fontWeight: 600
#     lineHeight: 1.1
# rounded:
#   sm: 4px
#   md: 8px
# spacing:
#   xs: 4px
#   sm: 8px
#   md: 16px
# components:
#   button-primary:
#     backgroundColor: "{colors.primary}"
#     padding: 12px
---

# Design System

This is the **single source of truth for the project's visual identity**. Agents read it when working on UI. Only the Orchestrator updates it.

The YAML frontmatter above holds machine-readable design tokens. The sections below hold the human-readable rationale. Tokens are the normative values; prose explains how to apply them.

Remove sections that don't apply. Keep entries short and factual.

---

## Overview

[Brand personality, target audience, emotional response. Playful or professional, dense or spacious. Foundational context for stylistic decisions when no specific token applies.]

## Colors

[Color palettes and their semantic roles. At minimum: `primary`. Common additions: `secondary`, `tertiary`, `neutral`, `surface`, `error`. Describe how each is used (e.g., "primary only for the single most important action per screen").]

## Typography

[Typography levels and their roles. Common naming: `headline-*`, `display-*`, `body-*`, `label-*`, `caption`. Describe font choices and where each level applies.]

## Layout

[Layout strategy: grid model, fluid vs. fixed, breakpoints. Spacing scale (e.g., 4px/8px base). Containment principles — how related items are grouped.]

## Elevation & Depth

[How visual hierarchy is conveyed: shadows, tonal layers, borders, color contrast. If flat, explain the alternative.]

## Shapes

[Corner radii and shape language. Sharp/rounded/mixed and where each applies.]

## Components

[Style guidance for component atoms: buttons (primary/secondary/tertiary), inputs, chips, lists, tooltips, checkboxes, radios. Sizing, padding, and states. Domain-specific components belong here too.]

## Do's and Don'ts

[Practical guardrails — what to do, what to avoid. Concrete and enforceable.]
