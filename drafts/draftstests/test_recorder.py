"""The demo recorder: scenarios, Chrome's lockdown, and a real recording when Chrome is installed.

The end-to-end test serves a tiny page from a sandboxed demo (loopback only). The page tries to fetch a
service on the host's loopback; the recording must show that the fetch was refused, which proves Chrome's
proxy lockdown on macOS. On Linux, recording refuses: Chrome cannot run inside the offline sandbox.
"""
import json
import platform
import shutil
import socket
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from buildctl_drafts.recorder import Demo, RecordError, chrome_args, find_chrome, record, validate_scenario


def chrome() -> str | None:
    try:
        return find_chrome()
    except RecordError:
        return None


class ScenarioTests(unittest.TestCase):
    def test_a_good_scenario_passes(self):
        self.assertEqual(validate_scenario({"title": "t", "steps": [
            {"name": "open", "open": "#/drafts", "expect": "Release drafts", "hold": 2},
            {"name": "click", "click": "Launch post"},
            {"name": "type", "type": {"target": "css:input", "text": "abc"}},
            {"name": "wait", "wait_for": "Signed"}, {"name": "pause", "pause": 1}]}), [])

    def test_each_step_has_exactly_one_action(self):
        problems = validate_scenario({"title": "t", "steps": [{"name": "two", "open": "/", "click": "x"}, {"name": "none"}]})
        self.assertEqual(len([p for p in problems if "exactly one" in p]), 2)

    def test_unknown_keys_holds_and_shapes_are_refused(self):
        problems = " ".join(validate_scenario({"title": "t", "steps": [
            {"name": "a", "open": "/", "eval": "alert(1)"}, {"name": "b", "open": "/", "hold": 99},
            {"name": "c", "type": "text"}]}))
        self.assertIn("unknown keys eval", problems)
        self.assertIn("0 to 15", problems)
        self.assertIn("{target, text}", problems)
        self.assertTrue(validate_scenario({"steps": []}))


class ChromeArgumentTests(unittest.TestCase):
    def test_a_host_browser_is_locked_to_the_demo_address(self):
        args = chrome_args("chrome", Path("/tmp/p"), (1280, 800), ["127.0.0.1:5000"])
        self.assertIn("--proxy-server=http://127.0.0.1:9", args)
        self.assertIn("--proxy-bypass-list=<-loopback>;127.0.0.1:5000", args)
        self.assertIn("--force-webrtc-ip-handling-policy=disable_non_proxied_udp", args)
        self.assertNotIn("--no-sandbox", args)


PAGE = """<!doctype html><title>demo</title><body><h1>Wordtally demo</h1>
<button onclick="document.getElementById('out').textContent='Counted 3 words'">Count</button>
<p id="out"></p><p id="net">checking</p>
<script>fetch("http://127.0.0.1:{secret}/").then(() => net.textContent = "LEAKED")
  .catch(() => net.textContent = "Host service sealed off");</script></body>"""

SERVER = """import http.server, sys
page = open(sys.argv[2]).read()
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.send_header("Content-Type", "text/html"); self.end_headers()
        self.wfile.write(page.encode())
    def log_message(self, *a): pass
http.server.ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
"""


@unittest.skipUnless(chrome(), "Chrome is not installed")
@unittest.skipUnless(platform.system() in {"Darwin", "Linux"}, "needs buildctl's isolation backend")
class RecordingTests(unittest.TestCase):
    def setUp(self):
        from buildctl.supervisor.isolation import IsolationError, production_backend
        try:
            production_backend().preflight()
        except IsolationError as exc:
            self.skipTest(f"isolation backend unavailable: {exc}")
        self.root = Path(tempfile.mkdtemp(prefix="recorder-test-"))
        self.addCleanup(shutil.rmtree, self.root, True)

        class Secret(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200); self.send_header("Access-Control-Allow-Origin", "*"); self.end_headers()
                self.wfile.write(b"secret")

            def log_message(self, *args):
                pass

        self.secret = ThreadingHTTPServer(("127.0.0.1", 0), Secret)
        threading.Thread(target=self.secret.serve_forever, daemon=True).start()
        self.addCleanup(self.secret.server_close)
        self.addCleanup(self.secret.shutdown)

    def test_a_recording_drives_the_demo_and_the_page_reaches_nothing(self):
        workspace = self.root / "workspace"; workspace.mkdir()
        subprocess.run(["git", "init", "-q", str(workspace)], check=True)
        (workspace / "server.py").write_text(SERVER)
        (workspace / "page.html").write_text(PAGE.replace("{secret}", str(self.secret.server_address[1])))
        control, repo = self.root / "control", self.root / "repo"
        control.mkdir(mode=0o700); repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        if platform.system() == "Linux":
            from buildctl.supervisor.isolation import LinuxBubblewrapBackend
            LinuxBubblewrapBackend().prepare(control)
        demo = Demo(["/usr/bin/python3", "server.py", "{port}", "page.html"], timeout_seconds=30, viewport=(800, 600))
        scenario = {"title": "Count words", "steps": [
            {"name": "The demo opens", "open": "/", "expect": "Wordtally demo", "hold": 0.5},
            {"name": "Its page cannot reach the host", "wait_for": "Host service sealed off", "hold": 0.5},
            {"name": "Count", "click": "Count", "expect": "Counted 3 words", "hold": 0.5}]}
        out = self.root / "out"
        if platform.system() == "Linux":
            with self.assertRaises(RecordError) as caught:
                record(demo, scenario, workspace, out, control_root=control, repo=repo)
            self.assertIn("not supported on Linux", str(caught.exception))
            return
        summary = record(demo, scenario, workspace, out, control_root=control, repo=repo)
        self.assertTrue(summary["passed"], json.dumps(summary["steps"], indent=1))
        self.assertTrue((out / summary["steps"][2]["frame"]).is_file())
        self.assertFalse((out / "home").exists())
        if shutil.which("ffmpeg"):
            self.assertTrue((out / "demo.gif").is_file())
        self.assertEqual(json.loads((out / "recording.json").read_text())["isolation"]["loopback"], True)


if __name__ == "__main__":
    unittest.main()
