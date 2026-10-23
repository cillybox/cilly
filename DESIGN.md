---
name: Cilly
description: Control panel for sandboxed coding agents; a dark, quiet, tool-like web app.
colors:
  bg-sidebar: "#07070b"
  bg: "#0a0b10"
  surface: "#11121a"
  surface-2: "#171923"
  surface-3: "#24222b"
  code-bg: "#08090d"
  line: "#1c1e28"
  line-strong: "#22242f"
  fg: "#ece8e1"
  fg-3: "#a8a2b3"
  fg-5: "#8d88a0"
  accent: "#3e3858"
  accent-fg: "#a99bd6"
  accent-line: "#7468a3"
  ok: "#8fd18b"
  warn: "#f2b45a"
  err: "#ff5d62"
  danger-btn: "#c2414a"
typography:
  body:
    fontFamily: "Geist, system-ui, sans-serif"
    fontSize: "14px"
    fontWeight: 400
    lineHeight: 1.5
  label:
    fontFamily: "Geist, system-ui, sans-serif"
    fontSize: "13px"
    fontWeight: 500
  small:
    fontFamily: "Geist, system-ui, sans-serif"
    fontSize: "12px"
  mono:
    fontFamily: "Geist Mono, ui-monospace, monospace"
    fontSize: "13px"
rounded:
  hair: "3px"
  tiny: "4px"
  xs: "6px"
  sm: "8px"
  md: "10px"
  lg: "12px"
  xl: "14px"
  panel: "16px"
  sheet: "20px"
  dialog: "22px"
  pill: "999px"
components:
  button:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.fg}"
    rounded: "{rounded.pill}"
    padding: "7px 16px"
  button-primary:
    backgroundColor: "{colors.fg}"
    textColor: "{colors.bg-sidebar}"
    rounded: "{rounded.pill}"
  button-accent:
    backgroundColor: "{colors.accent}"
    textColor: "#ddd6f7"
    rounded: "{rounded.pill}"
  button-danger:
    backgroundColor: "{colors.danger-btn}"
    rounded: "{rounded.pill}"
  input:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.fg}"
    rounded: "{rounded.md}"
    padding: "8px 12px"
  tag:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.fg-3}"
    rounded: "{rounded.xs}"
    padding: "1px 8px"
---

# Design System: Cilly

## Overview

**Creative North Star: "The Quiet Workbench"**

Cilly is an Operate-mode tool: a panel for starting, watching and approving sandboxed agents. It is dark, low-contrast between layers and dense but calm. Colour is rare: one muted lavender accent and semantic ok/warn/err. Brand lives in the pixel logo, the Geist typeface and the Phosphor icon set.

The look is themeable. Every colour and font is a CSS custom property on `:root` in `style.css`; themes (`themes.py`) override them. The standard theme is "Dusk" (cold blue-black, lavender accent, Geist Mono headings, text at 110%) and is the reference for this file; "Classic" is the neutral-grey variant (`:root` defaults in `style.css`) and "Dystopian" uses the Terminal style, which only changes shapes, icons and cursors via `theme/terminal.css`, so new work must use tokens, never literals.

**Key Characteristics:**
- Flat tonal layering, no decorative shadows.
- Pill buttons, rounded 10-14px containers.
- One lavender accent, used for links, selection and primary affirmative actions.
- Nothing loads from the internet: fonts, icons and cursors are bundled.

## Colors

Cold blue-black neutrals carry the interface; lavender marks interactive emphasis; semantic hues carry status.

### Primary
- **Dusk Violet** (accent #3e3858): accent buttons, count badges, selected tints. Text-on-dark variant **Pale Lavender** (accent-fg #a99bd6) for links; border variant (accent-line #7468a3) for focus.

### Neutral
- **Blue-Black Stack** (#07070b sidebar, #0a0b10 page, #11121a / #171923 / #24222b raised surfaces): depth is stepped lightness, one step per layer.
- **Hairline** (#1c1e28, #22242f): dividers and input strokes.
- **Paper Text** (#ece8e1 primary, #a8a2b3 secondary, #8d88a0 tertiary): three text tiers; do not go below fg-5 for readable text.

### Semantic
- **ok** #8fd18b, **warn** #f2b45a, **err** #ff5d62 with matching dark tinted backgrounds (`--ok-bg`, `--warn-bg`, `--err-bg`) for tags and banners. **danger-btn** #c2414a for destructive buttons.

### Named Rules
**The Token Rule.** Use `var(--...)` from `:root`; a literal colour breaks user themes.
**The One Accent Rule.** Lavender is the only chromatic non-status colour (Classic swaps it for teal).

## Typography

**Body Font:** Geist (system sans in Dusk). **Headings and mono:** Geist Mono in Dusk; Geist Mono for code everywhere. Themes can swap in Geist Pixel, Pixelify Sans, Silkscreen or Departure Mono.

**Character:** neutral grotesque for UI, monospace for logs, code and paths.

### Hierarchy
- **Body** (400, 14px, 1.5): chat and forms.
- **Label** (500, 13-14px): buttons, row titles.
- **Small** (12-13px): tags, badges, hints.
- **Brand name** (600, 15px). Dusk sets text at 110%, so the shipped sizes are about 10% above the px values here.
- **Mono** (13px): code, tool lines, logs.

All sizes are `round(calc(Npx * var(--text-scale)), var(--text-step))` so themes can scale text.

### Named Rules
**The Scaled Size Rule.** Font sizes go through `--text-scale`; never hard-code px font sizes.

## Layout

Fixed-width left sidebar (agents, inbox, settings links) beside a single main column. At 767px and below the phone layout takes over: a bottom bar replaces the sidebar, the sidebar becomes a drawer, and forms go to one column (`.mode-cards`, `.paired-cols`). Spacing is a loose 4/6/8/12/16px rhythm.

## Elevation & Depth

Flat by default; layers are tonal. Shadows appear only for overlays: the mobile drawer (`0 0 40px rgba(0,0,0,.6)`), dropdown lists (`0 12px 32px rgba(0,0,0,.45)`), modals (`0 24px 70px rgba(0,0,0,.6)`).

### Named Rules
**The Flat-By-Default Rule.** A surface never gets a shadow at rest.

## Shapes

Soft and rounded: pills (999px) for buttons, tags and badges; 10px inputs; 12-14px cards and menus; 16px phone cards and popovers; 20-22px sheets and dialogs; 6-8px for small tags and rows; 3-4px for scroll thumbs and keycaps. The Terminal style squares this off.

## Components

### Buttons
- **Shape:** pill. **Default:** surface fill, hover to surface-2. **Primary:** inverted (light fill, dark text). **Accent:** lavender. **Danger:** red-orange. **Ghost:** transparent, fg-3 text. **Small:** 4px 12px.
- **Hover** is wrapped in `@media (hover: hover)`. **Disabled:** surface-3 fill, fg-5 text, not-allowed cursor.

### Inputs / Fields
- Surface fill, 1px line-strong stroke, 10px radius. **Focus:** border becomes accent-line (outline none). Selects use the page-drawn `base-select` picker where supported.

### Icon buttons
- 32px square, 8px radius, transparent, fg-3; hover surface fill. Icons are Phosphor masks tinted with currentColor, drawn at 16px.

### Tags and pills
- Tags: 6px radius, tinted bg per semantic (ok, warn, err, accent, lavender). Pills: 999px, with a 6px status dot.

### Navigation
- Sidebar rows (`.sb-row`) hover to `--hover`; groups collapsible; count badges in accent. Mobile: drawer.

## Do's and Don'ts

### Do:
- **Do** use existing tokens and the Phosphor icons in `theme/phosphor`.
- **Do** guard hover styles with `(hover: hover)` and keep a visible `:focus-visible` ring.
- **Do** keep keyboard navigation working.

### Don't:
- **Don't** load fonts, icons or scripts from a CDN; the CSP forbids it.
- **Don't** add literal colours or font sizes.
- **Don't** add shadows to resting surfaces or a second chromatic accent.
