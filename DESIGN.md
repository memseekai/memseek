---
name: Memseek
description: Instrument-panel dark UI for memory infrastructure — mono headings, a single orange-to-pink gradient, and a per-section hue system.
colors:
  bg: "#0c0e11"
  surface: "#12161b"
  surface-2: "#1a1f27"
  border: "#232a33"
  border-strong: "#33404c"
  text: "#e8eaed"
  muted: "#9aa1ab"
  faint: "#5d6570"
  orange: "#ff7a2f"
  pink: "#ff3d67"
  cyan: "#2fd4c4"
  violet: "#9b87ff"
  green: "#58d68d"
  amber: "#ffb14d"
  bg-light: "#f7f7f5"
  surface-light: "#ffffff"
  surface-2-light: "#f1f2ef"
  border-light: "#e3e4df"
  border-strong-light: "#cfd1ca"
  text-light: "#16191c"
  muted-light: "#55606a"
  faint-light: "#8a939c"
  orange-light: "#e0641c"
  pink-light: "#e02a54"
  cyan-light: "#0e9e90"
  violet-light: "#6d4ae0"
  green-light: "#1f9d57"
  amber-light: "#c07d16"
typography:
  display:
    fontFamily: "'JetBrains Mono', ui-monospace, 'SF Mono', Menlo, Consolas, monospace"
    fontSize: "clamp(2.1rem, 1.5rem + 3vw, 4rem)"
    fontWeight: 700
    lineHeight: 1.1
    letterSpacing: "-0.02em"
  headline:
    fontFamily: "'JetBrains Mono', ui-monospace, 'SF Mono', Menlo, Consolas, monospace"
    fontSize: "clamp(1.5rem, 1.2rem + 1.4vw, 2.3rem)"
    fontWeight: 700
    lineHeight: 1.1
    letterSpacing: "-0.02em"
  body:
    fontFamily: "'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif"
    fontSize: "clamp(.95rem, .92rem + .18vw, 1.05rem)"
    fontWeight: 400
    lineHeight: 1.6
  label:
    fontFamily: "'JetBrains Mono', ui-monospace, 'SF Mono', Menlo, Consolas, monospace"
    fontSize: "clamp(.78rem, .76rem + .1vw, .84rem)"
    fontWeight: 700
    letterSpacing: "0.22em"
rounded:
  sm: "6px"
  md: "9px"
  lg: "14px"
spacing:
  xs: "4px"
  sm: "8px"
  md: "16px"
  lg: "32px"
  xl: "64px"
components:
  button-primary:
    backgroundColor: "{colors.orange}"
    textColor: "#16090b"
    typography: "{typography.label}"
    rounded: "{rounded.md}"
    padding: "0.85rem 1.4rem"
  button-ghost:
    backgroundColor: "transparent"
    textColor: "{colors.text}"
    typography: "{typography.label}"
    rounded: "{rounded.md}"
    padding: "0.85rem 1.4rem"
  button-ghost-hover:
    textColor: "{colors.violet}"
  chip:
    backgroundColor: "transparent"
    textColor: "{colors.muted}"
    rounded: "{rounded.sm}"
    padding: "0.2rem 0.5rem"
  surface-panel:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.text}"
    rounded: "{rounded.lg}"
    padding: "1.5rem"
---

# Design System: Memseek

## Overview

**Creative North Star: "The Instrument Panel"**

Memseek reads like a well-made piece of measurement equipment rendered for the screen: a
near-black chassis, monospaced legends, precisely ruled dividers, and exactly one hot filament of
color running through it. The product's whole promise is that it *shows its work*, so the
interface is built to display state honestly — values, counts, sources, and timestamps are the
ornament. Nothing is decorated that could instead be instrumented.

The density is technical but never cramped. Long-form reasoning gets sans-serif body copy at a
comfortable measure, while every label, count, identifier, and control is monospaced — the type
system itself encodes the difference between prose and data. Headings are monospace too, which is
the system's most opinionated move: it makes even the largest display type read as machine
output rather than marketing.

Color is disciplined. A single orange-to-pink gradient is the brand's one flourish and is spent
almost entirely on the primary action; beyond that, each section of a page claims **one** hue
from a fixed palette and tints only its small parts. The result is a surface that stays neutral
and legible at rest and lights up exactly where meaning is.

**Key Characteristics:**

- Near-black neutral chassis with a true light-mode counterpart; both first-class.
- Monospace headings and labels, sans body — type encodes data vs. prose.
- One gradient, reserved for the primary action and rare headline emphasis.
- A per-section hue system rather than a single global accent.
- Data as ornament: counts, IDs, dates, and citations are the visual interest.

## Colors

A neutral graphite chassis carrying one warm gradient and a fixed set of six signal hues.

### Primary

- **Filament Orange** (`#ff7a2f`): the default accent and the origin of the brand gradient.
  Primary buttons, focus rings, the first section's hue, and hero atmospherics.
- **Filament Pink** (`#ff3d67`): the gradient's far end. Rarely used alone; it exists to make the
  primary action feel lit rather than filled.

### Secondary

The signal hues. Each is a *section* color, not a decoration — a region claims one and tints its
eyebrow, tags, and chips from it.

- **Signal Cyan** (`#2fd4c4`): inline links throughout, and the hue for retrieval/query regions.
- **Signal Violet** (`#9b87ff`): model-backed and inference regions; the ghost button's hover.
- **Signal Green** (`#58d68d`): confirmed, deterministic, zero-cost, or successful states.
- **Signal Amber** (`#ffb14d`): pending, bounded, cautionary, or gap states.

### Neutral

- **Chassis** (`#0c0e11` dark / `#f7f7f5` light): the page ground.
- **Panel** (`#12161b` / `#ffffff`): raised regions and cards.
- **Panel Deep** (`#1a1f27` / `#f1f2ef`): insets, code wells, and nested fields.
- **Rule** (`#232a33` / `#e3e4df`): hairline dividers and default borders.
- **Rule Strong** (`#33404c` / `#cfd1ca`): interactive borders and ghost buttons.
- **Text** (`#e8eaed` / `#16191c`), **Muted** (`#9aa1ab` / `#55606a`), **Faint** (`#5d6570` /
  `#8a939c`): the three-step text ramp. Faint is for legends and units only, never body copy.

### Named Rules

**The One Filament Rule.** The orange→pink gradient appears at most twice per viewport, and the
primary action always owns one of them. It is the only gradient in the system; gradients never
fill a panel, a border, or a background field.

**The Section Hue Rule.** A region sets `--hue` once, and its eyebrow, tags, and chips inherit
it. Never mix two signal hues inside one region for decoration; a second hue in a region must
carry a second *meaning* (e.g. green "no model" against violet "model").

**The Faint Floor Rule.** Faint (`#5d6570`) is legend-only. Any text a visitor must actually read
sits at Muted or above.

## Typography

**Display / Heading Font:** JetBrains Mono (falling back to SF Mono, Menlo, Consolas)
**Body Font:** Inter (falling back to the system sans stack)
**Label Font:** JetBrains Mono

**Character:** Machine-authored, not hand-set. Monospace headings give the page the cadence of
console output; Inter underneath keeps long explanation genuinely readable. The pairing's tension
— rigid heading, humane body — is the system's signature.

### Hierarchy

- **Display** (700, `clamp(2.1rem, 1.5rem + 3vw, 4rem)`, 1.1, `-0.02em`): the page's one
  headline. Constrain to ~20ch so it breaks into a stack of short machine lines.
- **Headline** (700, `clamp(1.5rem, 1.2rem + 1.4vw, 2.3rem)`, 1.1, `-0.02em`): section titles.
- **Title** (700, `clamp(1.1rem, 1rem + .5vw, 1.35rem)`, 1.2): panel and card headings.
- **Body** (400, `clamp(.95rem, .92rem + .18vw, 1.05rem)`, 1.6): explanatory copy. Hold the
  measure to 65–75ch.
- **Label** (700, `clamp(.78rem, .76rem + .1vw, .84rem)`, `0.22em`, uppercase): eyebrows and
  section legends.
- **Micro-label** (700, `.56–.66rem`, `0.08–0.1em`, uppercase): tags, chips, and units. The
  smallest type in the system; monospace only, and never below `.56rem`.

### Named Rules

**The Two-Voice Rule.** Monospace means data, identifier, label, or code. Sans means prose. A
sentence a human wrote reads in Inter; a value the system produced reads in JetBrains Mono. Never
set body copy in mono to look technical.

**The Balanced Heading Rule.** Every heading carries `text-wrap: balance` and a `max-width` in
`ch`. Headings never run the full container width.

## Layout

A single centered column, `max-width: 1180px`, with fluid gutters
(`padding-inline: clamp(1.1rem, 4vw, 2.5rem)`). Content is organized into full-width *regions*
separated by hairline rules rather than by cards floating on a field.

Asymmetric two-column splits (roughly `1fr 1.05fr`) carry the hero and any copy-plus-demonstration
pairing; they collapse to a single column at ~900px. Dense instrument rows may hold 3–7 columns on
desktop and step down to 2, then 1.

Spacing rhythm is a 4px base with an 8/16/32/64 scale. Regions are separated by `clamp(3rem, 7vw,
6rem)`; a heading always carries more space above it than below.

Sticky elements are used sparingly and always with `backdrop-filter: blur(12px)` over a
`color-mix(in srgb, var(--bg) 82%, transparent)` ground: the navigation, and at most one
persistent readout per page.

## Elevation & Depth

Predominantly flat and tonal. Depth comes from *tonal layering* — chassis, panel, panel-deep —
and from hairline rules, not from stacked shadows. Exactly one shadow token exists, and it is
ambient rather than structural.

### Shadow Vocabulary

- **Ambient lift** (`box-shadow: 0 1px 0 rgba(255,255,255,.02) inset, 0 24px 60px -30px rgba(0,0,0,.8)`):
  the only shadow. A hairline top highlight plus a wide, deeply offset soft shadow. In light mode
  the highlight inverts (`rgba(255,255,255,.6)`) and the cast softens
  (`0 20px 50px -30px rgba(20,30,45,.28)`).

Atmospheric radial blooms (large, very low-opacity `color-mix` radials behind the hero) are
permitted as *lighting*, not as elevation.

### Named Rules

**The No-Halo Rule.** Depth always has an offset. A zero-offset colored glow around an element is
never elevation in this system; state is shown with a border color change, a hue tint, or a
tonal step instead.

## Shapes

Softly-squared, machine-cut geometry. Radii are small and consistent: `6px` for chips, tags, and
small controls; `9px` for buttons and inputs; `14px` for panels and large containers. Nothing is
pill-shaped except a deliberate status dot, and nothing is fully square except rules and code
wells.

Borders are 1px hairlines in Rule, stepping to Rule Strong for interactive edges and to a
`color-mix` of the section hue when active. Small square status marks (7px, `2px` radius) tinted
by hue are the system's recurring micro-shape — used as list bullets, chip markers, and legend
keys.

## Components

### Buttons

- **Shape:** softly squared (`9px`), monospace 700, `0.85rem 1.4rem` padding, inline-flex with a
  `0.5rem` gap for a trailing arrow.
- **Primary:** the brand gradient (`linear-gradient(120deg, #ff7a2f, #ff3d67)`) with near-black
  ink (`#16090b`).
- **Hover / Focus:** `filter: brightness(1.07)` plus `translateY(-1px)`; focus shows a 2px orange
  outline offset by 3px.
- **Ghost:** transparent with a Rule Strong border; hover shifts border and text to Violet.

### Chips & Tags

- **Style:** monospace micro-label, uppercase, `0.08–0.1em` tracking, tinted by the region's
  `--hue`. Either borderless with a 7px square hue marker, or outlined with
  `color-mix(in srgb, var(--hue) 45%, var(--border))`.
- **State:** an active chip fills with `color-mix(in srgb, var(--hue) 14%, transparent)` and
  raises its border to the hue.

### Panels / Containers

- **Corner Style:** `14px`.
- **Background:** Panel over the chassis; nested wells step to Panel Deep.
- **Shadow Strategy:** the single ambient lift, and only on genuinely raised elements.
- **Border:** 1px Rule.
- **Internal Padding:** `1.5rem`, tightening to `1.1rem` below 640px.

### Inputs / Fields

- **Style:** Panel Deep ground, 1px Rule border, `9px` radius, Inter at body size, monospace when
  the field holds an identifier or query.
- **Focus:** border steps to the section hue and the 2px orange focus outline appears.

### Navigation

- Sticky, 64px tall, `backdrop-filter: blur(12px)` over an 82% chassis mix, 1px Rule bottom edge.
- Links are monospace at label size in Muted, going to Text on hover with no underline.
- The nav CTA is a compact gradient pill (`8px`).

### Eyebrow (signature component)

A monospace uppercase legend at `0.22em` tracking, colored by the region's `--hue`, preceded by a
`2.4em × 2px` rule of the same hue at 70% opacity. This is the system's section-opening mark and
the primary carrier of the hue system. Because it is the *named* kicker of this system, it is
used consistently at region openings rather than sprinkled arbitrarily.

## Do's and Don'ts

### Do:

- **Do** give every region exactly one `--hue` and let its eyebrow, tags, and chips inherit it.
- **Do** set values, identifiers, counts, dates, and code in JetBrains Mono, and prose in Inter.
- **Do** show real state — counts, IDs, citations, timestamps — as the visual interest of a
  region.
- **Do** ship both themes. Light mode is a designed counterpart with its own palette row, not an
  inversion.
- **Do** honor `prefers-reduced-motion`, keep a visible `:focus-visible` outline (2px orange,
  3px offset), and include a skip link.
- **Do** separate regions with hairline rules and space before reaching for a card.

### Don't:

- **Don't** use the gradient as a background fill, a border, or on more than one non-action
  element per viewport.
- **Don't** add a zero-offset colored glow to indicate elevation or state.
- **Don't** set explanatory body copy in monospace to signal "technical".
- **Don't** introduce a hue outside the fixed palette, or mix two hues in one region without two
  distinct meanings.
- **Don't** build page structure from a grid of same-size icon-heading-text cards.
- **Don't** let Faint carry text the visitor needs to read.
