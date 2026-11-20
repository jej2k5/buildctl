"""buildctl-ui: the local console (default) or the terminal UI for a buildctl build."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from buildctl.buildctl import resolve_repository

from .actions import Build


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="buildctl-ui", description=__doc__)
    result.add_argument("surface", nargs="?", choices=("web", "tui"), default="web",
                        help="web: the console in your browser (default); tui: the terminal UI")
    result.add_argument("--repo", help="governed repository (default: Git toplevel of the current directory)")
    result.add_argument("--port", type=int, default=0, help="web: port on 127.0.0.1 (default: any free port)")
    result.add_argument("--no-open", action="store_true", help="web: print the link instead of opening a browser")
    result.add_argument("--demo", nargs="?", const="blocked", metavar="SCENARIO",
                        help="open a synthetic build instead: blocked, checkpoint, ceiling, unknown, running, debate, fresh")
    result.add_argument("--session", metavar="NAME",
                        help="web: open a buildctl-discover draft read-only, before it is finalized")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.session:
            if args.surface == "tui":
                print("buildctl-ui: a discovery draft opens in the web console only", file=sys.stderr)
                return 2
            from .draft import Draft
            build = Draft.open(resolve_repository(args.repo), args.session)
        elif args.demo:
            from .demo import build_temporary
            repo, control = build_temporary(args.demo)
            build = Build.open(repo, control, demo=args.demo)
        else:
            build = Build.open(resolve_repository(args.repo))
    except (ValueError, OSError) as exc:
        print(f"buildctl-ui: {exc}", file=sys.stderr)
        return 2
    if args.surface == "tui":
        try:
            from .tui import run
        except ImportError:
            print("buildctl-ui: the terminal UI needs Textual: pip install 'buildctl-ui[tui]'", file=sys.stderr)
            return 2
        run(build)
        return 0
    from .server import serve
    serve(build, port=args.port, open_browser=not args.no_open)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
