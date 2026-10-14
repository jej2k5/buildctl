from __future__ import annotations

import os
import platform
import shutil
import subprocess
import tempfile
import json
import sys
import hashlib
import socket
import struct
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .safety import ensure_safe_relative


class IsolationError(RuntimeError):
    pass


@dataclass(frozen=True)
class IsolationContext:
    role: str
    workspace: Path
    control_root: Path
    authoritative_repo: Path
    read_only_workspace: bool = False
    output_dir: Path | None = None
    home_dir: Path | None = None
    network_allowed: bool = False
    host_home: Path | None = None
    # Host-session roles keep the operator's real HOME so an interactive provider login
    # resolves. They remain OS-sandboxed under a distinct, more restrictive profile.
    host_session: bool = False

    def canonical(self) -> "IsolationContext":
        return IsolationContext(self.role, self.workspace.resolve(), self.control_root.resolve(),
                                self.authoritative_repo.resolve(), self.read_only_workspace,
                                self.output_dir.resolve() if self.output_dir else None,
                                self.home_dir.resolve() if self.home_dir else None,
                                self.network_allowed, self.host_home.resolve() if self.host_home else None,
                                self.host_session)


class IsolationBackend:
    name = "abstract"

    def available(self) -> bool:
        raise NotImplementedError

    def command(self, argv: list[str], context: IsolationContext) -> list[str]:
        raise NotImplementedError

    def preflight(self) -> None:
        """Refuse, naming the fix, when this host cannot run the backend at all."""

    def prepare(self, control_root: Path) -> None:
        """Write anything command() needs into the trusted control root. Runs at verify."""

    def host_facts(self) -> dict[str, str]:
        """What the conformance record pins about this host, beyond the OS string."""
        return {"machine": platform.machine()}


class MacOSSandboxBackend(IsolationBackend):
    name = "macos-sandbox-exec-v2-deny-default"

    def available(self) -> bool:
        return platform.system() == "Darwin" and Path("/usr/bin/sandbox-exec").is_file()

    def host_session_command(self, argv: list[str], context: IsolationContext) -> list[str]:
        """Profile for a host-authenticated read-only role.

        The operator's real HOME stays readable so an interactive provider login resolves,
        while the trusted control root, the canonical repository, other operations' runtime
        credentials, and every credential store remain denied. The phase workspace is
        readable evidence and is never writable, so workspace protection is enforced by the
        OS rather than by after-the-fact digest comparison.
        """
        if not self.available():
            raise IsolationError("macOS sandbox backend is unavailable")
        context = context.canonical()
        if context.host_home is None or context.output_dir is None:
            raise IsolationError("host-session isolation requires a host home and an output directory")
        def literal(path: Path) -> str:
            return json.dumps(str(path))
        home = context.host_home
        runtime_area = (context.workspace / ".factory-runtime").resolve()
        rules = ["(version 1)", "(deny default)", "(allow process*)", "(allow signal)",
                 "(allow ipc*)", "(allow mach*)",
                 '(deny mach-lookup (global-name "com.apple.securityd") (global-name "com.apple.security.agent"))',
                 "(allow sysctl-read)",
                 "(allow file-read*)",
                 '(allow file-write* (literal "/dev/null"))',
                 f"(deny file-write* (subpath {literal(home)}))",
                 f"(deny file-write* (subpath {literal(context.workspace)}))",
                 f"(deny file-read* file-write* (subpath {literal(context.control_root)}))",
                 f"(deny file-read* file-write* (subpath {literal(context.authoritative_repo)}))"]
        rules.extend(f"(deny file-read* file-write* (subpath {literal((home / name).resolve())}))"
                     for name in (".ssh", ".aws", ".config/gh", ".gnupg", ".docker"))
        rules.extend(f"(deny file-read* file-write* (literal {literal(home / name)}))"
                     for name in (".gitconfig", ".git-credentials", ".netrc"))
        # Other operations' runtimes hold staged implementer credentials; only this
        # operation's own output directory is re-allowed, after the class deny.
        rules.append(f"(deny file-read* file-write* (subpath {literal(runtime_area)}))")
        rules.append(f"(allow file-read-metadata (literal {literal(runtime_area)}))")
        rules.append(f"(allow file-read* file-write* (subpath {literal(context.output_dir)}))")
        if context.network_allowed:
            rules.append("(allow network-outbound)")
        return ["/usr/bin/sandbox-exec", "-p", " ".join(rules), *argv]

    def command(self, argv: list[str], context: IsolationContext) -> list[str]:
        if not self.available():
            raise IsolationError("macOS sandbox backend is unavailable")
        if context.host_session:
            return self.host_session_command(argv, context)
        context = context.canonical()
        def literal(path: Path) -> str:
            return json.dumps(str(path))
        readable = [Path("/System"), Path("/usr"), Path("/bin"), Path("/sbin"),
                    Path("/Library"), Path("/private/etc"), Path("/private/var"), context.workspace]
        executable = Path(argv[0]).resolve()
        readable.append(Path("/opt/homebrew") if executable.is_relative_to(Path("/opt/homebrew"))
                        else executable.parent)
        writable = [] if context.read_only_workspace else [context.workspace]
        if context.output_dir: readable.append(context.output_dir); writable.append(context.output_dir)
        if context.home_dir: readable.append(context.home_dir); writable.append(context.home_dir)
        runtime_area = (context.workspace / ".factory-runtime").resolve()
        rules = ["(version 1)", "(deny default)", "(allow process*)", "(allow signal)",
                 "(allow ipc*)", "(allow mach*)",
                 '(deny mach-lookup (global-name "com.apple.securityd") (global-name "com.apple.security.agent"))',
                 "(allow file-read*)",
                 # Rust/Node provider CLIs read sysctl during startup to size the stack
                 # guard page; without this they abort before argument parsing.
                 "(allow sysctl-read)",
                 '(allow file-write* (literal "/dev/null"))']
        rules.extend(f"(allow file-write* (subpath {literal(path.resolve())}))" for path in writable)
        # Runtime credentials are denied as a class; only this operation's exact
        # runtime subtree is re-allowed after the deny. Cleanup is not auth.
        rules.append(f"(deny file-read* file-write* (subpath {literal(runtime_area)}))")
        if context.output_dir and context.output_dir.resolve().is_relative_to(runtime_area):
            # Metadata on the runtime directory node itself (not its contents) is required so
            # path canonicalization of CODEX_HOME can traverse it. Content reads, writes and
            # directory enumeration remain denied, so cross-role credential isolation holds.
            rules.append(f"(allow file-read-metadata (literal {literal(runtime_area)}))")
            rules.append(f"(allow file-read* file-write* (subpath {literal(context.output_dir)}))")
        rules.extend((f"(deny file-read* file-write* (subpath {literal(context.control_root)}))",
                      f"(deny file-read* file-write* (subpath {literal(context.authoritative_repo)}))"))
        rules.append(f"(deny file-write* (subpath {literal(context.workspace / '.git')}))")
        if context.host_home:
            rules.append(f"(deny file-read* file-write* (subpath {literal(context.host_home)}))")
        if context.network_allowed:
            rules.append("(allow network-outbound)")
        profile = " ".join(rules)
        return ["/usr/bin/sandbox-exec", "-p", profile, *argv]


# Classic BPF for seccomp, as bwrap --seccomp reads it: an array of struct sock_filter.
_SECCOMP_ARCH = {"x86_64": (0xC000003E, 41), "aarch64": (0xC00000B7, 198)}  # audit arch, socket nr
_IO_URING_SETUP = 425  # the same number on both architectures


def offline_seccomp_filter(machine: str | None = None) -> bytes:
    """Refuse socket(AF_UNIX) and io_uring; kill any other syscall ABI.

    macOS denies Unix-socket connects with the network, and a network namespace does not:
    docker.sock, an SSH agent or the session bus stay reachable through the read-only root.
    socketpair stays allowed. io_uring is refused because its socket ops bypass seccomp, and
    a foreign ABI (i386 socketcall, x32) is killed because it would bypass the number check.
    """
    machine = machine or platform.machine()
    if machine not in _SECCOMP_ARCH:
        raise IsolationError(f"no seccomp filter for {machine}")
    audit_arch, socket_nr = _SECCOMP_ARCH[machine]
    kill, deny, allow = 0x80000000, 0x00050000 | 13, 0x7FFF0000  # KILL_PROCESS, ERRNO(EACCES), ALLOW
    load, jeq, jge, ret = 0x20, 0x15, 0x35, 0x06
    program = [(load, 0, 0, 4), (jeq, 1, 0, audit_arch), (ret, 0, 0, kill), (load, 0, 0, 0)]
    if machine == "x86_64":
        program += [(jge, 0, 1, 0x40000000), (ret, 0, 0, kill)]
    program += [(jeq, 0, 1, _IO_URING_SETUP), (ret, 0, 0, deny),
                (jeq, 0, 3, socket_nr), (load, 0, 0, 16), (jeq, 0, 1, 1), (ret, 0, 0, deny),
                (ret, 0, 0, allow)]
    return b"".join(struct.pack("=HBBI", *instruction) for instruction in program)


BWRAP_MINIMUM = (0, 8, 0)  # --disable-userns
UNSUPPORTED = "ISOLATION_HOST_UNSUPPORTED"


def _read_setting(path: str) -> str | None:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


def classify_bwrap_failure(stderr: str, settings: dict[str, str | None] | None = None,
                           in_container: bool | None = None) -> str:
    """Turn bwrap's refusal into the operator's fix. bwrap names the symptom, not the cause."""
    if settings is None:
        settings = {name: _read_setting(f"/proc/sys/{name.replace('.', '/')}") for name in (
            "kernel.apparmor_restrict_unprivileged_userns", "kernel.unprivileged_userns_clone",
            "user.max_user_namespaces")}
    if in_container is None:
        in_container = Path("/.dockerenv").exists() or "container" in os.environ
    see = "See the manual, Running on Linux."
    if "Can't mount proc" in stderr:
        return f"{UNSUPPORTED}: Docker masks /proc. Add --security-opt systempaths=unconfined. {see}"
    if in_container:
        # Inside a container /proc/sys shows the host's settings, so name the container first.
        return (f"{UNSUPPORTED}: this container blocks user namespaces. Run it with the three "
                f"flags in the manual, or under gVisor or Sysbox. {see}")
    if settings.get("user.max_user_namespaces") == "0":
        return f"{UNSUPPORTED}: user namespaces are off. Set user.max_user_namespaces above 0. {see}"
    if settings.get("kernel.unprivileged_userns_clone") == "0":
        return (f"{UNSUPPORTED}: Debian disables user namespaces. "
                f"Set kernel.unprivileged_userns_clone=1. {see}")
    if settings.get("kernel.apparmor_restrict_unprivileged_userns") == "1":
        return (f"{UNSUPPORTED}: Ubuntu restricts user namespaces. "
                f"Load the bwrap-userns-restrict AppArmor profile. {see}")
    first = (stderr.strip().splitlines() or ["no output"])[0][:200]
    return f"{UNSUPPORTED}: bwrap failed: {first}"


class LinuxBubblewrapBackend(IsolationBackend):
    """The macOS profile as a bubblewrap mount layout, rule for rule and in the same order.

    The root is bound read-only (macOS: allow file-read*). Writable paths are bound over it,
    and each deny is an empty read-only tmpfs mounted after them, so a later deny wins as it
    does in the profile. Symlinked aliases resolve into the tmpfs. No network namespace is
    shared unless the role allows network, and then a seccomp filter also refuses Unix sockets.
    """
    name = "linux-bwrap-v1"
    executable = Path("/usr/bin/bwrap")
    filter_name = "seccomp-offline.bpf"

    def available(self) -> bool:
        return platform.system() == "Linux" and self.executable.is_file()

    def version(self) -> tuple[int, ...]:
        completed = subprocess.run([str(self.executable), "--version"], capture_output=True, text=True)
        try:
            return tuple(int(part) for part in completed.stdout.split()[-1].split("."))
        except (IndexError, ValueError) as exc:
            raise IsolationError(f"{UNSUPPORTED}: cannot read the bwrap version") from exc

    def preflight(self) -> None:
        if not self.available():
            raise IsolationError(f"{UNSUPPORTED}: install bubblewrap at {self.executable}")
        found = self.version()
        if found < BWRAP_MINIMUM:
            raise IsolationError(f"{UNSUPPORTED}: bubblewrap {'.'.join(map(str, found))} is too old; "
                                 f"0.8.0 or newer is required. See the manual, Running on Linux.")
        trial = subprocess.run([*self._base(network_allowed=False), "--", "/bin/true"],
                               capture_output=True, text=True, timeout=15)
        if trial.returncode != 0:
            raise IsolationError(classify_bwrap_failure(trial.stderr))

    def prepare(self, control_root: Path) -> None:
        control_root = control_root.resolve()
        control_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        program = control_root / self.filter_name
        staged = program.with_name(f"{self.filter_name}.{os.getpid()}.tmp")
        staged.write_bytes(offline_seccomp_filter()); staged.replace(program)

    def host_facts(self) -> dict[str, str]:
        return {"machine": platform.machine(), "bwrap": str(self.executable),
                "bwrap_version": ".".join(map(str, self.version())),
                "bwrap_sha256": hashlib.sha256(self.executable.read_bytes()).hexdigest(),
                "seccomp_sha256": hashlib.sha256(offline_seccomp_filter()).hexdigest()}

    def _base(self, network_allowed: bool) -> list[str]:
        # No nested user namespace: nothing inside can gain mount rights over the deny layout.
        args = [str(self.executable), "--die-with-parent", "--new-session", "--unshare-user",
                "--disable-userns", "--unshare-pid", "--unshare-ipc", "--unshare-uts",
                "--unshare-cgroup-try", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc"]
        return args if network_allowed else [*args, "--unshare-net"]

    def command(self, argv: list[str], context: IsolationContext) -> list[str]:
        if not self.available():
            raise IsolationError("Linux bubblewrap backend is unavailable")
        if context.host_session:
            # Linux hosts authenticate agents per operation; no host login is ever readable.
            raise IsolationError("host-session isolation is not supported on Linux")
        context = context.canonical()
        git_dir = context.workspace / ".git"
        for required in (context.control_root, context.authoritative_repo, git_dir):
            # A deny over a missing path would let the sandbox create it.
            if not required.exists():
                raise IsolationError(f"isolation requires {required} to exist")
        args = self._base(context.network_allowed)
        def bind(path: Path) -> None:
            args.extend(("--bind", str(path), str(path)))
        def deny(path: Path) -> None:
            args.extend(("--perms", "0000", "--tmpfs", str(path), "--remount-ro", str(path)))
        if not context.read_only_workspace:
            bind(context.workspace)
        # Runtime credentials are denied as a class and only this operation's output is bound
        # back. The tmpfs is traversable but not listable (macOS: metadata on the node only),
        # and goes read-only after the bind so its mount point can be created first.
        runtime_area = (context.workspace / ".factory-runtime").resolve()
        if runtime_area.exists():
            args.extend(("--perms", "0111", "--tmpfs", str(runtime_area)))
        for path in (context.output_dir, context.home_dir):
            if path:
                bind(path)
        if runtime_area.exists():
            args.extend(("--remount-ro", str(runtime_area)))
        deny(context.control_root)
        deny(context.authoritative_repo)
        args.extend(("--ro-bind", str(git_dir), str(git_dir)))
        if context.host_home and context.host_home.exists():
            deny(context.host_home)
        if context.network_allowed:
            return [*args, "--", *argv]
        # bwrap reads the filter from a descriptor. The shell opens it from the control root,
        # which the sandbox cannot see, and then becomes bwrap. It was written at verify.
        program = context.control_root / self.filter_name
        try:
            written = program.read_bytes()
        except OSError:
            written = b""
        if written != offline_seccomp_filter():
            raise IsolationError("the offline seccomp filter is missing or changed; run buildctl isolation verify")
        return ["/bin/sh", "-c", 'exec "$@" 3<"$0"', str(program),
                *args, "--seccomp", "3", "--", *argv]


class FakeIsolationBackend(IsolationBackend):
    """Test-only backend; never accepted by production bootstrap."""
    name = "fake-test-only"

    def available(self) -> bool:
        return True

    def command(self, argv: list[str], context: IsolationContext) -> list[str]:
        return argv


def production_backend() -> IsolationBackend:
    for backend in (MacOSSandboxBackend(), LinuxBubblewrapBackend()):
        if backend.available():
            return backend
    raise IsolationError("no conformant IsolationBackend is available; autonomous execution is blocked")


def isolation_policy_hash(backend: IsolationBackend, snapshot_digest: str) -> str:
    material = Path(__file__).read_bytes() + backend.name.encode() + snapshot_digest.encode()
    return hashlib.sha256(material).hexdigest()


def probe_owned_path(control_root: Path, relative: str) -> Path:
    """Resolve a probe path and reject anything outside the probe-owned subtree."""
    root = control_root.resolve()
    candidate = ensure_safe_relative(root, relative)
    probe_root = ensure_safe_relative(root, ".isolation-probe").resolve()
    if not candidate.resolve().is_relative_to(probe_root):
        raise IsolationError("isolation probe path must remain inside .isolation-probe")
    return candidate


def conformance_record(control_root: Path, backend: IsolationBackend, snapshot_digest: str) -> dict[str, Any]:
    return {"verified_at": datetime.now(timezone.utc).isoformat(), "os": platform.platform(),
            "host": backend.host_facts(), "backend": backend.name, "policy_hash": isolation_policy_hash(backend, snapshot_digest),
            "snapshot_digest": snapshot_digest, "status": "PASS"}


def require_fresh_conformance(control_root: Path, backend: IsolationBackend, snapshot_digest: str,
                              max_age_hours: int = 24) -> None:
    path = control_root.resolve() / "isolation-conformance.json"
    try:
        value = json.loads(path.read_text())
        verified = datetime.fromisoformat(value["verified_at"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise IsolationError("production isolation has not been verified; run buildctl isolation verify") from exc
    expected = conformance_record(control_root, backend, snapshot_digest)
    for key in ("os", "host", "backend", "policy_hash", "snapshot_digest", "status"):
        if value.get(key) != expected[key]:
            raise IsolationError("production isolation conformance is stale or has the wrong policy binding")
    if datetime.now(timezone.utc) - verified > timedelta(hours=max_age_hours):
        raise IsolationError("production isolation conformance result is stale")


def verify_backend(backend: IsolationBackend, control_root: Path, authoritative_repo: Path,
                   runner: Callable[..., Any] = subprocess.run) -> dict[str, Any]:
    """Prove the OS boundary before any agent can be invoked."""
    control_root = control_root.resolve(); authoritative_repo = authoritative_repo.resolve()
    control_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    probe_root = ensure_safe_relative(control_root, ".isolation-probe")
    if probe_root.exists() and not probe_root.is_dir():
        raise IsolationError("isolation probe path is not a directory")
    probe_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if probe_root.stat().st_mode & 0o777 != 0o700:
        raise IsolationError("isolation probe directory must be mode 0700")
    sentinel = probe_owned_path(control_root, ".isolation-probe/isolation-sentinel")
    sentinel.write_text("not-agent-readable")
    trusted_config = probe_owned_path(control_root, ".isolation-probe/trusted-config")
    if trusted_config.exists() and trusted_config.is_symlink():
        raise IsolationError("isolation probe target cannot be a symlink")
    trusted_config.write_text("trusted")
    backend.preflight()
    backend.prepare(control_root)
    results: dict[str, Any] = {"backend": backend.name, "roles": {}}
    with tempfile.TemporaryDirectory(prefix="buildctl-isolation-check-") as directory:
        workspace = Path(directory).resolve()
        subprocess.run(["/usr/bin/git", "init", "-q", str(workspace)], check=True, capture_output=True)
        remote_parent = Path(tempfile.mkdtemp(prefix="buildctl-push-fixture-", dir=probe_root)).resolve()
        remote = remote_parent / "remote.git"
        subprocess.run(["/usr/bin/git", "init", "--bare", "-q", str(remote)], check=True, capture_output=True)
        subprocess.run(["/usr/bin/git", "-C", str(workspace), "remote", "add", "origin", str(remote)],
                       check=True, capture_output=True)
        subprocess.run(["/usr/bin/git", "-C", str(workspace), "-c", "user.name=probe",
                        "-c", "user.email=probe@invalid", "commit", "--allow-empty", "-qm", "probe"],
                       check=True, capture_output=True)
        (workspace / "probe-file").write_text("staging probe\n")
        runtime = (workspace / ".factory-runtime").resolve(); runtime.mkdir()
        home = (workspace / ".probe-host-home").resolve(); home.mkdir()
        (home / ".codex").mkdir(); (home / ".codex/auth.json").write_text('{"fixture_token":"do-not-leak"}')
        (home / ".ssh").mkdir(); (home / ".ssh/id_rsa").write_text("secret")
        (home / ".gitconfig").write_text("[credential]\nhelper=osxkeychain\n")
        (home / ".config/gh").mkdir(parents=True); (home / ".config/gh/hosts.yml").write_text("oauth_token: secret")
        (home / ".claude.json").write_text('{"token":"secret"}')
        (home / "Library/Keychains").mkdir(parents=True); (home / "Library/Keychains/login.keychain-db").write_text("secret")
        control_alias = workspace / "control-alias"; control_alias.symlink_to(control_root, target_is_directory=True)
        repo_alias = workspace / "repo-alias"; repo_alias.symlink_to(authoritative_repo, target_is_directory=True)
        probe = authoritative_repo / ".buildctl-isolation-probe"
        listener = socket.socket(); listener.bind(("127.0.0.1", 0)); listener.listen(5)
        # A host Unix socket (docker.sock, an SSH agent, the session bus) is network too. It sits
        # outside the workspace and outside every deny, so only the network rule can refuse it.
        socket_dir = Path(tempfile.mkdtemp(prefix="buildctl-sock-", dir="/tmp")).resolve()
        unix_path = socket_dir / "host.sock"
        unix_listener = socket.socket(socket.AF_UNIX); unix_listener.bind(str(unix_path)); unix_listener.listen(16)
        unix_connect = ["/usr/bin/python3", "-c",
                        f"import socket; socket.socket(socket.AF_UNIX).connect({str(unix_path)!r})"]
        network_port = listener.getsockname()[1]
        for role, read_only in (("implementer", False), ("reviewer", True), ("gate", False)):
            target = workspace / f"{role}-write"
            output = runtime / f"{role}-output"; output.mkdir()
            isolated_home = output / "home"; isolated_home.mkdir()
            staged_auth = isolated_home / ".codex/auth.json"
            if role == "implementer":
                staged_auth.parent.mkdir(mode=0o700); staged_auth.write_text('{"fixture_token":"staged-only"}')
                os.chmod(staged_auth, 0o600)
            stale_auth = runtime / "implementer-stale/home/.codex/auth.json"
            stale_auth.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            stale_auth.write_text('{"fixture_token":"stale-implementer-only"}')
            os.chmod(stale_auth, 0o600)
            context = IsolationContext(role, workspace, control_root, authoritative_repo, read_only,
                                       output, isolated_home, False, home)
            environment = sanitized_environment(role, home=isolated_home)
            diagnostics: dict[str, str] = {}
            def execute(argv: list[str], extra_env: dict[str, str] | None = None) -> Any:
                command_env = dict(environment); command_env.update(extra_env or {})
                result = runner(backend.command(argv, context), cwd=workspace, text=True, capture_output=True,
                                timeout=15, env=command_env)
                diagnostics[" ".join(argv[:2])] = f"rc={result.returncode} stderr={(result.stderr or '')[:160]}"
                return result
            def succeeds(argv: list[str]) -> bool:
                return execute(argv).returncode == 0
            network_context = IsolationContext(role, workspace, control_root, authoritative_repo, read_only,
                                               output, isolated_home, True, home)
            unix_prerequisite_result = runner(backend.command(unix_connect, network_context), cwd=workspace,
                                              text=True, capture_output=True, timeout=15, env=environment)
            network_prerequisite_result = runner(
                backend.command(["/usr/bin/nc", "-z", "-w", "1", "127.0.0.1", str(network_port)],
                                network_context),
                cwd=workspace, text=True, capture_output=True, timeout=15, env=environment)
            devnull_script = ("import os, subprocess\n"
                              "fds=[os.open('/dev/null', f) for f in (os.O_RDONLY,os.O_WRONLY,os.O_RDWR)]\n"
                              "[os.close(fd) for fd in fds]\n"
                              "subprocess.run(['/bin/echo','ok'],stdout=subprocess.DEVNULL,check=True)\n"
                              "subprocess.run(['/bin/sh','-c','echo err >&2'],stderr=subprocess.DEVNULL,check=True)\n")
            devnull = succeeds(["/usr/bin/python3", "-c", devnull_script])
            git_version = succeeds(["/usr/bin/git", "--version"])
            git_status = succeeds(["/usr/bin/git", "-C", str(workspace), "status", "--porcelain"])
            git_diff = succeeds(["/usr/bin/git", "-C", str(workspace), "diff", "--no-ext-diff"])
            git_add_result = execute(["/usr/bin/git", "-C", str(workspace), "add", "--", "probe-file"])
            git_add_text = (git_add_result.stdout or "") + (git_add_result.stderr or "")
            push = execute(["/usr/bin/git", "-C", str(workspace), "push", "origin", "HEAD"],
                           {"GIT_TRACE": "1"})
            push_text = (push.stdout or "") + (push.stderr or "")
            push_reached = ("git-receive-pack" in push_text or "receive-pack" in push_text) and "/dev/null" not in push_text
            observed = {
                "devnull_functional": devnull,
                "git_version": git_version, "git_status": git_status, "git_diff": git_diff,
                "git_add_reached_index": git_add_result.returncode != 0
                    and "/dev/null" not in git_add_text and "pathspec" not in git_add_text
                    and ("index" in git_add_text.lower() or "permission" in git_add_text.lower()
                         or "operation not permitted" in git_add_text.lower()),
                "control_read": succeeds(["/bin/cat", str(sentinel)]),
                "trusted_config_read": succeeds(["/bin/cat", str(trusted_config)]),
                "repo_write": succeeds(["/usr/bin/touch", str(probe)]),
                "control_alias_read": succeeds(["/bin/cat", str(control_alias / "isolation-sentinel")]),
                "repo_alias_write": succeeds(["/usr/bin/touch", str(repo_alias / ".alias-probe")]),
                "workspace_write": succeeds(["/usr/bin/touch", str(target)]),
                "workspace_git_write": succeeds(["/usr/bin/touch", str(workspace / ".git/probe")]),
                "output_write": succeeds(["/usr/bin/touch", str(output / "result")]),
                "ssh_read": succeeds(["/bin/cat", str(home / ".ssh/id_rsa")]),
                "gh_read": succeeds(["/bin/cat", str(home / ".config/gh/hosts.yml")]),
                "gitconfig_read": succeeds(["/bin/cat", str(home / ".gitconfig")]),
                "claude_config_read": succeeds(["/bin/cat", str(home / ".claude.json")]),
                "keychain_read": succeeds(["/bin/cat", str(home / "Library/Keychains/login.keychain-db")]),
                "host_codex_auth_read": succeeds(["/bin/cat", str(home / ".codex/auth.json")]),
                "staged_codex_auth_read": succeeds(["/bin/cat", str(staged_auth)]),
                "stale_implementer_auth_read": succeeds(["/bin/cat", str(stale_auth)]),
                "home_write": succeeds(["/usr/bin/touch", str(home / "escape")]),
                "network_probe_prerequisite": network_prerequisite_result.returncode == 0,
                "network": succeeds(["/usr/bin/nc", "-z", "-w", "1", "127.0.0.1", str(network_port)]),
                "unix_socket_prerequisite": unix_prerequisite_result.returncode == 0,
                "unix_socket_connect": succeeds(unix_connect),
                "git_push_succeeded": push.returncode == 0,
                "git_push_reached_transport": push_reached,
                "git_push_failure_reason": "protected_remote_denied" if push_reached and push.returncode != 0 else "prerequisite_failure",
                "environment_clean": not any(key in environment for key in (
                    "GITHUB_TOKEN", "GH_TOKEN", "AWS_SECRET_ACCESS_KEY", "ANTHROPIC_API_KEY"))
                    and environment.get("SSH_AUTH_SOCK") == "",
            }
            expected_write = role != "reviewer"
            expected = {"devnull_functional": True, "git_version": True, "git_status": True, "git_diff": True,
                        "git_add_reached_index": True,
                        "control_read": False, "trusted_config_read": False, "repo_write": False,
                        "control_alias_read": False, "repo_alias_write": False, "workspace_write": expected_write,
                        "workspace_git_write": False,
                        "output_write": True,
                        "ssh_read": False, "gh_read": False, "gitconfig_read": False,
                        "claude_config_read": False, "keychain_read": False,
                        "host_codex_auth_read": False, "staged_codex_auth_read": role == "implementer",
                        "stale_implementer_auth_read": False,
                        "home_write": False, "network_probe_prerequisite": True, "network": False,
                        "git_push_succeeded": False, "git_push_reached_transport": True,
                        "git_push_failure_reason": "protected_remote_denied", "environment_clean": True,
                        "unix_socket_prerequisite": True, "unix_socket_connect": False}
            if isinstance(backend, LinuxBubblewrapBackend):
                # Nested user namespaces would give the sandbox mount rights over the deny layout.
                observed["nested_userns"] = succeeds(["/usr/bin/unshare", "-U", "/bin/true"])
                expected["nested_userns"] = False
            if observed != expected:
                raise IsolationError(f"isolation backend is nonconformant for {role}: {observed}; {diagnostics}")
            results["roles"][role] = observed
        listener.close(); unix_listener.close(); shutil.rmtree(socket_dir)
        if probe.exists():
            raise IsolationError("isolation probe modified the authoritative repository")
        if subprocess.run(["/usr/bin/git", "--git-dir", str(remote), "show-ref"], capture_output=True).stdout:
            raise IsolationError("sandboxed git push mutated the protected fixture remote")
        shutil.rmtree(remote_parent)
    try:
        trusted_config.unlink()
    except FileNotFoundError:
        pass
    return results


def sanitized_environment(role: str, credentials: dict[str, str] | None = None,
                          home: Path | None = None) -> dict[str, str]:
    allow = ("PATH", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR")
    result = {key: os.environ[key] for key in allow if key in os.environ}
    result.update(credentials or {})
    if home:
        home = home.resolve(); temporary = home / "tmp"; temporary.mkdir(parents=True, exist_ok=True)
        result.update({"HOME": str(home), "TMPDIR": str(temporary), "GIT_CONFIG_GLOBAL": "/dev/null",
                       "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
                       "GCM_INTERACTIVE": "never", "SSH_AUTH_SOCK": ""})
    result["BUILDCTL_SUBPROCESS_ROLE"] = role
    if role == "gate":
        result["BUILDCTL_GATE_PROCESS"] = "1"
    return result
