# buildctl

A supervisor for agent-built software. You write a roadmap of phases. buildctl has one agent implement each phase, another review it adversarially, runs your acceptance commands in a sandbox, and commits only what passed. A human approves the phases that matter.

buildctl owns the state, the git history and the budget. Agents never touch any of them. They work in a disposable clone with no remote, and their output is checked before a single byte reaches your repository.

It was extracted from the `build/` supervisor in the original project, which drove that product through 45 phases.

## Requirements

- macOS. Agents run under `sandbox-exec`; there is no Linux backend yet.
- Python 3.11 or newer.
- The `codex` CLI (implementer) and the `claude` CLI (reviewer), both logged in: `codex login`, `claude auth login`.

## Install

```sh
pip install -e /path/to/buildctl     # or: pipx install /path/to/buildctl
buildctl --help
```

`python -m buildctl` works too.

## Quick start

Run these from inside the repository you want built (buildctl finds the Git toplevel), or pass `--repo <path>`.

```sh
# 1. Write build.yaml and your phase prompts, commit them, and review that commit yourself.
git add build.yaml prompts/ && git commit -m "Roadmap"

# 2. Trust that exact commit. Typing the SHA twice is deliberate.
buildctl trust init --to "$(git rev-parse HEAD)" --confirm "$(git rev-parse HEAD)"

# 3. Prove the sandbox holds on this machine. Spends nothing.
buildctl isolation verify

# 4. Check both agent CLIs are logged in. Spends nothing.
buildctl auth check

# 5. Create the build state and start.
buildctl bootstrap
buildctl start
```

`buildctl validate` checks `build.yaml` at any time. `buildctl --dry-run bootstrap` is a smoke test that touches neither agents nor Git.

## How a phase runs

1. **Clone.** A fresh clone of your repository at the current commit, with no remote and with `AGENTS.md`, `CLAUDE.md`, `.claude` and `.codex` removed.
2. **Implement.** The implementer gets the phase prompt and its acceptance contract and works in the clone, sandboxed.
3. **Gate.** The required files must exist, and every acceptance command must pass, sandboxed and with no network.
4. **Review.** The reviewer, read-only, gets the exact changed bytes and must account for every changed path. Its findings, not its verdict, decide what happens next:
   - a fixable `HIGH` finding goes back to the implementer for remediation;
   - an architectural or recurring `HIGH` goes to a debate;
   - a `CRITICAL` stops for a human;
   - nothing open above `MEDIUM` advances.
5. **Commit.** Only paths the phase is allowed to touch, none of them governance files, are copied into your repository and committed to `build/phase-N`.
6. **Integrate.** `main` is fast-forwarded to the phase commit and the repository is left on `main`, so the next phase starts from it. For a phase with `merge_policy: human`, this waits for your approval.

Every step is recorded before and after it runs. If the process dies, the next run picks up where it stopped, or tells you exactly what it cannot account for.

## Commands

| Command | What it does |
|---|---|
| `start` | Run every incomplete phase in order. |
| `run --until <N or milestone>` | Run up to phase N, or a milestone named in `build.yaml`. |
| `phase <N>` | Run one phase. Refused if it is already complete. |
| `status` | State, current phase, any pending approval or grant (with its nonce), and today's spend. |
| `pause` | Stop starting new work. |
| `resume` | Allow it again. |
| `approve --nonce <n>` | Approve the pending checkpoint. The nonce comes from `status`. |
| `retry` | Try again after an ordinary failure. Starts a fresh attempt. |
| `retry --authorize-extra-attempt --nonce <n>` | Buy exactly one more pass past an exhausted limit. |
| `reconcile --operation <id> --action accept-output\|authorize-retry` | Settle an agent call whose outcome is unknown, for example after a crash or timeout. |
| `trust init --to <sha> --confirm <sha>` / `trust show` | Pin, or show, what buildctl trusts. |
| `isolation verify` | Probe the sandbox and record that it passed. Runs refuse to start once that record is over 24 hours old. |
| `auth check` | Confirm both agent CLIs are logged in. |
| `bootstrap` | Create the build state. Requires fresh isolation conformance. |
| `validate` | Check `build.yaml`. |
| `inspect` | Everything a front end needs, as one JSON document: state, phases, trust, isolation, budget, the stop and its meaning, and every action with the conditions that allow or lock it. Read-only; works before trust is set up. |
| `audit --after <seq>` | Audit events after a sequence number, as JSON. |

Global flags: `--repo <path>` (must be the repository toplevel) and `--dry-run`.

## Operator console

`buildctl-ui` is an optional companion: a local web console and a terminal UI over the same build. It lives in `ui/` as its own distribution, so editing it never changes the kernel digest that `trust init` pins.

```sh
pip install -e ./ui            # web console only
pip install -e './ui[tui]'     # adds the terminal UI (Textual)
buildctl-ui                    # opens the console for the repository you are in
buildctl-ui tui                # the same thing in the terminal
buildctl-ui --demo checkpoint  # a synthetic build to look around in: blocked, checkpoint, ceiling, unknown, running, fresh
```

Both surfaces read `buildctl inspect` and change state only by running the `buildctl` CLI, so the kernel's checks, lock and audit apply exactly as in a terminal. Every action is shown with the conditions that allow or lock it. Approving, pinning trust, buying an extra attempt and reconciling ask you to type the evidence (the tree-hash prefix, the commit, the phase number or the operation); the console sends the nonce it showed you, so an approval made against state that has since moved is refused. Runs start detached and keep going if you close the console.

The web console listens on 127.0.0.1 only. Every request needs the token in the link `buildctl-ui` prints, the Host header must name the console, and state changes must be JSON from the console's own origin.

## When it stops

buildctl stops with an escalation code whenever it cannot continue safely. `buildctl status` shows the code and, where relevant, the nonce you need.

| Code | What it means | What to do |
|---|---|---|
| `CHECKPOINT_REQUIRED` | A phase in `checkpoints.before_phases`, a `merge_policy: human` merge, or the MVP milestone needs you. | Review, then `approve --nonce`. |
| `REVIEW_BLOCKED`, `UNREVIEWABLE_DELTA`, `DEBATE_UNRESOLVED` | The reviewer or the debate could not clear the phase, or the change has binary or oversized files the reviewer cannot read. | Read the findings in the artifacts directory, adjust the phase prompt or decide, then `retry`. |
| `*_EXHAUSTED` | A retry, review or remediation limit was hit, or too many failures in a row. | `retry --authorize-extra-attempt --nonce`, or fix the cause first. |
| `UNKNOWN_EXTERNAL_OPERATION`, `OPERATION_TIMEOUT_UNKNOWN`, `STALE_RUN_UNKNOWN_OPERATION` | An agent call may or may not have finished. | `reconcile`: accept its output, or authorize a retry. |
| `AUTHENTICATION_REQUIRED` | An agent CLI is logged out. | Log in, then `retry`. |
| `DAILY_BUDGET_UNAVAILABLE` | The next agent call would exceed `max_daily_cost_usd`. | Wait for tomorrow (UTC) or raise the limit and re-trust, then `retry`. |
| `ADVANCE_PRECONDITIONS_UNMET` | The phase passed review, but the budget ledger or a required approval no longer checks out. | Fix what it names, then `retry`. |
| `AGENT_OPERATION_FAILED` | An agent call failed. The status detail has a redacted reason. | Fix the cause, then `retry`. |
| `PHASE_DEADLINE_EXCEEDED` | The phase ran past `max_phase_minutes`. | `retry`. |
| `DELTA_CHANGED_AFTER_REVIEW` | What would be committed differs from what was reviewed. | Investigate before retrying. This should never happen. |

**Debates.** With `roles.arbiter.mode: interactive`, a debate writes `DEBATE_REQUEST.json` to the phase's artifacts directory and stops. It contains the findings, the evidence digest and the exact output format. Run the debate (for example with `agent-debate`), write the result to the `output_path` the request names, then `retry`. A result that agrees becomes an ADR in `decisions/` and is implemented.

## Trust

buildctl refuses to run under rules or code you have not explicitly accepted.

**What `trust init` pins:**
- The governed files at that exact commit: `build.yaml`, `AGENTS.md`, `CLAUDE.md`, everything under `prompts/` and `templates/`, and every file in `governance_documents`. buildctl reads them from its own read-only snapshot, never from your working tree, and fails the phase if an agent changes any of them.
- The installed buildctl itself: its version and a digest of every file in the package.
- The `codex` and `claude` executables, by path and hash.

**Run `trust init` again after you:**
- change any governed file (the change only takes effect once you trust the new commit);
- upgrade, reinstall or edit buildctl (until you do, buildctl refuses to start and shows both digests);
- upgrade the `codex` or `claude` CLI (until you do, the next call to it fails with `EXECUTABLE_INTEGRITY_FAILURE`).

**Run `isolation verify` again** when the last one is more than 24 hours old, and after any `trust init` that changed governed files or followed a buildctl upgrade. The recorded result is bound to the sandbox policy and the trusted snapshot, so either change invalidates it.

## Configuration

`build.yaml` is JSON (JSON is valid YAML). This is a complete, minimal example:

<!-- example:build.yaml -->
```json
{
  "limits": {
    "max_phase_attempts": 6,
    "max_review_format_attempts": 4,
    "max_semantic_review_attempts": 10,
    "max_debate_rounds": 3,
    "max_phase_minutes": 120,
    "max_daily_cost_usd": 50.0,
    "max_consecutive_failures": 3
  },
  "operation_cost_usd": {"implementation": 2.0, "review": 2.0, "remediation": 2.0, "debate": 5.0},
  "governance_documents": ["docs/ARCHITECTURE.md"],
  "git": {"remote": "origin", "push": false, "create_pr": false, "merge": false,
          "integration_branch": "main"},
  "roles": {
    "implementer": {
      "network": true, "auth_mode": "host_cli", "credential_env": [],
      "command": ["codex", "exec", "--ignore-user-config", "--dangerously-bypass-approvals-and-sandbox",
                  "--output-last-message", "{output}", "{prompt}"]
    },
    "reviewer": {
      "vendor": "anthropic", "model_family": "claude", "prompt_lineage": "v1",
      "protocol_version": "buildctl-review-v2",
      "network": true, "auth_mode": "host_cli", "credential_env": [],
      "command": ["claude", "-p", "{prompt}", "--no-session-persistence", "--tools", "Read,Grep,Glob"],
      "context_documents": ["docs/ARCHITECTURE.md"]
    },
    "arbiter": {
      "network": false, "auth_mode": "none", "credential_env": [], "mode": "interactive",
      "command": ["agent-debate"]
    }
  },
  "milestones": {"mvp": 1},
  "checkpoints": {"before_phases": [0], "mvp_completion": 1},
  "phase_defaults": {
    "required_files": ["docs/HANDOFF_PHASE_{phase}.md"],
    "commands": [["{python}", "-m", "unittest", "discover", "-s", "tests"]],
    "invariants": ["no_credentials_in_source"],
    "claude_review_required": true,
    "debate_on_architecture_disagreement": true,
    "max_remediation_attempts": 4,
    "merge_policy": "auto",
    "allowed_paths": ["src/", "tests/", "pyproject.toml", "docs/HANDOFF_PHASE_{phase}.md"]
  },
  "phases": [
    {"number": 0, "name": "Foundation", "prompt": "prompts/phases/phase-00.md",
     "acceptance": {"merge_policy": "human"}},
    {"number": 1, "name": "First feature", "prompt": "prompts/phases/phase-01.md"}
  ]
}
```

| Key | Meaning |
|---|---|
| `limits` | Ceilings. Hitting one stops the build for a human. All seven are required. |
| `operation_cost_usd` | Estimated cost of each paid agent call, charged against `max_daily_cost_usd` before the call runs. |
| `governance_documents` | Files beyond the fixed governed set that `trust init` pins and agents may not modify. Required; may be `[]`. |
| `git.push`, `create_pr`, `merge` | Publication. All `false` keeps everything local and fast-forwards `main` after each phase. With `push` on, the phase branch is pushed instead and `main` is not moved locally; it moves only when a PR merges (`create_pr` plus `merge` requests auto-merge). |
| `git.integration_branch` | The branch fast-forwarded after each phase. Default `main`. Fast-forward only: if it has commits the phase lacks, buildctl stops instead of merging. |
| `roles.*.command` | The argv for each agent. `{prompt}` and `{output}` are substituted. |
| `roles.*.network`, `auth_mode`, `credential_env` | Network access in the sandbox, and how the agent authenticates: `host_cli` (your CLI login), `environment` (the named `*_API_KEY` / `*_TOKEN` variables) or `none`. |
| `roles.reviewer.vendor`, `model_family`, `prompt_lineage` | The reviewer identity every review report must echo back exactly. Required. |
| `roles.reviewer.context_documents` | Governed files the reviewer gets as architecture context. Required; may be `[]`. |
| `roles.reviewer.review_prompt`, `roles.implementer.remediation_prompt` | Optional. Replace the built-in prompt with your own file, which must be governed (under `prompts/` or in `governance_documents`). A review prompt must contain ``Protocol version: `buildctl-review-v2` ``. |
| `roles.arbiter.mode` | `interactive`: debates stop for a human (see above). |
| `milestones` | Names for `run --until`. |
| `checkpoints.before_phases` | Phases that need your approval before they start. |
| `checkpoints.mvp_completion` | The phase after which the build stops for your sign-off. |
| `phase_defaults` | The acceptance contract every phase inherits. `{phase}` in paths becomes the phase number. |
| `phases[].acceptance` | Per-phase overrides of any `phase_defaults` key. |

**Acceptance commands.** Each command runs in the sandbox with no network and must exit 0. `{python}` resolves to an interpreter with the dependencies declared in the phase's `pyproject.toml` installed. Every dependency must have an upper bound (`pkg>=1,<2`), and only pre-built wheels are installed, so nothing from the agent's manifest executes as you. A command without `{python}` runs as written.

**Allowed paths.** A phase may only change paths under `allowed_paths`. Anything else, and any change to a governed file, fails the phase.

## What buildctl provides, and what you provide

buildctl ships the output formats agents must return (`PHASE_RESULT`, `REVIEW_RESULT`, `DEBATE_RESULT`) and the default review and remediation prompts. They live in the package and are pinned with it. The formats cannot be overridden, because the code that validates them lives here too. To tailor a prompt for one project, point `review_prompt` or `remediation_prompt` at your own file rather than editing buildctl's copy: an edited copy changes the kernel and needs `trust init`.

You provide `build.yaml`, the phase prompts, and any governance and context documents.

## Where state lives

Canonical state, the hash-chained audit log, the cost ledger, the trusted snapshot and every agent artifact live outside your repository, in `$XDG_STATE_HOME/buildctl/<hash of repo path>` (by default `~/.local/state/buildctl/...`). Set `BUILDCTL_STATE_DIR` to put it elsewhere. Agents cannot read it.

`LEGACY_FACTORY_STATE_DIR` from the the original project supervisor is refused, not ignored, so state never quietly lands somewhere you did not intend.

## Moving the original project onto buildctl

1. Add the two required lists to its `build.yaml`. `tests/fixtures/target/build.yaml` here is its file with exactly those additions.
2. Its existing review and remediation prompts keep working, now as overrides through the `review_prompt` and `remediation_prompt` keys it already declares.
3. Its `templates/*_RESULT.json` files are no longer read. Delete them or leave them.
4. Commit, then `trust init`, `isolation verify`, `bootstrap`.
5. It currently sits on `build/phase-45` with `main` 63 commits behind. Start from `build/phase-45`, and the first finished phase brings `main` all the way forward.

## Limitations

- macOS only: the sandbox is `sandbox-exec`.
- The implementer is assumed to be Codex and the reviewer Claude. `auth check`, credential staging and parts of config validation are written for those two CLIs.
- Dependency provisioning for acceptance commands is Python-only (`pyproject.toml`, `{python}`).
- With `arbiter.mode` set to anything but `interactive`, the arbiter command's `{phase}`, `{review}` and `{rounds}` placeholders are not filled in. Use interactive debates.
- The review protocol identifier is still `buildctl-review-v2`, the name it had in the original project, so existing review prompts keep working.

## Development

```sh
python3 -m unittest discover -s tests -t .
PYTHONPATH=ui python3 -m unittest discover -s ui/uitests -t ui   # the console
```

The suite needs only the standard library. Tests that need the real `codex` CLI or `pytest` skip when those are absent. `tests/fixtures/target/` is a frozen copy of the original project's governed files, used to check that a real configuration still validates.
