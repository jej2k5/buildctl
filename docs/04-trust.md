## Trust and the sandbox {#trust}

buildctl refuses to run under rules or code you have not explicitly accepted.

### What trust init pins {#trust-pins}

`buildctl trust init --to <sha> --confirm <sha>` takes a full 40-character commit SHA, typed twice. It pins three things:

- **The governed files at that commit:** `build.yaml`, `AGENTS.md`, `CLAUDE.md`, everything under `prompts/` and `templates/`, and every file in `governance_documents`. buildctl reads them from its own read-only snapshot, never from your working tree, and fails the phase if an agent changes any of them.
- **The installed buildctl itself:** its version and a digest of every file in the package.
- **The `codex` and `claude` executables,** by path and hash.

`buildctl trust show` prints what is pinned.

### When to trust again {#retrust}

Run `trust init` again after you:

- change any governed file. The change only takes effect once you trust the new commit.
- upgrade, reinstall or edit buildctl. Until you do, buildctl refuses to start and shows both digests.
- upgrade the `codex` or `claude` CLI. Until you do, the next call to it fails with `EXECUTABLE_INTEGRITY_FAILURE`.

The console (`buildctl-ui`) is a separate package, so updating it never requires a new `trust init`.

### Isolation conformance {#isolation}

`buildctl isolation verify` probes the sandbox on this machine and records that it passed. It spends nothing.

Runs refuse to start once that record is over 24 hours old. The record is also bound to the sandbox policy and the trusted snapshot, so run `isolation verify` again after any `trust init` that changed governed files or followed a buildctl upgrade.

### What the sandbox holds {#sandbox}

- Agents work in a disposable clone with no remote.
- Gate commands run with no network.
- Agent output is checked before a single byte reaches your repository: only allowed, non-governed paths are copied, and the copy must match what the reviewer saw.
- The canonical state, audit log and cost ledger live outside your repository, where agents cannot read them.
