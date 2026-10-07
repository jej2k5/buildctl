---
version: 1
slug: "ui-buildctl-ui-web-docs-index-html"
primary_target: "ui/buildctl_ui/web/docs/index.html"
related_targets: ["docs/build.py"]
---

# buildctl manual

Mode: Read. One page, rendered from `docs/*.md` by `docs/build.py` (standard library only) into `ui/buildctl_ui/web/docs/`. Served by the console at `/docs/` and published to GitHub Pages.
Readers: the first-time operator (top to bottom), the operator mid-incident (deep link to a stop code from the console), the evaluator (How it works, Guarantees and limits).
Constraints: content only from code and README; every escalation code, command and config key has an anchor, enforced by a test; rendered page must match its source, enforced by a test; inherits DESIGN.md unchanged.

## Direction contract

THESIS: The whole of buildctl on one page you can search with Cmd-F, in reading order, from install to the full reference. It refuses the multi-page docs site with a sidebar tree and a search box.
OWN-WORLD: The console's world from DESIGN.md: signal-plan paper, ink, system sans and mono, square corners, hairline rules, the four signal shapes on stop codes, interlocking condition lists, ink (never blue) for the reading-position marker.
STORY: The reader learns what buildctl does, gets a phase committed, and when a build stops lands on the code's section and does exactly what it says.
FIRST VIEWPORT: Sticky chapter bar across the top (numbered chapters, current one marked in ink). Below, the manual's title, a one-paragraph statement of what buildctl is, and the five-command quick start as the first thing to do. Prose column about 72 characters, tables breaking out to full width.
FORM: One-page manual, position 6 on the re-rolled ordered list, locked by the user; seed key f0b6e632, re-roll round 1 (concept-seed printed the key with --from f0b6e632 --reroll 1; the hand was served as decision page f6f8717d, answer one-page-manual).
FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, DESIGN.md, and every shipping raster carrying its provenance
