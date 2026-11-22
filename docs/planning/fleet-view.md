# Spec: a fleet view across every build on a machine

Status: specified 2026-11-22 against `3ecb0e4`. Not built.

The console shows one build. An operator running several builds at once checks each console in turn, or writes their own overview by scraping `buildctl inspect` and `git log` across repos. One such overview, a static page rebuilt each morning, went stale within hours of four builds moving. It also reported a trust-file read error as "needs trust init", so it asked the operator for something nothing needed. The fleet view moves that overview into `buildctl-ui`, reading every build through the same code the per-build console uses.

Decided 2026-11-22, by JJ:

| # | Question | Chosen | Rejected |
|---|---|---|---|
| 1 | Where does a cross-build overview live? | In `buildctl-ui`, as the admin view across all builds. | A separate per-machine dashboard that re-derives build state. |
| 2 | How does it find builds? | No hard-coded path. Builds register themselves; folders to scan are an explicit option. | A fixed folder such as `~/Code`. |

Decided by Claude, open to change:
- Only build state belongs here. Notes, knowledge bases, agent reports and anything else personal stay in the operator's own tools, which can link to the fleet view.
- The fleet home is read-only. Levers stay in each build's own console, behind the same nonce, generation and tree checks as today.
- Discovery drafts appear as rows next to live builds and open the existing draft view.
- Keeping a console running in the background is the operator's choice, not something `buildctl-ui` installs.

## The question

Can one page answer "what needs me, across every build on this machine?" without deriving any state the supervisors don't record?

## What it shows

`buildctl-ui --all` opens the fleet home. The per-build console keeps working exactly as before: `buildctl-ui` inside a repository still opens that build.

**The lede.** One plain sentence built from the rows, then one line per thing that waits on the operator, as on the Plan view. For example: "2 builds running. ingest waits on your checkpoint before phase 9. 1 draft is ready to finalize." With nothing waiting it says so. It counts builds, not phases.

**One row per build:**

| Column | Source |
|---|---|
| Signal | The build aspect the console already computes: occupied (running), caution (waiting on the operator), danger (stopped), clear (complete or idle), unknown. |
| Name | The repository folder name, linking to that build's console. |
| Where it is | "Building phase 7 of 12", "Stopped at phase 0", "Complete". |
| Why | For a stop, the escalation code and its one-line meaning. For a checkpoint, the operator's note for it from `docs/ROADMAP.md`, if any. |
| Next checkpoint | The next phase behind a checkpoint, from `build.yaml`. |
| Spend today | Spend against `max_daily_cost_usd`, from the budget ledger. |
| Last activity | The newest audit event's time. |

**Draft rows:** one per open buildctl-discover session, showing the draft's gate result (open calls, failing checks, or ready to finalize) and linking to `--session` view.

**Unreadable rows:** a build whose state can't be read shows the actual reason (a missing repo, unreadable state, a kernel mismatch), never a guessed remedy. A registry entry whose repo has moved shows the path it expected.

Rows sort by what needs the operator first: caution, then danger, then unknown, then occupied, then clear.

## Finding builds

The fleet view takes builds from three sources and merges them by real path.

1. **The registry (target).** Each buildctl state folder is named by a hash of the repo's real path, and none of them records the path. The kernel writes `repo.json` (`{"path": "<realpath>", "recorded_at": "<iso>"}`) into the state folder at `bootstrap` and `trust init`. buildctl-discover does the same in its session folder at `start`. The fleet view lists state folders and reads `repo.json`, with no configuration. Moving a repo leaves a stale entry, shown as unreadable until `trust init` runs in the new place.
2. **Scan folders (until the registry ships, and for repos never trusted).** `--scan DIR` (repeatable) or `BUILDCTL_SCAN_PATHS` (colon-separated) lists folders whose immediate children are checked for `build.yaml`. There's no default folder: with nothing set, it scans the current directory only.
3. **Explicit repos.** `--repo DIR` (repeatable) adds named repositories.

Writing `repo.json` is a kernel change, so it changes the kernel digest and every build needs `trust init` once. Ship it with other kernel fixes, not alone. Scanning works without it.

## How it reads each build

- The server holds one `Build` per repository and calls the same `model()` the per-build console uses, in process. It doesn't run `buildctl inspect` as a subprocess per row.
- The home polls a summary every 5 seconds rather than every 2. A build's own console keeps its 2-second refresh.
- A build that fails to read becomes an unreadable row and never breaks the page.
- PRODUCT.md's rule holds: the fleet view never derives state the supervisor doesn't record. "Waiting on you" means the supervisor recorded a pending approval or a caution escalation, and nothing else.

## Routing and security

- One server, one per-launch token, bound to 127.0.0.1 as today. The token covers every build the server lists, so the launch line says how many builds it opened.
- Routes gain a build segment: `#/b/<id>/plan`, where `<id>` is the state folder hash. The existing per-build routes stay valid when one build is open.
- Pulling a lever in a build opened from the fleet uses that build's own checks (nonce, generation, tree). The home has no levers.

## Design

A new DESIGN.md section, "The Fleet (Operate)":
- The lede is the Plan's status panel, unchanged.
- Rows use the Interlocking Table: signal shapes carry state, square corners, flat surfaces, mono for codes and phase numbers.
- Caution (the amber diamond) appears only for real asks. A setup problem or unreadable build is danger or unknown, never caution.
- No per-build colours: the name is the identity.

## Tests

- Model: a fleet of demo builds (blocked, checkpoint, running, complete) plus one unreadable state folder, one stale registry entry and one draft. Check the rows, the order and the lede.
- Discovery: registry entries, scan folders and explicit repos merge by real path, and nothing outside the configured sources is read.
- The lede counts builds, not phases, and never shows caution for an unreadable build.
- Routing: a lever pulled through `#/b/<id>/...` reaches only that build and keeps its checks.
- Kernel: `bootstrap` and `trust init` write `repo.json`, and an existing state folder without one still loads.
- `--demo fleet` gives the screenshots and docs a scenario.

## Out of scope

- Builds on other machines.
- Starting, pausing or approving from the home page.
- The terminal UI. It follows once the web view settles, as the feed did.
- Installing a background service. The manual can show a launchd or systemd example.

## Open questions

- Should a fleet console show only builds whose state folder is under the current user's state root, or also accept `--state-root` for a shared machine?
- Is one token across every build acceptable, or should each build's levers need their own confirmation the first time they're pulled from the fleet?
