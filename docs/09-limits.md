## Guarantees and limits {#limits}

### What buildctl guarantees {#guarantees}

- **Agents never touch your repository, state or budget.** They work in a disposable clone with no remote, and buildctl alone copies their output in.
- **Only reviewed bytes are committed.** What is committed must match what the reviewer saw, or the phase stops with [DELTA_CHANGED_AFTER_REVIEW](#DELTA_CHANGED_AFTER_REVIEW).
- **Governed files stay governed.** No phase can change `build.yaml`, prompts, templates or governance documents.
- **Nothing runs under rules you did not trust.** buildctl reads governed files from its trusted snapshot, so a change in your working tree has no effect until you trust the new commit. A changed buildctl or agent executable stops buildctl until you do.
- **No call spends past the budget.** Each call is reserved against the daily limit before it runs.
- **A crash is accounted for.** Every step is recorded before and after it runs. An agent call whose outcome is unknown stays unknown until you reconcile it.
- **Every state change is recorded.** The audit log is hash-chained, so editing or removing an event breaks the chain and buildctl refuses to load the state.

### What it does not guarantee {#non-guarantees}

- **That the code is correct.** The review is adversarial, not a proof. Your acceptance commands are the only executable check.
- **A compliance-grade ledger.** The audit chain is tamper-evident, not signed or externally anchored.
- **Your real cost.** The budget counts the estimates in `operation_cost_usd`, not your provider's bill.

### Current limitations {#limitations}

- On Linux, the host must allow unprivileged user namespaces. Stock containers do not, so buildctl refuses to run in them. See [Running on Linux](#linux).
- On Linux, a role with network access reaches the whole network. There is no per-host allowlist yet.
- The implementer is assumed to be Codex and the reviewer Claude. `auth check`, credential staging and parts of config validation are written for those two CLIs.
- Dependency provisioning for acceptance commands is Python-only (`pyproject.toml`, `{python}`).
- With `arbiter.mode` set to anything but `interactive`, the arbiter command only receives `{prompt}` and `{output}`. A command that uses `{phase}`, `{review}` or `{rounds}` fails. Use interactive debates.
