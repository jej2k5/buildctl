---
name: buildctl
description: Operator console for a governed, restart-safe build supervisor, drawn as a railway interlocking.
colors:
  ink: "#171c19"
  ink-2: "#434b46"
  ink-3: "#5c655f"
  paper: "#f3f5f1"
  paper-2: "#e8ebe6"
  surface: "#fbfcfa"
  rule: "#d3d8d2"
  rule-strong: "#a3aba4"
  select: "#d5ddd3"
  bar: "#18201d"
  bar-ink: "#e8ece6"
  bar-ink-2: "#a9b4ad"
  bar-rule: "#2f3a35"
  danger: "#c4261d"
  danger-ink: "#a51f17"
  danger-wash: "#fbe9e6"
  caution: "#d49b0f"
  caution-ink: "#8a5d00"
  caution-wash: "#fbf1d8"
  clear: "#2d7a3e"
  clear-ink: "#24683a"
  occupied: "#1f5bd6"
  occupied-wash: "#e6edfc"
  stamp: "#6a2c91"
  stamp-wash: "#f3eafa"
  on-stamp: "#ffffff"
typography:
  headline:
    fontFamily: "system-ui, -apple-system, \"Segoe UI\", Roboto, \"Helvetica Neue\", sans-serif"
    fontSize: "1.25rem"
    fontWeight: 600
    letterSpacing: "-0.01em"
  title:
    fontFamily: "system-ui, -apple-system, \"Segoe UI\", Roboto, \"Helvetica Neue\", sans-serif"
    fontSize: "1rem"
    fontWeight: 600
  body:
    fontFamily: "system-ui, -apple-system, \"Segoe UI\", Roboto, \"Helvetica Neue\", sans-serif"
    fontSize: "0.875rem"
    fontWeight: 400
    lineHeight: 1.45
    fontFeature: "\"tnum\""
  body-sm:
    fontFamily: "system-ui, -apple-system, \"Segoe UI\", Roboto, \"Helvetica Neue\", sans-serif"
    fontSize: "0.8125rem"
    fontWeight: 400
  label:
    fontFamily: "system-ui, -apple-system, \"Segoe UI\", Roboto, \"Helvetica Neue\", sans-serif"
    fontSize: "0.75rem"
    fontWeight: 600
  mono:
    fontFamily: "ui-monospace, \"SF Mono\", SFMono-Regular, Menlo, Consolas, monospace"
    fontSize: "0.92em"
  mono-figure:
    fontFamily: "ui-monospace, \"SF Mono\", SFMono-Regular, Menlo, Consolas, monospace"
    fontSize: "1.5rem"
    fontWeight: 600
    lineHeight: 1
  stop-code:
    fontFamily: "ui-monospace, \"SF Mono\", SFMono-Regular, Menlo, Consolas, monospace"
    fontSize: "1.25rem"
    fontWeight: 600
    lineHeight: 1.2
    letterSpacing: "-0.01em"
  read-title:
    fontFamily: "system-ui, -apple-system, \"Segoe UI\", Roboto, \"Helvetica Neue\", sans-serif"
    fontSize: "2.25rem"
    fontWeight: 650
    lineHeight: 1.15
    letterSpacing: "-0.02em"
  read-lead:
    fontFamily: "system-ui, -apple-system, \"Segoe UI\", Roboto, \"Helvetica Neue\", sans-serif"
    fontSize: "1.25rem"
    fontWeight: 400
  read-chapter:
    fontFamily: "system-ui, -apple-system, \"Segoe UI\", Roboto, \"Helvetica Neue\", sans-serif"
    fontSize: "1.5rem"
    fontWeight: 650
    lineHeight: 1.25
    letterSpacing: "-0.01em"
  read-section:
    fontFamily: "system-ui, -apple-system, \"Segoe UI\", Roboto, \"Helvetica Neue\", sans-serif"
    fontSize: "1.25rem"
    fontWeight: 600
    lineHeight: 1.3
  read-body:
    fontFamily: "system-ui, -apple-system, \"Segoe UI\", Roboto, \"Helvetica Neue\", sans-serif"
    fontSize: "1rem"
    fontWeight: 400
    lineHeight: 1.6
    fontFeature: "\"tnum\""
rounded:
  none: "0px"
spacing:
  s1: "4px"
  s2: "8px"
  s3: "12px"
  s4: "16px"
  s5: "24px"
  s6: "32px"
  s7: "48px"
components:
  button-primary:
    backgroundColor: "{colors.ink}"
    textColor: "{colors.paper}"
    typography: "{typography.body-sm}"
    rounded: "{rounded.none}"
    padding: "9px 14px"
    height: "34px"
  button-primary-hover:
    backgroundColor: "{colors.ink-2}"
  button-quiet:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    rounded: "{rounded.none}"
    padding: "9px 14px"
    height: "34px"
  button-sign:
    backgroundColor: "{colors.stamp}"
    textColor: "{colors.on-stamp}"
    rounded: "{rounded.none}"
    padding: "9px 14px"
    height: "34px"
  button-disabled:
    backgroundColor: "{colors.paper-2}"
    textColor: "{colors.ink-3}"
  input:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    typography: "{typography.mono}"
    rounded: "{rounded.none}"
    padding: "8px 10px"
    height: "36px"
  panel:
    backgroundColor: "{colors.surface}"
    rounded: "{rounded.none}"
    padding: "{spacing.s4}"
  lever-plate:
    backgroundColor: "{colors.paper-2}"
    textColor: "{colors.ink}"
    typography: "{typography.mono}"
    width: "44px"
  tag:
    textColor: "{colors.ink-2}"
    typography: "{typography.mono}"
    padding: "2px 5px"
  stop-danger:
    backgroundColor: "{colors.danger-wash}"
    padding: "16px 24px"
  stop-caution:
    backgroundColor: "{colors.caution-wash}"
    padding: "16px 24px"
  ceremony:
    backgroundColor: "{colors.stamp-wash}"
    padding: "12px 16px"
  rail:
    backgroundColor: "{colors.paper-2}"
    textColor: "{colors.ink-2}"
    padding: "16px 8px"
    width: "184px"
  rail-current:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
  stamp:
    textColor: "{colors.stamp}"
    rounded: "{rounded.none}"
    padding: "3px 8px"
  top-bar:
    backgroundColor: "{colors.bar}"
    textColor: "{colors.bar-ink}"
    height: "52px"
  chapter-bar:
    backgroundColor: "{colors.bar}"
    textColor: "{colors.bar-ink-2}"
    typography: "{typography.body-sm}"
    padding: "0 12px"
    height: "52px"
  chapter-bar-current:
    textColor: "{colors.bar-ink}"
  chapter-plate:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    typography: "{typography.mono}"
    rounded: "{rounded.none}"
    padding: "5px 6px"
    width: "30px"
  stop-entry:
    backgroundColor: "{colors.surface}"
    rounded: "{rounded.none}"
  stop-entry-danger:
    backgroundColor: "{colors.danger-wash}"
    padding: "8px 16px"
  stop-entry-caution:
    backgroundColor: "{colors.caution-wash}"
    padding: "8px 16px"
  stop-entry-unknown:
    backgroundColor: "{colors.paper-2}"
    padding: "8px 16px"
  stop-index:
    backgroundColor: "{colors.surface}"
    rounded: "{rounded.none}"
    padding: "{spacing.s4}"
  reference-table-wide:
    backgroundColor: "{colors.surface}"
    rounded: "{rounded.none}"
    width: "min(1100px, calc(100vw - 64px))"
  code-block-wide:
    backgroundColor: "{colors.surface}"
    rounded: "{rounded.none}"
    width: "min(1100px, calc(100vw - 64px))"
  inline-code:
    backgroundColor: "{colors.paper-2}"
    textColor: "{colors.ink}"
    typography: "{typography.mono}"
    padding: "1px 4px"
---

# Design System: buildctl

## Overview

**Creative North Star: "Railway Interlocking"**

The console is a signal box. The build is a line of blocks; each action is a lever that unlocks only when its interlocking table proves every precondition. The operator reads the build's aspect at once, reads which condition holds or blocks, and pulls the one lever the kernel will accept. Everything is drawn on signal-plan paper: cool off-white with near-black ink in light mode, a blackened panel tone in dark mode, with hairline rules, square corners and dense tables.

Colour is scarce and semantic. Four signal aspects (danger, caution, clear, unknown) are drawn as distinct shapes so state survives colour loss, on a 16-colour terminal and under colour-vision deficiency. Two further colours are reserved for single meanings: occupancy blue for the operation actually in flight, and stamp violet for a human signature. Everything else is ink on paper.

The same world runs in three surfaces. The web console is static HTML/CSS/JS on system fonts; the Textual TUI mirrors the dark palette and draws the same shapes as Unicode glyphs; the manual (one generated page, served at `/docs/`) sets the same tokens for reading. The system refuses the CI-dashboard arrangement of status pills over a log pane.

**Key Characteristics:**
- Shape carries state; colour reinforces it.
- One colour per reserved meaning: occupied blue, stamp violet.
- System sans for UI, system mono for digests, nonces, commands and numbers; no web fonts.
- Square corners, 1px hairlines, flat surfaces, dense tables with tabular figures.
- The interlocking table (lever plate, conditions, lock note) is the signature component.
- Light and dark themes share every token name; the TUI uses the dark values.
- Two modes share one world: Operate surfaces (console, TUI) keep the dense ramp; Read surfaces (the manual) add a larger reading ramp and a 72ch column.

## Colors

Signal-plan paper and ink, four signal aspects, and two reserved single-meaning colours. Frontmatter values are the light theme; every token has a dark counterpart under `prefers-color-scheme: dark` and `[data-theme="dark"]` (recorded in `.impeccable/design.json` as `colorMeta.<token>.dark`). The `bar` family is dark in both themes.

### Primary
- **Signal-Box Ink** (`ink`): text, primary button fill, completed track, the current-page marker in the rail, lever-plate number border, focus ring. The primary action colour is ink, not a hue.

### Secondary (the signal aspects)
- **Danger Red** (`danger`, text `danger-ink`, field `danger-wash`): stopped, blocked condition, failed operation, a checkpoint ahead not yet approved, spend over 95% of cap, deadline passed. Always drawn as a filled square.
- **Caution Amber** (`caution`, text `caution-ink`, field `caution-wash`): the build is waiting for the operator: a pending approval, a pause, a sandbox verification gone stale, an untrusted config. Always drawn as a filled diamond.
- **Clear Green** (`clear`, text `clear-ink`): held condition, completed operation, verified trust or sandbox, phase completed. Always drawn as a filled circle.
- **Unknown** (stroke `ink-2`, dashed): outcome unknown, condition checked only at call time, run died. Drawn as a dashed hollow circle; no hue of its own.

### Tertiary (reserved meanings)
- **Occupancy Blue** (`occupied`, field `occupied-wash`): only an operation actually in flight (status started and the lock held): the occupied track block, the "in progress" bar glyph, the held-lock glyph, the phase-deadline bar and the current phase's cost bar while running.
- **Stamp Violet** (`stamp`, field `stamp-wash`): only a human signature: the ceremony panel border and wash, the evidence underline, the Sign button, the SIGNED stamp, the signature receipt, and SIGNED marks in the ledger.

### Neutral
- **Signal-Plan Paper** (`paper`): page ground.
- **Plate Grey** (`paper-2`): the rail, lever plates, table headers, day rows, hovered and selected rows, disabled buttons.
- **Sheet** (`surface`): panels, tables, the line diagram, inputs.
- **Ink 2 / Ink 3** (`ink-2`, `ink-3`): secondary text and condition detail / muted captions, kbd hints, legends.
- **Hairline / Strong Rule** (`rule`, `rule-strong`): 1px borders and dividers / table header rule, quiet-button border, the dashed track ahead, the pending glyph.
- **Selection** (`select`): text selection, line-diagram hit hover, the in-place cell flip.
- **Signal-Box Bar** (`bar`, `bar-ink`, `bar-ink-2`, `bar-rule`): the top bar in both themes, and its meter track.

### Named Rules
**The Shape Carries State Rule.** Every aspect has its own silhouette: danger square, caution diamond, clear circle, unknown dashed hollow circle, occupied horizontal bar, pending thin line. Never show a state by colour alone, and never draw two states with the same shape. The hollow ink-stroked square of a milestone station is track furniture, not a signal, and stays unfilled.

**The Occupied Block Rule.** Occupancy blue means an operation is in flight right now. A finished, failed or idle operation is never blue.

**The One Stamp Rule.** Stamp violet appears only where a human signs or has signed. No other control, link, chart or highlight uses it.

**The Waiting For You Rule.** Amber means the build is waiting on the operator. It is not a generic warning tint.

## Typography

**Body Font:** system-ui (with -apple-system, Segoe UI, Roboto, Helvetica Neue, sans-serif)
**Mono Font:** ui-monospace (with SF Mono, SFMono-Regular, Menlo, Consolas, monospace)

**Character:** The operating system's own faces, as on an instrument panel. Sans names things; mono carries anything a machine produced or a person must match exactly.

### Hierarchy
- **Headline** (600, 1.25rem, -0.01em): view titles (Line, Stop, Phases, Ledger, Setup), followed inline by a muted sans caption.
- **Title** (600, 1rem): section headings and lever names; the inline `small` caption beside a section heading is 400, 0.8125rem, `ink-3`.
- **Body** (400, 0.875rem, 1.45): all running UI text. Tabular figures on the whole body.
- **Body small** (0.8125rem): conditions, tables, buttons, definition lists.
- **Label** (600, 0.75rem): table headers, top-bar fact labels.
- **Mono** (0.92em of context; weight follows context, 500 in inputs, evidence and tags, 700 in lever plates and severity chips): the DEMO flag, digests, tree hashes, nonces, commands, operation IDs, escalation codes, condition detail, phase and lever numbers, money.
- **Mono figure** (600, 1.5rem, 1): the stopped phase number; **Stop code** (600, 1.25rem mono): the escalation code.

### Reading Hierarchy (Read surfaces only)
The manual uses the same faces with a larger ramp. These sizes belong to Read surfaces only; the console and TUI keep the Operate ramp above.
- **Read title** (650, 2.25rem, 1.15, -0.02em, balanced wrap; 1.75rem at 820px and below): the manual's one title.
- **Read lead** (400, the `t-xl` step 1.25rem, `ink-2`, max 62ch; the `t-lg` step 1rem at 820px and below): the one-paragraph statement under the title.
- **Read chapter** (650, 1.5rem, 1.25, -0.01em): chapter headings, led by the chapter plate.
- **Read section** (600, the `t-xl` step 1.25rem, 1.3): section headings inside a chapter, 32px above, 8px below; a hover-revealed `#` permalink in `ink-3` follows the text.
- **Read body** (400, 1rem, 1.6, tabular figures): running prose at a 72ch measure. Tables and code blocks inside the column stay at 0.8125rem; `strong` is 650.
- **Inline code** (mono 0.9em on a `paper-2` field, 1px by 4px padding): a span of 20 characters or fewer never wraps. A longer one wraps only between groups: a flag keeps its value (`--nonce <n>`), and a `<placeholder>` or quoted string stays whole.

### Named Rules
**The Machine Text Rule.** If the kernel produced it or the operator must type it back, set it in mono.

**The No Web Fonts Rule.** System stacks only. Nothing is downloaded.

**The Plan Lettering Rule.** Capitals belong to machine vocabulary and plan lettering only: escalation codes, severity levels, the SIGNED word and the DEMO flag (all mono), and station names on the line. Sans UI labels, headings and captions are never uppercased; there are no kickers or eyebrows.

**The Two Ramps Rule.** Operate surfaces use the dense ramp (0.75rem to 1.5rem, weight 600). Read surfaces add the reading ramp (body 1rem/1.6, headings to 2.25rem, weight 650). Neither ramp crosses into the other's surface.

**The Unbroken Token Rule.** A command, key or code never breaks inside a word, and a flag never parts from its value. Wrap between groups; let a table stack rather than split a name.

## Layout

A fixed app frame: a sticky top bar (52px), a 184px left rail, and a main column padded 24px / 32px / 48px. Main content splits into two columns (1.55fr and a minimum 300px 1fr) for "next move" against "current call", and into a detail grid (1fr / 1.2fr) for evidence against levers. The phase line spans the full main width.

Spacing steps through `s1`–`s7` (4, 8, 12, 16, 24, 32, 48px). Sections sit 32px apart; panels pad 16px; table cells pad 7–8px by 10px. Density is deliberate: the operator glances between other work.

At 1100px the columns stack and the top bar's facts wrap onto a second row under a `bar-rule` hairline. At 820px the rail becomes a horizontal, sticky, scrolling strip under the bar (kbd hints and footer hidden, the current-page marker moves to the bottom edge), the Stop panel's phase block moves below the code, and readiness and finding rows collapse to one column.

### Read surfaces
The manual has no rail. Main is a centred reading column, 72ch plus 32px padding each side, padded 32px / 32px / 48px (24px / 16px / 48px at 820px and below). Chapters sit 48px apart; the title block closes with a `rule-strong` hairline and 24px below. Code blocks and ordinary tables stay inside the column. Two things break out, centred on the page at `min(1100px, 100vw - 64px)` (`100vw - 32px` at 820px and below): a table marked wide, and a code block with a line longer than 76 characters, which `docs/build.py` marks wide. Shell blocks are reflowed to fit the column instead (comments on their own lines, `\` continuations), so only data such as the build.yaml example breaks out. At 820px and below every table stacks, and code wraps instead of scrolling: each line hangs 4ch under its own start, so a wrapped continuation never reads as a new command. Anchored targets scroll clear of the sticky bar (116px offset; 150px at 820px and below, where the bar takes two rows).

**The Reading Column Rule.** Prose never runs wider than about 72ch. Only wide reference tables and code blocks whose lines would clip break out, centred, up to 1100px; nothing else leaves the column, and nothing is ever clipped.

## Elevation & Depth

Flat. No drop shadows anywhere. Depth is tonal: `paper` ground, `paper-2` plates and rails, `surface` sheets, separated by 1px rules. The only `box-shadow` uses are inset hairlines: a 1px inset ring on the current rail item and a 2px inset ink edge on a selected table row. The top bar is the one dark mass, sticky over the page.

### Named Rules
**The Flat Plan Rule.** Surfaces sit on the plan, not above it. Separate with tone and hairlines, never with shadows.

## Shapes

Square corners everywhere (0px), including inputs, which reset the browser radius explicitly. Borders are 1px hairlines; emphasis steps up in weight rather than in radius: 1.5px on the lever-number box, 2px on the CRITICAL severity chip, and a 2px border plus 1px outline ring offset 2px on the stamp. The only non-rectangular shapes are the signal glyphs (diamond, circles) and the line diagram's tracks and posts.

Dashes carry three distinct meanings, each fixed: a dashed hollow circle or dashed stop panel border means outcome unknown; a thin dashed track means phases not yet run; a dashed ink box is the DEMO flag. Dotted hairlines separate conditions inside a lever.

## Components

### Buttons
Standard controls, ink on paper.
- **Shape:** square (0px), min height 34px, 9px by 14px padding, 600 weight at 0.8125rem.
- **Primary:** ink fill, paper text. Used for a lever the kernel will accept without ceremony.
- **Hover / Focus / Active:** hover lightens to `ink-2`; 0.15s transitions on the shared ease; active nudges down 1px; focus is a 2px ink outline offset 2px.
- **Quiet:** `surface` fill, ink text, `rule-strong` border (ink on hover). Used for levers that open a ceremony, and for Cancel and Dismiss.
- **Sign:** stamp fill and border, white text; brightens on hover. Appears only inside a ceremony and is disabled until the typed evidence matches.
- **Disabled:** `paper-2` fill, `ink-3` text, `rule` border, not-allowed cursor. A locked lever's button is disabled and described by its lock note.

### Chips
- **Tag:** mono 0.75rem, `ink-2` on transparent, `rule-strong` 1px border ("paid", "runs detached").
- **Severity:** mono 700 0.75rem, 62px minimum, 1px currentColor border. CRITICAL is `danger-ink` on `danger-wash` with a 2px border; HIGH `danger-ink`; MEDIUM `caution-ink`; LOW `ink-3`. The word always carries the severity.
- **DEMO flag:** mono 600 0.75rem, `bar-ink` text in a 1px dashed `bar-ink` box on the top bar. No fill, no hue.

### Cards / Containers
- **Corner Style:** square (0px).
- **Background:** `surface` panels on `paper`.
- **Shadow Strategy:** none (see Elevation & Depth).
- **Border:** 1px `rule`.
- **Internal Padding:** 16px.
- **Stop panel:** a full-width block at the top of main when the build halts: 1px aspect-coloured border on the aspect's wash (danger, caution, or a dashed `ink-2` border on `paper-2` for unknown), a large aspect glyph, the escalation code in mono, its meaning, the remedy, and the phase number set large in mono at right.

### Inputs / Fields
- **Style:** mono 500 0.875rem, `surface` fill, 1px `rule-strong` border, square, min height 36px. Labels are 600 sans 0.8125rem above; hints 0.75rem `ink-3` below.
- **Focus:** border and a 1px outline both turn ink.
- **Error:** `aria-invalid` turns the border `danger`.

### Navigation
- **Top bar:** `bar` mass carrying, left to right, the lowercase mono wordmark `buildctl`, repository and branch@commit, the build aspect between two `bar-rule` hairlines (large glyph, word, code in mono), then facts: spend versus cap meter (4px bar, `bar-ink` fill, danger above 95% of cap), sandbox age, trust digest, lock holder.
- **Rail:** `paper-2` column with 18px stroked line icons, labels at 500 weight in `ink-2`, and kbd hints 1–5. Hover fills `rule`; the current page sits on `surface` with an inset hairline and a 3px ink bar on its left edge. The Stop item carries a small danger square, or caution diamond, when there is a stop.
- **Tabs:** sans 500 0.8125rem, `ink-2`, 2px ink underline and 600 weight when selected, counts in mono `ink-3`. The tab panel continues the tab bar's rule with no top border.

### The Interlocking Table (signature)
Each lever is a row in a table on `surface` with a 1px `rule` border.
- **Lever plate:** a 44px `paper-2` column at left holding the lever's fixed, kernel-assigned number, zero-padded, in a mono 700 box with a 1.5px ink border. The number never changes with position. A locked lever greys the box (`rule-strong` border, `ink-3` text) and shows a lock icon beneath it.
- **Lever head:** the lever name as a title with any tags, a one-line summary in `ink-2` (max 60ch), and the action button at right.
- **Conditions:** a ruled list under the head, each row a 12px aspect glyph, the condition label, and mono `ink-3` detail beneath. Held is a clear circle; blocked is a danger square and the whole row turns `danger-ink` at 500 weight; checked at call time is the dashed unknown circle. Rows separate with dotted hairlines.
- **Lock note:** below a locked lever, a lock icon and "Locked by one condition: …" (or N conditions) in `ink-3`, naming the blocking conditions.
- **Order:** only levers the kernel offers are shown. Levers stack inside one table; separate tables are 12px apart.

### The Evidence Ceremony
Trust-critical levers do not act on click. The quiet button opens a ceremony inside the lever: a 1px stamp border on `stamp-wash`, a caption naming what is being signed, the full evidence in mono with the prefix to type set bold ink and underlined 2px in stamp, a mono input asking for exactly that prefix (or the phase number, or the operation kind), a hint line that states whether it matches, and the Sign button, disabled until it does, beside a quiet Cancel. The console sends the nonce it displayed; the operator never types a nonce. On success the stamp appears and a receipt (stamp border on `stamp-wash`, stamp, what was signed, the command, Dismiss) sits above the levers.

### The Stamp
Mono 700 0.75rem, uppercase "SIGNED" with 0.06em tracking, followed by sentence-case detail at 500 weight. 2px stamp border with a 1px stamp outline offset 2px, stamp-coloured text, no fill. Freshly applied stamps settle from 1.35x scale over 0.45s; ledger stamps are static.

### The Phase Line
A track diagram on a `surface` panel. Completed phases collapse into one 3px ink stretch labelled with its count. The current block is a 7px stroke in its aspect (occupied, danger, caution, or dashed `ink-2` for unknown). Phases ahead are a 2px dashed `rule-strong` track with numbered joints. Milestones are square station marks labelled in capitals with their number. Checkpoint signals stand on posts above the track before gated phases: danger until approved, caution while waiting, clear once approved. A legend under a hairline names every shape. Phase numbers are mono 10px; the current one is ink 700.

### Data Tables
Dense, sticky headers on `paper-2` (600, 0.75rem, `ink-2`, `rule-strong` underline), 7px by 10px cells, 1px `rule` row lines, right-aligned numeric columns, mono for IDs and times. Clickable rows hover to `paper-2`; the selected row adds a 2px inset ink edge. A cell whose value changed flips from `select` to transparent over 0.9s instead of the table re-rendering.

### Terminal UI
The Textual TUI uses the dark palette (`#111513` background, `#161b18` surface, `#181d1a` panel, `#e6ebe5` ink) and draws the aspects as ■ danger, ◆ caution, ● clear, ◌ unknown, ▬ occupied, · pending, each in its dark-theme colour. Levers are bordered blocks with a boxed, zero-padded number; the stop panel takes a heavy aspect-coloured border (dashed for unknown); the ceremony is a modal with the evidence prefix bold and underlined and a Sign button that stays disabled until the typed value matches.

### The Chapter Bar (Read)
The manual's navigation is the console's top bar carrying chapters instead of facts: `bar` mass, sticky, 52px, the mono wordmark `buildctl` with a `bar-ink-2` "manual" caption, then a numbered list of chapters. Each chapter link is sans 0.8125rem `bar-ink-2` led by its number in mono 600 0.75rem; hover lifts to `bar-ink`. The chapter being read (the last whose top has passed 40% of the viewport) turns `bar-ink` with a 2px `bar-ink` underline, the ink of the bar, never occupancy blue. The list scrolls sideways when it overflows and keeps the current chapter in view. At 820px and below the bar takes two rows, wordmark above and chapters under a `bar-rule` hairline, with 44px tap targets.

### The Chapter Plate (Read)
Every chapter heading leads with its number in a plate, the same plate language as a lever number: mono 700 at 0.8125rem, a 1.5px ink border, square, 5px by 6px padding, 30px minimum width, centred. The chapter plate sits on `surface` rather than inside a `paper-2` column. The same number labels the chapter in the bar. A visually hidden ". " follows the number in both places, so a screen reader says "1. Start", not "1Start". The current chapter carries `aria-current="location"`; the others carry no `aria-current` at all.

### The Stop-Code Entry (Read)
The console's Stop panel, small, one per escalation code. A `surface` block with a 1px `rule` border, 12px apart. Its header band carries the aspect's wash (`danger-wash`, `caution-wash`, or `paper-2` for unknown) over a `rule` hairline, 8px by 16px: the 16px signal glyph, then the code in mono 700 at 1rem, then the permalink. The body is the meaning, then a line that opens with a bold "Clear it:" and gives the remedy and command. The entry closes with "↑ All stop codes" (0.8125rem, `ink-2`; the arrow is hidden from screen readers), back to the index: a reader the console dropped on one code is one step from the rest. When the entry is the URL target it takes a 2px ink outline offset 2px and its band settles from `select` to its wash over 1.2s on the shared ease. Unlike the console Stop panel, the border stays `rule` for every aspect and is never dashed; the band alone carries the aspect.

### The Stop-Code Index (Read)
Every stop code at a glance, for a reader who arrives mid-incident. A `surface` panel with a 1px `rule` border and 16px padding, set in balanced columns (minimum 260px, 24px gap). Codes group under sans 600 0.8125rem `ink-2` labels (Waiting for you, Stopped, and so on). The labels are plain text that names its list, not headings, because the chapter already has a heading for each group below it. Each code is a link of its 16px signal glyph and the code in mono 0.8125rem, underlined in `rule-strong` (ink on hover). Here the glyph is hidden from screen readers, since the group label already names the aspect; everywhere else a glyph is announced. Groups never split across columns.

### Code Links (Read)
A link to a stop code from prose or a table carries the code's 12px signal glyph before the code, so its aspect reads before the page jumps.

### Reference Tables (Read)
The console's data table, set for reference: `surface` in a 1px `rule` frame, `paper-2` header at 600 0.75rem `ink-2` over a `rule-strong` line, 8px by 12px cells, 0.8125rem at 1.5. The name column is nowrap and 1% wide so the meaning takes the rest; a code in it drops its field and sets 600. A row that is the URL target fills `select`. Wide reference tables (commands, config keys) break out of the column per the Reading Column Rule. At 820px and below every table, wide or not, loses its header row and stacks: each row a block, the name whole on its own line and the meaning under it, and from the third column on each cell leads with its column label in 600 `ink-2` ("Clear it: …"), so no remedy hides off the right edge. A row with an anchor carries its own permalink after the name.

### Code Blocks (Read)
`surface` in a 1px `rule` frame, mono 0.8125rem at 1.6, 12px by 16px padding. With script, the block gains a band on top: its language in 0.75rem `ink-3` on the left, and a quiet Copy button on the right (sans 500 0.75rem, `ink-2`, transparent with a transparent border that turns `rule` on hover, 32px tall). Copy writes the block's exact text and says "Copied" for 2s, in the button and through a polite live region. Without script the block has no band and reads the same. Long lines follow the Reading Column Rule.

### Permalinks (Read)
A "#" in `ink-3` after every h3, h4 and anchored table row, named for its target ("Link to Quick start"), never a generic "Link to this section". It appears on hover of its heading or row and on focus; on a device without hover it is always shown.

### The Version Stamp (Read)
Under the title, mono 0.8125rem `ink-3`: "For buildctl 0.1.0", read from `buildctl/__init__.py` by `docs/build.py`. The Pages workflow appends " at <short commit>" to the published copy only, because a committed page cannot name its own commit.

## Do's and Don'ts

### Do:
- **Do** draw every state with its own shape (square, diamond, circle, dashed hollow circle, bar, line) and name it in text or an accessible label.
- **Do** reserve `occupied` for an operation that is started with the lock held.
- **Do** reserve `stamp` for the ceremony, the Sign button, the SIGNED stamp and the signature receipt.
- **Do** set digests, nonces, commands, codes, IDs and numbers in the system mono stack, with tabular figures.
- **Do** keep corners square and borders 1px; step weight (1.5px, 2px) for emphasis.
- **Do** give each lever its fixed kernel number in a plate, list its conditions as held, blocked or checked at call time, and explain a lock in a lock note.
- **Do** require typed evidence before a trust-critical lever acts, and send the displayed nonce rather than asking for it.
- **Do** update table cells in place and mark the change with the 0.9s selection flip.
- **Do** keep the TUI on the dark palette with the same glyph per aspect.
- **Do** set Read surfaces on the reading ramp (body 1rem/1.6, title 2.25rem 650) in a centred 72ch column, and keep the console on the Operate ramp.
- **Do** mark the current chapter with a 2px `bar-ink` underline on the bar, and lead every chapter with its numbered plate.
- **Do** draw every stop code with its aspect glyph, in the entry band, the index and every link to it.

### Don't:
- **Don't** convey state by colour alone, or reuse a filled signal shape for another meaning (hollow station squares on the line are the one native exception).
- **Don't** use occupancy blue for links, focus, selection, charts or completed work.
- **Don't** use stamp violet anywhere a human is not signing.
- **Don't** use amber as a general warning tint; amber means the build is waiting for the operator.
- **Don't** load web fonts or introduce a display face.
- **Don't** add rounded corners, drop shadows or gradients.
- **Don't** add decorative icons; icons are stroked line drawings for navigation and the lock, and status is drawn only with the signal shapes.
- **Don't** arrange status pills over a log pane.
- **Don't** uppercase sans labels, headings or captions, and don't add kickers or eyebrows; capitals are for codes, severities, SIGNED, DEMO and station names.
- **Don't** mark reading position, links or the current chapter in occupancy blue.
- **Don't** let anything but a wide reference table or an over-long code block leave the reading column, and don't let a command, key or code break inside a word or part a flag from its value.
- **Don't** carry the reading ramp into the console or the TUI.
