"""Start the console's demo for a recording: `python docs/demo/serve.py SCENARIO PORT`.

The demo command in docs/demo/demo.json runs this from the repository root. It builds a demo build
(the Relay fixture, fictional data) in TMPDIR and serves the console on 127.0.0.1:PORT, printing the
link with its token, which the recorder reads.
"""
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(root), str(root / "ui"), str(root / "drafts")]

from buildctl_ui.__main__ import main  # noqa: E402

scenario, port = sys.argv[1], sys.argv[2]
sys.exit(main(["--demo", scenario, "--port", port, "--no-open"]))
