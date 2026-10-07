## Running and watching {#running}

### Starting work {#starting}

`buildctl start` runs every incomplete phase in order. `buildctl run --until <N or milestone>` stops after phase N or after a milestone named in `build.yaml`. `buildctl phase <N>` runs one phase, and refuses a phase that is already complete.

Only one buildctl process changes state at a time. A second one is refused while the first holds the state lock.

### Watching a run {#watching}

`buildctl status` prints the state, the current phase, any pending approval or grant with its nonce, and today's spend. `buildctl inspect` prints everything a front end needs as one JSON document, and works before trust is set up. `buildctl audit --after <seq>` prints audit events after a sequence number.

The console shows the same information live: the phase line, the call in flight and its deadline, and spend by phase. See [The console](#console).

### Pausing {#pausing}

`buildctl pause` stops buildctl starting new work. A call already in flight finishes. `buildctl resume` allows new work again. Both take the state lock, so they cannot interrupt a run that is holding it.

### The budget {#budget}

Each paid agent call has an estimated cost in `operation_cost_usd`. buildctl reserves that amount against `max_daily_cost_usd` before the call runs, and refuses the call if it would go over. The day is measured in UTC.

The ledger records estimates you configured, not your provider's bill.

### Where state lives {#state}

Canonical state, the hash-chained audit log, the cost ledger, the trusted snapshot and every agent artifact live outside your repository, in `$XDG_STATE_HOME/buildctl/<hash of repo path>` (by default `~/.local/state/buildctl/...`). Set `BUILDCTL_STATE_DIR` to put it elsewhere.

| File | What it holds |
|---|---|
| `state.json` | The build state: status, current phase, step, attempts, pending approval, operations. |
| `audit.jsonl` | Every state change, each event hash-chained to the one before. |
| `costs.jsonl` | One line per paid agent call reserved against the budget. |
| `trust.json` and `snapshot/` | The trust anchor and the read-only copy of the governed files. |
| `isolation-conformance.json` | The last sandbox verification. |
| `artifacts/phase-N/` | Agent results, review reports, gate results, debate requests and ADR proposals. |
