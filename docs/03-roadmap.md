## Writing a roadmap {#roadmap}

A roadmap is `build.yaml` plus one prompt file per phase. `build.yaml` is JSON, which is valid YAML.

### A complete minimal build.yaml {#example}

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
                  "--json", "--output-last-message", "{output}", "{prompt}"]
    },
    "reviewer": {
      "vendor": "anthropic", "model_family": "claude", "prompt_lineage": "v1",
      "protocol_version": "buildctl-review-v2",
      "network": true, "auth_mode": "host_cli", "credential_env": [],
      "command": ["claude", "-p", "{prompt}", "--no-session-persistence", "--tools", "Read,Grep,Glob",
                  "--output-format", "stream-json", "--verbose"],
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

Every key is described in [Configuration](#config). The implementer's `--dangerously-bypass-approvals-and-sandbox` turns off Codex's own sandbox and approval prompts: buildctl already runs Codex inside its own sandbox, and an unattended run has no one to answer a prompt. See [Trust and the sandbox](#trust) for what that sandbox holds. Codex's `--json` and Claude's `--output-format stream-json --verbose` make each agent emit the event stream that [recording](#evidence) reads. Without them buildctl refuses the configuration, unless you add `"evidence": {"recording": "off"}`.

Three optional parts are left out. A phase may set `"documentation": false` to skip the documentation step. A `demo` section names how to start the product for a [recorded demo](#demos). With `roles.arbiter.mode: debate`, `limits.max_debate_turns` caps each [debate](#debates) and `operation_cost_usd.debate_turn` prices each turn. All three are in [Configuration](#config).

The example is a macOS config. On Linux, set each role's `auth_mode` to `environment` and name its API key in `credential_env`: `host_cli` is refused there. See [Running on Linux](#linux).

### Phase prompts {#phase-prompts}

A phase prompt is a Markdown file that tells the implementer what to build. Good prompts name what already exists, what to add, which paths the phase may change, and what the handoff must record. The implementer also receives the phase's acceptance contract, so the prompt does not need to repeat the gate commands.

Prompts are governed files: an agent cannot change them, and a change you make takes effect only after you [trust](#trust) the new commit.

### The acceptance contract {#acceptance}

`phase_defaults` is the contract every phase inherits. A phase overrides any part of it under `acceptance`, and `{phase}` in a path becomes the phase number.

- **Required files** must exist after the implementer finishes.
- **Commands** each run in the sandbox with no network and must exit 0. `{python}` resolves to an interpreter with the dependencies declared in the phase's `pyproject.toml` installed. Every dependency needs an upper bound (`pkg>=1,<2`), and only pre-built wheels are installed, so nothing from the agent's manifest executes as you. Three governed `python` settings in `build.yaml` widen that for a real project: `extras` installs named optional-dependency groups (test tools such as pytest), `constraints` points at a governed constraints file that bounds otherwise unbounded requirements, and `direct_references` allows exact hash-pinned wheel URLs. Git and source URLs stay refused, because building them would run the project's build code as you. A command without `{python}` runs as written.
- **Node products** use `{node}`, `{npm}` or `{npx}` instead. Before the gate opens, buildctl installs the exact packages in the phase's `package-lock.json` (lockfile version 2 or later; every package from the npm registry with an integrity hash) into an environment outside the workspace, and runs `prisma generate` when `prisma/schema.prisma` exists. That install runs sandboxed with network on, writes confined to the environment and your home folder denied, because some packages fetch binaries in their install scripts. While the gate commands run, the workspace's `node_modules` links to that environment; the link is removed before the phase's changes are recorded. A workspace that already contains `node_modules` fails. Use `node --import tsx` rather than the `tsx` command: the `tsx` command opens a local socket, which the gate sandbox refuses.
- **Allowed paths** are the only paths the phase may change. Anything else, and any change to a governed file, fails the phase.
- **Invariants** are named rules the reviewer checks the change against.

### Skills {#skills}

A skill is a folder with a `SKILL.md` and any files it needs, the format Claude Code and Codex both read. A build can hand skills to its agents, but never from your installed folders at run time: a skill enters the build as committed files, so `trust init` pins it, and the version an agent used is the version you trusted.

```sh
buildctl skills add ~/.claude/skills/impeccable --exclude 'scripts/bin/*'
```

That copies the skill into `skills/impeccable/`. Declare it and say who gets it:

```json
"skills": {"impeccable": {}},
"roles": {
  "implementer": {"...": "...", "skills": ["impeccable"]},
  "reviewer":    {"...": "...", "skills": []}
},
"phases": [{"number": 3, "...": "...", "skills": {"reviewer": ["impeccable"]}}]
```

Then commit and `trust init`. A phase can add skills per role. The reviewer gets only skills named for it, never the implementer's, so its second opinion is shaped only by what you chose to show it. Each CLI also keeps its own built-in skills (Claude Code's `code-review` or `security-review`, Codex's `skill-creator` and the like); those ship inside the executable `trust init` pins, so they cannot change under a build either.

At each agent call, buildctl copies that call's skills from its trusted snapshot into the agent's own scratch space: Codex reads them from its isolated `$CODEX_HOME/skills`, and Claude Code from a throwaway plugin passed with `--plugin-dir`, with `Skill` added to the role's `--tools` and exactly those skills pre-approved. A skill that runs shell commands as it loads (a `` !`command` `` line) cannot work for the read-only reviewer, so `validate` refuses one listed for it. A skill's scripts run only where the role can already run commands, inside the sandbox; the reviewer stays read-only. Debate turns get no skills.

Every call's record lists the skills it was offered, each with a digest of its files, and the audit log notes which ones the agent's own events show it loading. `buildctl skills status` shows when an installed copy has moved on; `buildctl skills sync <name>` copies it in again for you to commit and trust.

Independent of skills, every Claude role runs with `--setting-sources project,local`, so your personal Claude Code settings, skills, plugins and `CLAUDE.md` stay out of the build. The agent's copy of the repository also has `.claude`, `.codex`, `.agents`, `AGENTS.md` and `CLAUDE.md` removed before it starts.

### Prompts buildctl provides {#protocol}

buildctl ships the output formats agents must return (`PHASE_RESULT`, `REVIEW_RESULT`, `DEBATE_RESULT`, `DEBATE_TURN`, `DOC_RESULT`) and the default review, remediation and documentation prompts. They live in the package and are pinned with it. The formats cannot be overridden, because the code that validates them lives in buildctl too.

To tailor a prompt for one project, point `roles.reviewer.review_prompt` or `roles.implementer.remediation_prompt` at your own governed file. A review prompt adds to the built-in one: buildctl appends it under "Project review policy", so the verdict, coverage and finding rules the validator enforces always reach the reviewer. A remediation prompt replaces the built-in one.

`documentation.brief` names a governed file with your docs' structure and voice, by default `prompts/documentation.md` when it exists. The documenter gets it as a brief alongside the built-in documentation prompt, which it does not replace.
