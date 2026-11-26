"""Record a demo: start the product on fictional data, sandboxed with loopback only, and drive a headless
browser through a committed scenario, capturing frames at each step and a GIF of the whole.

The demo command runs under buildctl's isolation backend with `loopback=True`: it may serve on a
loopback port and reaches nothing else, with a sanitized environment that carries no credential.

- macOS has no private loopback, so Chrome runs on the host. It is locked to the demo's address by a
  dead proxy with only that address bypassed, so the demo's own page cannot reach the host's other
  loopback services or the internet either.
- Linux gives the sandbox its own network namespace, which a host browser cannot reach. The recorder
  then runs inside the same sandbox as the demo (`--inner`), Chrome included.

Chrome is driven over its DevTools pipe with the standard library; nothing is installed.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CHROME_CANDIDATES = ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                     "/Applications/Chromium.app/Contents/MacOS/Chromium",
                     "google-chrome", "google-chrome-stable", "chromium", "chromium-browser")
ACTIONS = ("open", "click", "type", "wait_for", "pause")
FRAME_INTERVAL = 0.25


class RecordError(RuntimeError):
    pass


# --- the scenario -----------------------------------------------------------------------------

def load_scenario(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise RecordError(f"the scenario {path} is not readable JSON: {exc}") from exc
    problems = validate_scenario(value)
    if problems:
        raise RecordError(f"the scenario {path} is invalid: " + "; ".join(problems))
    return value


def validate_scenario(value: Any) -> list[str]:
    """{title, steps: [{name, <one action>, expect?, hold?}]}. Each step proves one thing on screen."""
    if not isinstance(value, dict) or not isinstance(value.get("title"), str) or not isinstance(value.get("steps"), list):
        return ["a scenario is {title, steps: [...]}"]
    problems = []
    if not value["steps"]:
        problems.append("a scenario needs at least one step")
    for number, step in enumerate(value["steps"], 1):
        if not isinstance(step, dict) or not isinstance(step.get("name"), str):
            problems.append(f"step {number} needs a name")
            continue
        actions = [key for key in ACTIONS if key in step]
        if len(actions) != 1:
            problems.append(f"step {number} needs exactly one of {', '.join(ACTIONS)}")
        if "type" in step and not (isinstance(step["type"], dict) and {"target", "text"} <= set(step["type"])):
            problems.append(f"step {number}: type is {{target, text}}")
        if "expect" in step and not isinstance(step["expect"], str):
            problems.append(f"step {number}: expect is the text the screen must show")
        hold = step.get("hold", 1.0)
        if not isinstance(hold, (int, float)) or isinstance(hold, bool) or not 0 <= hold <= 15:
            problems.append(f"step {number}: hold is 0 to 15 seconds")
        unknown = set(step) - {"name", "expect", "hold", *ACTIONS}
        if unknown:
            problems.append(f"step {number}: unknown keys {', '.join(sorted(unknown))}")
    return problems


# --- Chrome over its DevTools pipe ------------------------------------------------------------

def find_chrome() -> str:
    for candidate in CHROME_CANDIDATES:
        found = candidate if Path(candidate).is_file() else shutil.which(candidate)
        if found:
            return found
    raise RecordError("no Chrome or Chromium found; install Google Chrome to record demos")


def chrome_args(executable: str, profile: Path, viewport: tuple[int, int], allow: list[str] | None,
                sandboxed: bool = False) -> list[str]:
    args = [executable, "--headless=new", "--remote-debugging-pipe", "--no-first-run", "--no-default-browser-check",
            f"--user-data-dir={profile}", f"--window-size={viewport[0]},{viewport[1]}", "--hide-scrollbars",
            "--disable-extensions", "--disable-background-networking", "--disable-sync",
            "--disable-component-update", "--disable-default-apps", "--mute-audio",
            "--force-webrtc-ip-handling-policy=disable_non_proxied_udp"]
    if allow is not None:
        # Everything goes to a dead proxy except the demo's own address; `<-loopback>` removes
        # Chrome's implicit bypass for localhost, so other loopback ports are refused too.
        args += ["--proxy-server=http://127.0.0.1:9", "--proxy-bypass-list=" + ";".join(["<-loopback>", *allow])]
    if sandboxed:
        # Inside buildctl's sandbox Chrome cannot build its own; the outer one bounds it.
        args += ["--no-sandbox", "--no-zygote", "--disable-dev-shm-usage", "--disable-gpu"]
    return [*args, "about:blank"]


class Chrome:
    """A headless Chrome on --remote-debugging-pipe: requests on fd 3, replies on fd 4, NUL-terminated JSON."""

    def __init__(self, executable: str, profile: Path, viewport: tuple[int, int], allow: list[str] | None,
                 sandboxed: bool = False):
        to_chrome_r, self._to_chrome = os.pipe()
        self._from_chrome, from_chrome_w = os.pipe()
        args = chrome_args(executable, profile, viewport, allow, sandboxed)

        def wire() -> None:
            # Chrome reads fd 3 and writes fd 4. Copy first so a pipe end already at 3 or 4 is not
            # clobbered; dup2's copies are inheritable, every other descriptor closes on exec.
            read_end, write_end = os.dup(to_chrome_r), os.dup(from_chrome_w)
            os.dup2(read_end, 3)
            os.dup2(write_end, 4)

        self.process = subprocess.Popen(args, preexec_fn=wire, close_fds=False,
                                        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        os.close(to_chrome_r); os.close(from_chrome_w)
        self._next, self._buffer, self.events = 0, b"", []
        self.session: str | None = None

    def call(self, method: str, params: dict[str, Any] | None = None, session: bool = True,
             timeout: float = 30) -> dict[str, Any]:
        self._next += 1
        message: dict[str, Any] = {"id": self._next, "method": method, "params": params or {}}
        if session and self.session:
            message["sessionId"] = self.session
        os.write(self._to_chrome, json.dumps(message).encode() + b"\0")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for reply in self._read(deadline - time.monotonic()):
                if reply.get("id") == self._next:
                    if "error" in reply:
                        raise RecordError(f"Chrome refused {method}: {reply['error'].get('message')}")
                    return reply.get("result", {})
                self.events.append(reply)
        raise RecordError(f"Chrome did not answer {method} in {timeout:.0f}s")

    def _read(self, timeout: float) -> list[dict[str, Any]]:
        import select
        ready, _, _ = select.select([self._from_chrome], [], [], max(0.0, timeout))
        if not ready:
            return []
        chunk = os.read(self._from_chrome, 1 << 20)
        if not chunk:
            stderr = self.process.stderr.read().decode(errors="replace")[-400:] if self.process.stderr else ""
            raise RecordError(f"Chrome exited: {stderr.strip() or 'no output'}")
        self._buffer += chunk
        *messages, self._buffer = self._buffer.split(b"\0")
        return [json.loads(item) for item in messages if item]

    def open_page(self, viewport: tuple[int, int]) -> None:
        target = self.call("Target.createTarget", {"url": "about:blank"}, session=False)["targetId"]
        self.session = self.call("Target.attachToTarget", {"targetId": target, "flatten": True}, session=False)["sessionId"]
        self.call("Page.enable"); self.call("Runtime.enable")
        self.call("Emulation.setDeviceMetricsOverride", {"width": viewport[0], "height": viewport[1],
                                                         "deviceScaleFactor": 1, "mobile": False})

    def evaluate(self, expression: str) -> Any:
        result = self.call("Runtime.evaluate", {"expression": expression, "returnByValue": True, "awaitPromise": True})
        if result.get("exceptionDetails"):
            raise RecordError(f"page script failed: {result['exceptionDetails'].get('text')}")
        return result.get("result", {}).get("value")

    def screenshot(self, path: Path) -> None:
        data = self.call("Page.captureScreenshot", {"format": "png"})["data"]
        path.write_bytes(base64.b64decode(data))

    def close(self) -> None:
        try:
            self.call("Browser.close", session=False, timeout=5)
        except (RecordError, OSError):
            pass
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
        for fd in (self._to_chrome, self._from_chrome):
            try:
                os.close(fd)
            except OSError:
                pass
        if self.process.stderr:
            self.process.stderr.close()


# Find an element by its visible text (exact first, then contains) among things a person clicks,
# or by CSS when the target starts with "css:".
FIND = """(target) => {
  if (target.startsWith("css:")) return document.querySelector(target.slice(4));
  const pool = [...document.querySelectorAll('a,button,summary,[role=tab],[role=button],label,input,select,textarea')];
  const text = (el) => (el.innerText || el.value || el.getAttribute('aria-label') || '').trim();
  return pool.find((el) => text(el) === target) || pool.find((el) => text(el).includes(target)) || null;
}"""


def _step_script(step: dict[str, Any], base: str) -> str | None:
    if "open" in step:
        return f"location.href = new URL({json.dumps(step['open'])}, {json.dumps(base)}).href; true"
    if "click" in step:
        return (f"(() => {{ const el = ({FIND})({json.dumps(step['click'])}); if (!el) return 'missing';"
                " el.scrollIntoView({block: 'center'}); el.click(); return 'ok'; })()")
    if "type" in step:
        target, text = step["type"]["target"], step["type"]["text"]
        return (f"(() => {{ const el = ({FIND})({json.dumps(target)}); if (!el) return 'missing'; el.focus();"
                f" el.value = {json.dumps(text)}; el.dispatchEvent(new Event('input', {{bubbles: true}})); return 'ok'; }})()")
    return None


def _visible(chrome: Chrome, text: str) -> bool:
    return bool(chrome.evaluate(f"document.body && document.body.innerText.includes({json.dumps(text)})"))


def drive(chrome: Chrome, url: str, scenario: dict[str, Any], out: Path, viewport: tuple[int, int]) -> list[dict[str, Any]]:
    """Run each step, wait for what it should prove, and capture frames while it holds."""
    frames = out / "frames"; frames.mkdir(parents=True, exist_ok=True)
    chrome.open_page(viewport)
    chrome.call("Page.navigate", {"url": url})
    _settle(chrome, 10)
    results, sequence = [], 0
    for number, step in enumerate(scenario["steps"], 1):
        entry: dict[str, Any] = {"step": number, "name": step["name"], "expect": step.get("expect"), "ok": True}
        script = _step_script(step, url)
        if script:
            outcome = chrome.evaluate(script)
            if outcome == "missing":
                entry.update(ok=False, problem=f"nothing on screen matches {step.get('click') or step['type']['target']!r}")
        if "wait_for" in step:
            step = dict(step, expect=step.get("expect") or step["wait_for"])
        if "pause" in step:
            time.sleep(float(step["pause"]))
        if "open" in step:
            _settle(chrome, 10)
        if step.get("expect") and entry["ok"]:
            deadline = time.monotonic() + 10
            while not _visible(chrome, step["expect"]) and time.monotonic() < deadline:
                time.sleep(0.2)
            if not _visible(chrome, step["expect"]):
                entry.update(ok=False, problem=f"the screen never showed {step['expect']!r}")
        hold = float(step.get("hold", 1.0))
        shots = max(1, int(hold / FRAME_INTERVAL))
        first = None
        for _ in range(shots):
            sequence += 1
            path = frames / f"{sequence:04d}.png"
            chrome.screenshot(path)
            first = first or path
            time.sleep(FRAME_INTERVAL)
        entry["frame"] = str(first.relative_to(out)) if first else None
        results.append(entry)
    return results


def _settle(chrome: Chrome, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if chrome.evaluate("document.readyState") == "complete":
                time.sleep(0.6)  # let the page's first render and fetches land
                return
        except RecordError:
            pass
        time.sleep(0.2)


def make_gif(out: Path, viewport: tuple[int, int], width: int = 960) -> Path | None:
    """A GIF from the frames, with one palette so the text stays crisp. None without ffmpeg."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return None
    target = out / "demo.gif"
    rate = 1 / FRAME_INTERVAL
    graph = (f"fps={rate:g},scale={min(width, viewport[0])}:-1:flags=lanczos,split[a][b];"
             "[a]palettegen=stats_mode=diff[p];[b][p]paletteuse=dither=none")
    result = subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-framerate", f"{rate:g}",
                             "-i", str(out / "frames" / "%04d.png"), "-filter_complex", graph, str(target)],
                            capture_output=True, text=True)
    if result.returncode:
        raise RecordError(f"ffmpeg could not make the GIF: {result.stderr.strip()[-300:]}")
    return target


# --- the demo process -------------------------------------------------------------------------

@dataclass
class Demo:
    command: list[str]
    url: str = "http://127.0.0.1:{port}/"
    ready_pattern: str | None = None
    timeout_seconds: int = 120
    viewport: tuple[int, int] = (1280, 800)
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_config(cls, section: dict[str, Any]) -> "Demo":
        return cls(list(section["command"]), section.get("url", "http://127.0.0.1:{port}/"),
                   section.get("ready_pattern"), int(section.get("timeout_seconds", 120)),
                   tuple(section.get("viewport", [1280, 800])))


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def start_demo(demo: Demo, port: int, cwd: Path, wrap: Any, env: dict[str, str], log: Path) -> tuple[subprocess.Popen, str]:
    """Start the demo command (wrapped in the sandbox by `wrap`) and wait until it serves. Returns the URL."""
    argv = [part.replace("{port}", str(port)).replace("{python}", sys.executable) for part in demo.command]
    stream = log.open("w")
    process = subprocess.Popen(wrap(argv), cwd=cwd, env=env, stdout=stream, stderr=subprocess.STDOUT,
                               start_new_session=True)
    stream.close()  # the child holds its own copy
    url = demo.url.replace("{port}", str(port))
    deadline = time.monotonic() + demo.timeout_seconds
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RecordError(f"the demo command exited {process.returncode} before serving: "
                              f"{log.read_text(errors='replace')[-400:].strip()}")
        if demo.ready_pattern:
            found = re.search(demo.ready_pattern, log.read_text(errors="replace"))
            if found:
                return process, found.group(1)
        else:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                    return process, url
            except OSError:
                pass
        time.sleep(0.3)
    stop(process)
    raise RecordError(f"the demo did not start serving within {demo.timeout_seconds}s")


def stop(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, 15)
        process.wait(timeout=5)
    except (ProcessLookupError, subprocess.TimeoutExpired, PermissionError):
        try:
            os.killpg(process.pid, 9)
        except (ProcessLookupError, PermissionError):
            pass


# --- one recording ----------------------------------------------------------------------------

def record(demo: Demo, scenario: dict[str, Any], workspace: Path, out: Path, *, control_root: Path,
           repo: Path, backend: Any = None) -> dict[str, Any]:
    """Record `scenario` against the demo started in `workspace`. Writes frames, a GIF and recording.json to `out`."""
    from buildctl.supervisor.isolation import IsolationContext, production_backend, sanitized_environment

    backend = backend or production_backend()
    out.mkdir(parents=True, exist_ok=True)
    home = out / "home"; home.mkdir(exist_ok=True)
    context = IsolationContext("demo", workspace, control_root, repo, True, out, home, False, loopback=True)
    env = sanitized_environment("demo", home=home)
    linux = platform.system() == "Linux"
    started = time.time()
    if linux:
        # The browser has to live in the demo's own network namespace: run this module inside it.
        inner = [sys.executable, "-m", "buildctl_drafts.recorder", "--inner", "--out", str(out),
                 "--demo", json.dumps({"command": demo.command, "url": demo.url, "ready_pattern": demo.ready_pattern,
                                       "timeout_seconds": demo.timeout_seconds, "viewport": list(demo.viewport)}),
                 "--scenario", json.dumps(scenario)]
        env = dict(env, PYTHONPATH=os.pathsep.join(p for p in sys.path if p))
        result = subprocess.run(backend.command(inner, context), cwd=workspace, env=env, capture_output=True,
                                text=True, timeout=demo.timeout_seconds + 120)
        if result.returncode:
            raise RecordError(f"the sandboxed recorder failed: {(result.stderr or result.stdout).strip()[-600:]}")
        steps = json.loads((out / "steps.json").read_text())
    else:
        port = free_port()
        process, url = start_demo(demo, port, workspace, lambda argv: backend.command(argv, context), env,
                                  out / "demo.log")
        profile = Path(tempfile.mkdtemp(prefix="buildctl-recorder-"))
        chrome = Chrome(find_chrome(), profile, demo.viewport, [f"127.0.0.1:{port}", f"localhost:{port}"])
        try:
            steps = drive(chrome, url, scenario, out, demo.viewport)
        finally:
            chrome.close(); stop(process); shutil.rmtree(profile, ignore_errors=True)
    shutil.rmtree(home, ignore_errors=True)  # the demo's scratch (a whole fixture repository, for some demos)
    gif = make_gif(out, demo.viewport)
    summary = {"title": scenario["title"], "steps": steps, "passed": all(item["ok"] for item in steps),
               "gif": gif.name if gif else None, "seconds": round(time.time() - started, 1),
               "isolation": {"backend": backend.name, "loopback": True, "browser": "inside the sandbox" if linux
                             else "on the host, locked to the demo's address"}}
    (out / "recording.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary


def inner_main(out: Path, demo: Demo, scenario: dict[str, Any]) -> int:
    """Inside the sandbox (Linux): start the demo and Chrome here, where loopback is private."""
    port = free_port()
    process, url = start_demo(demo, port, Path.cwd(), lambda argv: argv, dict(os.environ), out / "demo.log")
    profile = Path(tempfile.mkdtemp(prefix="chrome-", dir=os.environ.get("TMPDIR")))
    chrome = Chrome(find_chrome(), profile, demo.viewport, None, sandboxed=True)
    try:
        steps = drive(chrome, url, scenario, out, demo.viewport)
    finally:
        chrome.close(); stop(process)
    (out / "steps.json").write_text(json.dumps(steps))
    return 0


def main(argv: list[str] | None = None) -> int:
    """Record from a working tree: `python -m buildctl_drafts.recorder --demo demo.json --scenario s.json --out DIR`.

    The demo still runs sandboxed with loopback only. This path is for a project's own docs; it is not
    a governed recording of a milestone (that is `buildctl-drafts record`)."""
    parser = argparse.ArgumentParser(prog="buildctl_drafts.recorder", description=main.__doc__)
    parser.add_argument("--demo", required=True, help="the demo section as JSON, or a path to a JSON file")
    parser.add_argument("--scenario", required=True, help="the scenario as JSON, or a path to it")
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--inner", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    load = lambda text: json.loads(text) if text.lstrip().startswith("{") else json.loads(Path(text).read_text())
    demo = Demo.from_config(load(args.demo))
    scenario = load(args.scenario)
    problems = validate_scenario(scenario)
    if problems:
        print("recorder: " + "; ".join(problems), file=sys.stderr)
        return 2
    try:
        if args.inner:
            return inner_main(args.out, demo, scenario)
        scratch = Path(tempfile.mkdtemp(prefix="buildctl-record-"))
        control, repo = scratch / "control", scratch / "repo"
        control.mkdir(mode=0o700); repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        from buildctl.supervisor.isolation import production_backend
        backend = production_backend()
        if hasattr(backend, "prepare"):
            backend.prepare(control)  # a scratch control root: write the Linux offline filter there
        summary = record(demo, scenario, args.workspace.resolve(), args.out.resolve(), control_root=control, repo=repo,
                         backend=backend)
    except RecordError as exc:
        print(f"recorder: {exc}", file=sys.stderr)
        return 1
    for item in summary["steps"]:
        print(f"  {'ok  ' if item['ok'] else 'FAIL'} {item['step']:>2}. {item['name']}" + (f"  ({item['problem']})" if not item["ok"] else ""))
    print(f"{'Recorded' if summary['passed'] else 'Recorded with failures'}: {args.out / (summary['gif'] or 'frames')}")
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
