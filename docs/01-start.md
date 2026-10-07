## Start {#start}

### Quick start {#quick-start}

Run these from inside the repository you want built. buildctl finds the Git toplevel; `--repo <path>` names it explicitly.

```sh
# 1. Commit build.yaml and your phase prompts, then review that commit yourself.
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

### Requirements {#requirements}

- macOS. Agents run under `sandbox-exec`; there is no Linux backend yet.
- Python 3.11 or newer.
- The `codex` CLI (the implementer) and the `claude` CLI (the reviewer), both logged in: `codex login`, `claude auth login`.

### Install {#install}

```sh
pip install -e /path/to/buildctl            # the supervisor
pip install -e '/path/to/buildctl/ui[tui]'  # optional: the console and terminal UI
buildctl --help
```

`python -m buildctl` works too.

### Look around first {#demo}

`buildctl-ui --demo` opens the console on a synthetic build of Relay, a fictional webhook delivery service, without spending anything or touching your repositories. Pick a situation:

```sh
buildctl-ui --demo checkpoint   # waiting for your approval
buildctl-ui --demo blocked      # the reviewer would not clear a phase
buildctl-ui --demo running      # a phase in flight
buildctl-ui tui --demo ceiling  # the same, in the terminal
```

The other scenarios are `unknown` and `fresh`. Demo builds refuse anything that would pin trust or spend money.
