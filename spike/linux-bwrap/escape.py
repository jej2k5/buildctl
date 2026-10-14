import os, socket, subprocess, tempfile, threading
from pathlib import Path
from buildctl.supervisor.isolation import IsolationContext, production_backend, sanitized_environment
backend = production_backend()
root = Path(tempfile.mkdtemp(prefix="esc-")).resolve()
ws = root / "ws"; ws.mkdir(); subprocess.run(["git", "init", "-q", str(ws)], check=True)
control = root / "control"; control.mkdir(); (control / "secret").write_text("trusted")
repo = root / "repo"; repo.mkdir()
out = ws / ".factory-runtime" / "op"; out.mkdir(parents=True); home = out / "home"; home.mkdir()
ctx = IsolationContext("implementer", ws, control, repo, False, out, home, False)
sock_path = "/tmp/host-agent.sock"
srv = socket.socket(socket.AF_UNIX); srv.bind(sock_path); srv.listen(1)
threading.Thread(target=lambda: srv.accept()[0].sendall(b"host socket reached"), daemon=True).start()
def run(label, argv):
    r = subprocess.run(backend.command(argv, ctx), cwd=ws, capture_output=True, text=True,
                       env=sanitized_environment("implementer", home=home), timeout=15)
    print(f"{label}: rc={r.returncode} out={r.stdout.strip()[:80]!r} err={r.stderr.strip()[:120]!r}")
run("unix socket connect, network off", ["/usr/bin/python3", "-c",
    f"import socket;s=socket.socket(socket.AF_UNIX);s.connect({sock_path!r});print(s.recv(64).decode())"])
run("nested userns, then unmount the control-root deny", ["/bin/sh", "-c",
    f"unshare -Urm sh -c 'umount {control} && cat {control}/secret'"])
run("nested userns at all", ["/usr/bin/unshare", "-Ur", "true"])
run("socketpair still works", ["/usr/bin/python3", "-c", "import socket;a,b=socket.socketpair();a.send(b'ok');print(b.recv(2).decode())"])
run("io_uring refused", ["/usr/bin/python3", "-c", "import ctypes,os;r=ctypes.CDLL(None,use_errno=True).syscall(425,1,ctypes.create_string_buffer(120));print(r,os.strerror(ctypes.get_errno()))"])
ctx = IsolationContext("implementer", ws, control, repo, False, out, home, True)
run("network allowed: unix socket (macOS parity: allowed)", ["/usr/bin/python3", "-c",
    f"import socket;s=socket.socket(socket.AF_UNIX);s.connect({sock_path!r});print('connected')"])
