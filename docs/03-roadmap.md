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

Every key is described in [Configuration](#config).

### Phase prompts {#phase-prompts}

A phase prompt is a Markdown file that tells the implementer what to build. Good prompts name what already exists, what to add, which paths the phase may change, and what the handoff must record. The implementer also receives the phase's acceptance contract, so the prompt does not need to repeat the gate commands.

Prompts are governed files: an agent cannot change them, and a change you make takes effect only after you [trust](#trust) the new commit.

### The acceptance contract {#acceptance}

`phase_defaults` is the contract every phase inherits. A phase overrides any part of it under `acceptance`, and `{phase}` in a path becomes the phase number.

- **Required files** must exist after the implementer finishes.
- **Commands** each run in the sandbox with no network and must exit 0. `{python}` resolves to an interpreter with the dependencies declared in the phase's `pyproject.toml` installed. Every dependency needs an upper bound (`pkg>=1,<2`), and only pre-built wheels are installed, so nothing from the agent's manifest executes as you. A command without `{python}` runs as written.
- **Allowed paths** are the only paths the phase may change. Anything else, and any change to a governed file, fails the phase.
- **Invariants** are named rules the reviewer checks the change against.

### Prompts buildctl provides {#protocol}

buildctl ships the output formats agents must return (`PHASE_RESULT`, `REVIEW_RESULT`, `DEBATE_RESULT`) and the default review and remediation prompts. They live in the package and are pinned with it. The formats cannot be overridden, because the code that validates them lives in buildctl too.

To tailor a prompt for one project, point `roles.reviewer.review_prompt` or `roles.implementer.remediation_prompt` at your own governed file. A review prompt must contain ``Protocol version: `buildctl-review-v2` ``.
