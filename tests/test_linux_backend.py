"""The Linux backend: its mount layout, its seccomp filter and its refusals.

Everything but the last class runs on any host: the layout is checked as arguments and the
filter is run through a small BPF interpreter. The last class needs Linux with bubblewrap.
"""
import json
import platform
import struct
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from buildctl.supervisor import phases
from buildctl.supervisor.isolation import (IsolationContext, IsolationError, LinuxBubblewrapBackend,
                                         classify_bwrap_failure, conformance_record, offline_seccomp_filter,
                                         require_fresh_conformance, verify_backend)

TARGET = Path(__file__).resolve().parent / "fixtures" / "target"
AUDIT_ARCH = {"x86_64": 0xC000003E, "aarch64": 0xC00000B7}
SOCKET = {"x86_64": 41, "aarch64": 198}
AF_UNIX, AF_INET, EACCES = 1, 2, 13


def run_filter(program: bytes, nr: int, arch: int, arg0: int = 0) -> str:
    """Interpret the four instructions the filter uses against one seccomp_data record."""
    data = struct.pack("<iIQ6Q", nr, arch, 0, arg0, 0, 0, 0, 0, 0)
    code = [struct.unpack("=HBBI", program[i:i + 8]) for i in range(0, len(program), 8)]
    accumulator, pc = 0, 0
    while True:
        op, jt, jf, k = code[pc]
        if op == 0x20:
            accumulator = struct.unpack_from("<I", data, k)[0]; pc += 1
        elif op in (0x15, 0x35):
            taken = accumulator == k if op == 0x15 else accumulator >= k
            pc += 1 + (jt if taken else jf)
        elif op == 0x06:
            if k == 0x7FFF0000:
                return "allow"
            if k == 0x80000000:
                return "kill"
            return f"errno {k & 0xFFFF}"
        else:
            raise AssertionError(f"unexpected opcode {op:#x}")


class SeccompFilterTests(unittest.TestCase):
    def test_unix_sockets_and_io_uring_are_refused_and_inet_is_not(self):
        for machine in ("x86_64", "aarch64"):
            program, arch = offline_seccomp_filter(machine), AUDIT_ARCH[machine]
            with self.subTest(machine=machine):
                self.assertEqual(run_filter(program, SOCKET[machine], arch, AF_UNIX), f"errno {EACCES}")
                self.assertEqual(run_filter(program, SOCKET[machine], arch, AF_INET), "allow")
                self.assertEqual(run_filter(program, 425, arch), f"errno {EACCES}")
                self.assertEqual(run_filter(program, 63, arch), "allow")  # an ordinary call

    def test_a_foreign_abi_is_killed(self):
        for machine in ("x86_64", "aarch64"):
            with self.subTest(machine=machine):
                # i386 socketcall on x86_64, or any other architecture, would dodge the number check
                self.assertEqual(run_filter(offline_seccomp_filter(machine), 102, 0x40000003), "kill")
        x32_socket = 0x40000000 + SOCKET["x86_64"]
        self.assertEqual(run_filter(offline_seccomp_filter("x86_64"), x32_socket, AUDIT_ARCH["x86_64"], AF_UNIX),
                         "kill")

    def test_an_unknown_machine_refuses(self):
        with self.assertRaises(IsolationError):
            offline_seccomp_filter("riscv64")


class LinuxLayoutTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        root = Path(directory.name).resolve()
        self.workspace = root / "workspace"; (self.workspace / ".git").mkdir(parents=True)
        self.control = root / "control"; self.control.mkdir()
        self.repo = root / "repo"; self.repo.mkdir()
        self.runtime = self.workspace / ".factory-runtime"
        self.output = self.runtime / "implementer-1"; self.home = self.output / "home"; self.home.mkdir(parents=True)
        self.host_home = root / "host-home"; self.host_home.mkdir()
        patcher = mock.patch.object(LinuxBubblewrapBackend, "available", return_value=True)
        patcher.start(); self.addCleanup(patcher.stop)
        self.backend = LinuxBubblewrapBackend()
        with mock.patch("buildctl.supervisor.isolation.platform.machine", return_value="x86_64"):
            self.backend.prepare(self.control)
        machine = mock.patch("buildctl.supervisor.isolation.platform.machine", return_value="x86_64")
        machine.start(); self.addCleanup(machine.stop)

    def context(self, **overrides):
        values = dict(role="implementer", workspace=self.workspace, control_root=self.control,
                      authoritative_repo=self.repo, output_dir=self.output, home_dir=self.home,
                      host_home=self.host_home)
        values.update(overrides)
        return IsolationContext(**values)

    @staticmethod
    def position(command, *sequence):
        for index in range(len(command) - len(sequence) + 1):
            if tuple(command[index:index + len(sequence)]) == sequence:
                return index
        raise AssertionError(f"{sequence} not in {command}")

    def test_offline_commands_run_bwrap_with_the_filter_from_the_control_root(self):
        command = self.backend.command(["/bin/true"], self.context())
        self.assertEqual(command[:4], ["/bin/sh", "-c", 'exec "$@" 3<"$0"', str(self.control / "seccomp-offline.bpf")])
        self.assertEqual(command[4], "/usr/bin/bwrap")
        self.assertIn("--unshare-net", command)
        self.assertIn("--disable-userns", command)
        self.assertEqual(command[-4:], ["--seccomp", "3", "--", "/bin/true"])

    def test_a_later_deny_is_mounted_after_the_bind_it_overrides(self):
        command = self.backend.command(["/bin/true"], self.context())
        workspace = self.position(command, "--bind", str(self.workspace), str(self.workspace))
        runtime = self.position(command, "--perms", "0111", "--tmpfs", str(self.runtime))
        output = self.position(command, "--bind", str(self.output), str(self.output))
        sealed = self.position(command, "--remount-ro", str(self.runtime))
        control = self.position(command, "--perms", "0000", "--tmpfs", str(self.control))
        repo = self.position(command, "--perms", "0000", "--tmpfs", str(self.repo))
        git = self.position(command, "--ro-bind", str(self.workspace / ".git"), str(self.workspace / ".git"))
        host = self.position(command, "--perms", "0000", "--tmpfs", str(self.host_home))
        self.assertLess(self.position(command, "--ro-bind", "/", "/"), workspace)
        self.assertTrue(workspace < runtime < output < sealed < control < git)
        self.assertLess(control, repo)
        self.assertLess(git, host)

    def test_a_read_only_workspace_is_never_bound_writable(self):
        command = self.backend.command(["/bin/true"], self.context(role="reviewer", read_only_workspace=True))
        with self.assertRaises(AssertionError):
            self.position(command, "--bind", str(self.workspace), str(self.workspace))

    def test_network_roles_share_the_network_and_skip_the_filter(self):
        command = self.backend.command(["/bin/true"], self.context(network_allowed=True))
        self.assertEqual(command[0], "/usr/bin/bwrap")
        self.assertNotIn("--unshare-net", command)
        self.assertNotIn("--seccomp", command)

    def test_a_missing_protected_path_refuses(self):
        for missing in (self.control / "absent", self.repo / "absent"):
            with self.subTest(missing=missing), self.assertRaises(IsolationError):
                field = "control_root" if missing.parent == self.control else "authoritative_repo"
                self.backend.command(["/bin/true"], self.context(**{field: missing}))
        (self.workspace / ".git").rmdir()
        with self.assertRaisesRegex(IsolationError, ".git"):
            self.backend.command(["/bin/true"], self.context())

    def test_there_is_no_host_session_profile(self):
        with self.assertRaisesRegex(IsolationError, "host-session"):
            self.backend.command(["/bin/true"], self.context(host_session=True))

    def test_a_missing_or_changed_filter_refuses(self):
        program = self.control / "seccomp-offline.bpf"
        program.write_bytes(program.read_bytes()[:-8])
        with self.assertRaisesRegex(IsolationError, "isolation verify"):
            self.backend.command(["/bin/true"], self.context())
        program.unlink()
        with self.assertRaisesRegex(IsolationError, "isolation verify"):
            self.backend.command(["/bin/true"], self.context())

    def test_command_writes_nothing(self):
        before = sorted(path.name for path in self.control.iterdir())
        self.backend.command(["/bin/true"], self.context())
        self.assertEqual(sorted(path.name for path in self.control.iterdir()), before)


class LinuxRefusalTests(unittest.TestCase):
    QUIET = {"kernel.apparmor_restrict_unprivileged_userns": None, "kernel.unprivileged_userns_clone": None,
             "user.max_user_namespaces": "15000"}

    def classify(self, stderr="bwrap: setting up uid map: Permission denied", container=False, **settings):
        values = dict(self.QUIET); values.update({k.replace("__", "."): v for k, v in settings.items()})
        return classify_bwrap_failure(stderr, values, container)

    def test_each_cause_names_its_fix(self):
        self.assertIn("AppArmor", self.classify("bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted",
                                                kernel__apparmor_restrict_unprivileged_userns="1"))
        self.assertIn("unprivileged_userns_clone=1", self.classify(kernel__unprivileged_userns_clone="0"))
        self.assertIn("max_user_namespaces", self.classify(user__max_user_namespaces="0"))
        self.assertIn("systempaths=unconfined", self.classify("bwrap: Can't mount proc on /newroot/proc"))
        self.assertIn("gVisor", self.classify(container=True))
        self.assertNotIn("AppArmor", self.classify(container=True))
        # Measured on GitHub's Ubuntu 24.04: the three flags alone still refuse there.
        self.assertIn("AppArmor profile on the host",
                      self.classify(container=True, kernel__apparmor_restrict_unprivileged_userns="1"))
        self.assertIn("bwrap failed: something new", self.classify("something new\nmore"))
        for message in (self.classify(), self.classify(container=True)):
            self.assertTrue(message.startswith("ISOLATION_HOST_UNSUPPORTED: "))

    def test_an_old_bubblewrap_refuses_by_version(self):
        backend = LinuxBubblewrapBackend()
        with mock.patch.object(LinuxBubblewrapBackend, "available", return_value=True), \
                mock.patch.object(LinuxBubblewrapBackend, "version", return_value=(0, 6, 1)):
            with self.assertRaisesRegex(IsolationError, "bubblewrap 0.6.1 is too old"):
                backend.preflight()

    def test_host_cli_is_refused_on_linux(self):
        config = json.loads((TARGET / "build.yaml").read_text())
        with mock.patch.object(phases, "host_cli_supported", return_value=False):
            with self.assertRaisesRegex(phases.ConfigurationError, "role implementer: auth_mode host_cli is macOS only"):
                phases.validate_config(config, TARGET)
        with mock.patch.object(phases, "host_cli_supported", return_value=True):
            phases.validate_config(config, TARGET)

    def test_a_changed_host_makes_the_record_stale(self):
        with tempfile.TemporaryDirectory() as directory:
            control = Path(directory)
            backend = LinuxBubblewrapBackend()
            facts = {"machine": "x86_64", "bwrap": "/usr/bin/bwrap", "bwrap_version": "0.9.0",
                     "bwrap_sha256": "a" * 64, "seccomp_sha256": "b" * 64}
            with mock.patch.object(LinuxBubblewrapBackend, "host_facts", return_value=facts):
                record = conformance_record(control, backend, "digest")
                (control / "isolation-conformance.json").write_text(json.dumps(record))
                require_fresh_conformance(control, backend, "digest")
            upgraded = dict(facts, bwrap_version="0.10.0", bwrap_sha256="c" * 64)
            with mock.patch.object(LinuxBubblewrapBackend, "host_facts", return_value=upgraded):
                with self.assertRaisesRegex(IsolationError, "stale"):
                    require_fresh_conformance(control, backend, "digest")


@unittest.skipUnless(platform.system() == "Linux" and LinuxBubblewrapBackend().available(),
                     "bubblewrap backend not supported on this host")
class LinuxConformanceTests(unittest.TestCase):
    def test_the_real_backend_passes_the_probe(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve(); control = root / "control"; repo = root / "repo"; repo.mkdir()
            result = verify_backend(LinuxBubblewrapBackend(), control, repo)
            self.assertEqual(set(result["roles"]), {"implementer", "reviewer", "gate"})
            for observed in result["roles"].values():
                self.assertFalse(observed["unix_socket_connect"])
                self.assertFalse(observed["nested_userns"])
            self.assertTrue((control / "seccomp-offline.bpf").is_file())
            record = conformance_record(control, LinuxBubblewrapBackend(), "digest")
            self.assertGreaterEqual(tuple(map(int, record["host"]["bwrap_version"].split("."))), (0, 8, 0))
            self.assertLessEqual(abs((datetime.now(timezone.utc)
                                      - datetime.fromisoformat(record["verified_at"])).total_seconds()), 60)


if __name__ == "__main__":
    unittest.main()
