## Trust and the sandbox {#trust}

buildctl refuses to run under rules or code you have not explicitly accepted.

### What trust init pins {#trust-pins}

`buildctl trust init --to <sha> --confirm <sha>` takes a full 40-character commit SHA, typed twice. It pins three things:

- **The governed files at that commit:** `build.yaml`, `AGENTS.md`, `CLAUDE.md`, everything under `prompts/` and `templates/`, every file in `governance_documents`, and every declared [skill](#skills) folder, byte for byte with its executable bits. buildctl reads them from its own read-only snapshot, never from your working tree, and fails the phase if an agent changes any of them.
- **The installed buildctl itself:** its version and a digest of every file in the package.
- **Each role's executable,** by path and hash: the implementer and the reviewer, the documenter when documentation is on, and the arbiter when it runs its own command. An `interactive` or `debate` arbiter has no executable of its own.

`buildctl trust show` prints what is pinned.

### When to trust again {#retrust}

Run `trust init` again after you:

- change any governed file. The change only takes effect once you trust the new commit.
- upgrade, reinstall or edit buildctl. Until you do, buildctl refuses to start and shows both digests.
- upgrade the `codex` or `claude` CLI. Until you do, the next call to it stops at [AGENT_OPERATION_FAILED](#AGENT_OPERATION_FAILED) with `<ROLE>_EXECUTABLE_INTEGRITY_FAILURE`.

The console (`buildctl-ui`), `buildctl-drafts` and `buildctl-discover` are separate packages, outside the kernel digest, so updating them never requires a new `trust init`. `buildctl-drafts make` still runs under the reviewer's pinned executable and needs fresh isolation conformance.

### Isolation conformance {#isolation}

`buildctl isolation verify` probes the sandbox on this machine and records that it passed. It spends nothing.

Runs, `bootstrap` and `buildctl-drafts` refuse to start once that record is over 24 hours old. The record is also bound to this host, the sandbox policy and the trusted snapshot, so run `isolation verify` again after any `trust init` that changed governed files or followed a buildctl upgrade.

On macOS the sandbox is `sandbox-exec`. On Linux it is bubblewrap, and the record also pins its version and digest, so run `isolation verify` again after a bubblewrap upgrade too.

### What the sandbox holds {#sandbox}

- Agents work in a disposable clone with no remote.
- Gate commands run with no network. Without network, nothing in the sandbox can reach a Unix socket on the host either, such as the Docker daemon or an SSH agent.
- A [recorded demo](#demos) runs in loopback mode: it may serve on a loopback port for the recorder and reaches nothing, neither your machine's other local services nor the internet. `isolation verify` probes this mode too.
- Agent output is checked before a single byte reaches your repository: only allowed, non-governed paths are copied, and the copy must match what the reviewer saw.
- The canonical state, audit log and cost ledger live outside your repository, where agents cannot read them.
