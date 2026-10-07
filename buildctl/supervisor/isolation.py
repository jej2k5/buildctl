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


class FakeIsolationBackend(IsolationBackend):
    """Test-only backend; never accepted by production bootstrap."""
    name = "fake-test-only"

    def available(self) -> bool:
        return True

    def command(self, argv: list[str], context: IsolationContext) -> list[str]:
        return argv


def production_backend() -> IsolationBackend:
    backend = MacOSSandboxBackend()
    if not backend.available():
        raise IsolationError("no conformant IsolationBackend is available; autonomous execution is blocked")
    return backend


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
            "backend": backend.name, "policy_hash": isolation_policy_hash(backend, snapshot_digest),
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
    for key in ("os", "backend", "policy_hash", "snapshot_digest", "status"):
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
                        "git_push_failure_reason": "protected_remote_denied", "environment_clean": True}
            if observed != expected:
                raise IsolationError(f"isolation backend is nonconformant for {role}: {observed}; {diagnostics}")
            results["roles"][role] = observed
        listener.close()
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
