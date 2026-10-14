## Start {#start}

### Quick start {#quick-start}

Run these from inside the repository you want built. buildctl finds the Git toplevel; `--repo <path>` names it explicitly. Step 1 assumes your roadmap exists. If it does not, [write it first](#roadmap), or [watch a demo build](#demo) that spends nothing.

```sh
# 1. Commit build.yaml and your phase prompts,
#    then review that commit yourself.
git add build.yaml prompts/ && git commit -m "Roadmap"

# 2. Trust that exact commit. Typing the SHA twice is deliberate.
buildctl trust init --to "$(git rev-parse HEAD)" \
                    --confirm "$(git rev-parse HEAD)"

# 3. Prove the sandbox holds on this machine. Spends nothing.
buildctl isolation verify

# 4. Check both agent CLIs are logged in. Spends nothing.
buildctl auth check

# 5. Create the build state and start.
buildctl bootstrap
buildctl start
```

`buildctl status` then shows the phase and step it is on, and `buildctl-ui` opens the console on it. `buildctl validate` checks `build.yaml` at any time. `buildctl --dry-run bootstrap` is a smoke test that touches neither agents nor Git.

### Requirements {#requirements}

- macOS, where agents run under `sandbox-exec`, or Linux, where they run under bubblewrap 0.8.0 or newer. Linux needs some setup first: see [Running on Linux](#linux).
- Python 3.11 or newer.
- The `codex` CLI (the implementer) and the `claude` CLI (the reviewer). On macOS, log both in: `codex login`, `claude auth login`. On Linux, give them API keys instead.

### Install {#install}

```sh
# The supervisor
pip install -e /path/to/buildctl
# Optional: the console and terminal UI
pip install -e '/path/to/buildctl/ui[tui]'
buildctl --help
```

`python -m buildctl` works too.

### Running on Linux {#linux}

buildctl runs on a Linux server, VM or CI runner when the host lets bubblewrap create user namespaces. Stock containers do not, and buildctl refuses rather than run with a weaker sandbox.

Install bubblewrap 0.8.0 or newer, then run `buildctl isolation verify`. If it refuses, its message names the setting to change.

Ubuntu 24.04 and GitHub's Ubuntu runners restrict user namespaces through AppArmor. Load the profile that lets bubblewrap use them:

```sh
sudo apt-get install -y bubblewrap apparmor-profiles
sudo cp /usr/share/apparmor/extra-profiles/bwrap-userns-restrict /etc/apparmor.d/
sudo apparmor_parser -r /etc/apparmor.d/bwrap-userns-restrict
```

Elsewhere:

- **Debian with user namespaces disabled:** `sudo sysctl -w kernel.unprivileged_userns_clone=1`.
- **Ubuntu 22.04 and RHEL 9** ship bubblewrap 0.6, which is too old. Install a newer bubblewrap, or upgrade the host.
- **Inside a container,** the default seccomp profile blocks namespaces. Start the container with `--security-opt seccomp=unconfined --security-opt apparmor=unconfined --security-opt systempaths=unconfined`, or run it under gVisor or Sysbox. On Kubernetes, set the same through the pod's `securityContext`.
- **If you use the system Python** for acceptance commands, install `python3-venv` as well. Without it, gates cannot build the product environment.

Two things differ from macOS:

- **Agents authenticate with API keys:** `auth_mode: environment` and `credential_env`, or cloud identity. `auth_mode: host_cli` is refused, because it would put someone's personal login on a shared machine.
- **A role with network access reaches the whole network,** including the cloud metadata endpoint. Block that at the host (an IMDSv2 hop limit of 1, or a firewall rule).

#### ISOLATION_HOST_UNSUPPORTED {#ISOLATION_HOST_UNSUPPORTED}

`isolation verify`, `bootstrap` and the console's Setup view show this when the host cannot run the sandbox. The rest of the message names the cause and its fix, from the list above. Nothing has run and nothing was spent. Fix the host, then run `buildctl isolation verify` again.

### Look around first {#demo}

`buildctl-ui --demo` opens the console on a synthetic build of Relay, a fictional webhook delivery service, without spending anything or touching your repositories. Pick a situation:

```sh
buildctl-ui --demo checkpoint   # waiting for your approval
buildctl-ui --demo blocked      # the reviewer would not clear a phase
buildctl-ui --demo running      # a phase in flight
buildctl-ui tui --demo ceiling  # the same, in the terminal
```

The other scenarios are `unknown` and `fresh`. Demo builds refuse anything that would pin trust or spend money.
