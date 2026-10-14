# Spec: a Linux isolation backend on bubblewrap

Status: spiked 2026-10-14 on `spike/linux-bwrap` (`c5b613a`). The draft backend passed the full isolation probe on GitHub's Ubuntu 24.04 runners, x86_64 and arm64 (run 38577564533). Not built.

Decided 2026-10-14:
- Linux runs on a remote VM or server, inside a container, and on CI runners. No Linux workstation target.
- The backend is **bubblewrap**, a distro package with a pinned path, the same pattern as `/usr/bin/sandbox-exec`.
- **Fail closed.** A host that cannot pass the probe refuses to run, as macOS does today. The refusal names the setting that unlocks it. There is no weaker tier.
- Agents on Linux authenticate per operation (API keys, or cloud identity through Bedrock, Vertex or Azure). No interactive host login.

## The question

Can buildctl keep its isolation claim on Linux, unchanged?

Yes, on any host that allows unprivileged user namespaces. Not in a stock container, and nothing unprivileged there can. That boundary is the spec's main fact.

## Why bubblewrap, and why not the others

The contract is `verify_backend` (`supervisor/isolation.py`): a default-deny filesystem with path carve-outs, `.git` never writable, the control root and repository hidden even through symlinks, credential stores denied, each operation limited to its own runtime subtree, and network off unless a role allows it.

| Candidate | Verdict |
|---|---|
| **bubblewrap** | Passes the whole probe. The macOS profile ("read everything, deny these subtrees") becomes a mount layout ("bind root read-only, cover these subtrees") rule for rule. |
| Landlock | The only option that works unprivileged in a default container. Fails two checks by design: it can only grant rights, so a writable workspace makes `.git` writable; and its network rules (ABI 4, kernel 6.7) filter TCP ports, not hosts or Unix sockets. |
| nsjail, firejail, minijail, `unshare` | Need the same namespaces as bubblewrap, with more surface (firejail is setuid) or no distro package on the targets. |
| gVisor, Kata, Firecracker | Run containers, not one command. They are how an operator gets namespaces in a container, so the manual names them. They are not the backend. |

Docker's default seccomp profile blocks `unshare` and `clone` with new namespaces unless the process has `CAP_SYS_ADMIN`, and allows the three Landlock calls. Ubuntu 24.04 restricts unprivileged user namespaces through AppArmor (`kernel.apparmor_restrict_unprivileged_userns=1`), and the bubblewrap package ships no profile for it.

## What the spike proved

| Check | Docker (arm64) | GitHub x86_64 | GitHub arm64 |
|---|---|---|---|
| Full `verify_backend`, three roles | PASS | PASS | PASS |
| Stock environment refuses | yes | yes (AppArmor) | yes (AppArmor) |
| Unix socket connect, network off | refused | refused | refused |
| Nested user namespace | refused | refused | refused |
| io_uring | refused | refused | refused |
| `socketpair` | works | works | works |
| Unix socket connect, network on | allowed | allowed | allowed |

Hosts: Linux 6.17 (Azure), bubblewrap 0.9.0, Python 3.12.3. Docker Desktop: 6.12 linuxkit.

It found three things the plan did not have:
1. **The probe missed an escape.** With network off, a sandboxed process reached a host Unix socket in `/tmp`. A read-only mount does not stop `connect()`, and a network namespace does not cover filesystem sockets. On a real host that reaches `docker.sock`, an SSH agent or the session bus, which is code execution outside the sandbox. macOS blocks it because `(deny default)` without `network-outbound` covers Unix sockets, so the probe never had to check.
2. **Nested containers need a third flag.** bwrap cannot mount `/proc` under Docker's masked paths without `systempaths=unconfined`.
3. **The refusal is unreadable.** Under Ubuntu's restriction bwrap says `loopback: Failed RTM_NEWADDR: Operation not permitted`. Nothing in it says AppArmor.

## Design

### The backend: `linux-bwrap-v1`

`LinuxBubblewrapBackend` in `supervisor/isolation.py`. `available()` is Linux with `/usr/bin/bwrap` present. `production_backend()` tries macOS, then Linux.

Every command is wrapped as:

```
bwrap --die-with-parent --new-session --unshare-user --disable-userns
      --unshare-pid --unshare-ipc --unshare-uts --unshare-cgroup-try
      --ro-bind / / --dev /dev --proc /proc
      [--unshare-net]                       network not allowed
      <mount layout>
      [--seccomp 3]                         network not allowed
      -- argv
```

The mount layout follows the macOS profile line by line, in the same order, so a later rule wins in the same way:

| macOS rule | bubblewrap |
|---|---|
| `(allow file-read*)` | `--ro-bind / /` |
| `(allow file-write* (subpath workspace))`, unless read-only | `--bind workspace` |
| deny the runtime area, re-allow this operation's output | `--perms 0111 --tmpfs runtime_area`, `--bind output_dir`, `--bind home_dir`, `--remount-ro runtime_area` |
| deny the control root and the repository | `--perms 0000 --tmpfs <path> --remount-ro <path>` |
| `(deny file-write* (subpath workspace/.git))` | `--ro-bind .git .git` |
| deny `host_home` | `--perms 0000 --tmpfs host_home --remount-ro host_home` |
| `(allow network-outbound)` | no `--unshare-net`, no filter |

The runtime area's tmpfs is mode `0111`: traversable, not listable. That is the macOS rule "metadata on this node only". It goes read-only after the binds, because bwrap has to create the output mount point inside it first.

Two spike shortcuts become hard rules:
- **The control root, the repository and `.git` must exist** when the command is built. The spike skipped a missing path; the backend raises `IsolationError`, because an absent `.git` would let an agent create one.
- **`host_session` raises.** There is no host-session profile on Linux (see Authentication).

### The offline seccomp filter

When a role has no network, bwrap loads a classic BPF filter, built with the standard library (`offline_seccomp_filter()`), that:
- refuses `socket(AF_UNIX, ...)` with `EACCES`, which closes the escape the spike found and matches macOS;
- refuses `io_uring_setup`, because io_uring socket operations bypass seccomp;
- kills any other syscall ABI: on x86_64 that is i386 `socketcall` and x32 (`nr >= 0x40000000`), which would get around the number check.

`socketpair` stays allowed. Child processes need it, and it can reach nothing outside.

**Change from the spike:** the spike wrote the filter on every `command()` call. Instead, `isolation verify` writes `seccomp-offline-<machine>.bpf` into the control root, the conformance record pins its SHA-256, and `command()` refuses if the file's digest differs. bwrap reads it on fd 3, opened by `/bin/sh -c 'exec "$@" 3<"$0"'`, so the backend interface (argv only) and every caller stay unchanged. The sandbox cannot see the control root, so an agent cannot swap the filter.

Supported machines: `x86_64` and `aarch64`. Anything else refuses.

### Authentication on Linux

| `auth_mode` | Linux |
|---|---|
| `environment` | Supported. Keys from a secrets manager, or cloud identity. |
| `none` | Supported. |
| `host_cli`, reviewer | **Rejected at config validation.** It needs the host-session profile, which reads the operator's real HOME. |
| `host_cli`, implementer | **Rejected at config validation** (recommended; see Open decisions). It copies a personal Codex login into the sandbox. |

`validate_config` raises `ConfigurationError("role <name>: auth_mode host_cli is macOS only")`, so a Linux operator learns this at `trust init`, not mid-run.

### The probe gains checks, on both backends

`verify_backend` adds, for every role:
- `unix_socket_connect`: connect to a listener in a temporary directory outside the workspace. Expected `False` with network off. macOS must pass it too: it should, and now it has to prove it.
- `nested_userns` (Linux only): `unshare -U true`. Expected `False`.

Adding keys changes the probe on macOS, so this ships in the same release.

### The conformance record pins the host

`conformance_record()` adds `host`:

```json
{"machine": "x86_64", "bwrap": "/usr/bin/bwrap", "bwrap_version": "0.9.0",
 "bwrap_sha256": "<digest>", "seccomp_sha256": "<digest>", "kernel": "6.17.0-1022-azure"}
```

`require_fresh_conformance` compares `machine`, the bwrap digest and the filter digest. A package upgrade then means "run `isolation verify` again", the same rule the manual already gives for a buildctl upgrade. Kernel is recorded but not compared, since hosts patch kernels routinely.

**bubblewrap 0.8.0 or newer** is required (`--disable-userns`). Older versions refuse at `available()` with a message naming the version found. Ubuntu 22.04 ships 0.6.1 and RHEL 9 ships 0.6.3 (checked 2026-10-14), so the manual says to install a newer bubblewrap there or to upgrade the host. Debian 12 ships 0.8.0 and qualifies. The spike only ran 0.9.0, so acceptance includes a 22.04 runner to prove the refusal.

### Refusals that name the fix

Isolation failures are not stop codes. They surface from `isolation verify`, from `bootstrap`, and as `isolation.error` in `buildctl inspect`, which the Setup view shows. `verify_backend` runs one bare `bwrap ... -- /bin/true` before the probe and classifies the failure:

| bwrap says | Message |
|---|---|
| `setting up uid map: Permission denied`, `Failed RTM_NEWADDR`, under `apparmor_restrict_unprivileged_userns=1` | `ISOLATION_HOST_UNSUPPORTED: Ubuntu restricts user namespaces. Load the bwrap-userns-restrict AppArmor profile; see the manual, Running on Linux.` |
| `No permissions to create new namespace` and `kernel.unprivileged_userns_clone=0` | `... Debian disables user namespaces: set kernel.unprivileged_userns_clone=1 ...` |
| same, inside a container (`/.dockerenv` or `container` in the environment) | `... This container's seccomp profile blocks namespaces. Run it with the three flags in Running on Linux, or under gVisor or Sysbox ...` |
| `Can't mount proc` | `... Docker masks /proc: add --security-opt systempaths=unconfined ...` |
| `user.max_user_namespaces` is 0 | `... set user.max_user_namespaces above 0 ...` |
| anything else | `ISOLATION_HOST_UNSUPPORTED: bwrap failed: <first line>` |

The prefix gets an anchor in the manual, so the console links it the way it links stop codes.

### The manual

- `01-start.md` and the README: "macOS or Linux".
- `09-limits.md`: drop "macOS only"; add "Linux needs unprivileged user namespaces; stock containers refuse".
- New section **Running on Linux** (`#linux`) in `01-start.md`, written for an operator who has just been refused:
  - Ubuntu 24.04 and GitHub runners: `apt install bubblewrap apparmor-profiles`, then copy and load `bwrap-userns-restrict` (two commands, the ones the spike workflow runs).
  - Debian: `kernel.unprivileged_userns_clone=1`.
  - Containers: `--security-opt seccomp=<profile allowing unshare and clone>` (or `unconfined`), `--security-opt apparmor=unconfined`, `--security-opt systempaths=unconfined`. Or run the container under gVisor or Sysbox. Kubernetes: the same through the pod's `securityContext`.
  - Authentication: `auth_mode: environment` only.
  - Network on means the whole network, including the cloud metadata endpoint. Block it at the host (IMDSv2 hop limit 1, a firewall rule) until allowlists ship.
- `08-reference.md`: document `ISOLATION_HOST_UNSUPPORTED`. A docs test must fail if a classified message has no anchor.

### Trust

`isolation_policy_hash` covers `isolation.py`, and `trust init` digests the package. Every governed repo re-trusts after this ships. That is the same chore `f7b8219` already left pending, so do both together.

## Out of scope

- **Stock containers.** They refuse; the manual names the flags. No Landlock tier.
- **Network allowlists** (`network: allowlist` through a buildctl proxy). Its own spec, starting on macOS, where `sandbox-exec` can already limit outbound to localhost.
- **Distros beyond Ubuntu and Debian.** Fedora and Arch should work (namespaces on, bwrap current) but are not verified.
- **A Linux operator console.** The UI reads the kernel and should just work; it is not tested here.

## Acceptance

1. Unit tests for `LinuxBubblewrapBackend.command()` that assert the argument order (later deny after earlier bind), the missing-path refusals, `host_session` refusal, and the network on and off variants. These run on macOS.
2. Unit tests for `offline_seccomp_filter()` that decode the program and walk it for each machine: AF_UNIX denied, AF_INET allowed, io_uring denied, a foreign arch killed, an x32 number killed.
3. A CI workflow (owner-only, the `docs.yml` guard) on `ubuntu-24.04`, `ubuntu-24.04-arm` and `ubuntu-22.04` that asserts:
   - the probe refuses before the fix, and the message names AppArmor;
   - after the fix, `verify_backend` passes all roles, including the two new checks;
   - on 22.04 the refusal names the bwrap version;
   - the kernel test suite passes on Linux.
4. `verify_backend` on macOS passes with `unix_socket_connect` added.
5. The manual's `Running on Linux` commands are the ones CI runs, so a test compares them.
6. `validate_config` rejects `host_cli` on Linux.

## Open decisions

1. **`host_cli` for the implementer on Linux.** Recommended: reject, for the enterprise reasons above. The alternative is to allow it, since staging copies a login into an isolated home and never exposes the real HOME. That works, but it puts a personal ChatGPT login on a shared server.
2. **Should the Linux CI job run the whole kernel test suite?** Recommended: yes. Several tests use `MacOSSandboxBackend` directly and will need a skip on Linux, and finding those is cheap now.

## Build order

1. Backend hardening and its unit tests (missing-path refusals, the filter pinned at verify).
2. Probe checks on both backends, then the conformance record's `host` block.
3. Classified refusals and `validate_config` for `host_cli`.
4. The CI workflow, replacing the spike's.
5. The manual, then `__version__` 0.2.0, since this is the first release that changes what buildctl runs on.
6. Delete `spike/`, merge, then re-trust each governed repo.
