---
version: 1
slug: "ui-buildctl-ui-web-index-html"
primary_target: "ui/buildctl_ui/web/index.html"
related_targets: ["ui/buildctl_ui/tui.py"]
---

# Operator console (web) and TUI

Mode: Operate. Surfaces: `buildctl_ui/web/` (local web console) and `buildctl_ui/tui.py` (Textual), one information architecture.
Audience and job: the engineer accountable for what lands on `main`; set up, watch a run, handle a stop, review history. Success: why it stopped and the correct action, in under a minute, with every CLI safety property intact.
Constraints: reads via the kernel `inspect` read model; every mutation shells out to the `buildctl` CLI; 127.0.0.1 plus per-launch token on every API call; Host and Origin checks; runs spawn detached.
Decisions: UI submits the nonce it rendered; the human types the evidence prefix (tree hash, SHA, phase, operation kind). No in-UI editing of governed files in v1.

## Direction contract

THESIS: Governed autonomy shown as railway interlocking. Every action is a lever that unlocks only when its interlocking table proves each precondition. It refuses the CI-dashboard arrangement of status pills over a log pane.
OWN-WORLD: Signal-plan paper (cool off-white, near-black ink) in light mode; a blackened panel tone in dark. Four signal aspects drawn as SVG shapes: danger red square, caution amber diamond, clear green circle, unknown dashed hollow circle. One reserved occupancy blue for the in-flight operation only. System sans for UI, system mono for digests, nonces, commands, phase numbers in lever-plate boxes. Hairline track lines, dense tables, square corners.
STORY: The operator sees the build's aspect at once, reads which condition holds or blocks, and pulls the one lever the kernel will accept, with ceremony where it protects.
FIRST VIEWPORT: Top bar (repo, branch, build aspect, spend vs cap meter, isolation age, trust, lock holder). Left rail nav (Line, Stop, Phases, Ledger, Setup). Main: the phase line across the full width (completed stretches collapsed and counted, milestones as stations, checkpoint signals ahead of gated phases, the current block occupied); below it, at left, the interlocking table for the next move with its levers; at right, the in-flight or stopped operation card. When stopped, the Stop panel takes the main area's top.
FORM: Railway interlocking (track diagram plus interlocking table), position 1 on the ordered list, chosen as IMPECCABLE'S PICK; seed key cffb4297. Signature move: the interlocking table. Raises kept: fixed columns that update cells in place; one colour reserved for the active leg; a single stamp mark for human signatures.
FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, DESIGN.md, and every shipping raster carrying its provenance
