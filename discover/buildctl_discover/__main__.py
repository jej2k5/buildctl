"""buildctl-discover: draft a buildctl roadmap from discovery, for the operator to commit and trust.

  start      the operator opens a session (base: the repo's build.yaml, or a built-in default)
  serve      the MCP server an agent runs the interview through
  status     what the record holds and the proposal digest
  render     re-render the proposal from the record
  check      the deterministic gate; exits 1 while anything fails
  finalize   the operator copies a passing proposal into the working tree
"""
from __future__ import annotations

import argparse
import getpass
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from buildctl.buildctl import resolve_repository
from buildctl.supervisor.audit import AuditIntegrityError

from .check import check
from .finalize import finalize
from .record import BUILTIN_BASE, Identity, RecordError, Session
from .render import read_proposal, render, write_proposal
from .server import Server


def default_name(repo: Path) -> str:
    result = subprocess.run(["git", "-C", str(repo), "config", "user.name"], text=True, capture_output=True)
    return result.stdout.strip() or getpass.getuser()


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="buildctl-discover", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    result.add_argument("--repo", help="the repository buildctl will build (default: Git toplevel of the current "
                                       "directory)")
    result.add_argument("--session", default="default", help="session name (default: default)")
    result.add_argument("--as", dest="name", help="who is speaking (default: git user.name)")
    commands = result.add_subparsers(dest="command", required=True)
    start = commands.add_parser("start", help="open a session; you are the operator")
    start.add_argument("--base", help="JSON build.yaml to take limits, roles and defaults from")
    start.add_argument("--builtin-base", action="store_true", help="ignore the repo's build.yaml")
    serve = commands.add_parser("serve", help="MCP stdio server for an agent")
    serve.add_argument("--role", choices=("owner", "operator"), default="owner",
                       help="pinned for the whole server; operator unlocks accept_risk (default: owner)")
    commands.add_parser("status", help="the record and the proposal digest")
    commands.add_parser("render", help="re-render the proposal from the record")
    commands.add_parser("check", help="run the gate")
    final = commands.add_parser("finalize", help="copy a passing proposal into the working tree")
    final.add_argument("--confirm", required=True, help="the first 8 or more characters of the proposal digest")
    return result


def load_base(repo: Path, args: argparse.Namespace) -> tuple[dict[str, Any], str]:
    if args.base:
        return json.loads(Path(args.base).read_text()), args.base
    existing = repo / "build.yaml"
    if existing.exists() and not args.builtin_base:
        return json.loads(existing.read_text()), "build.yaml"
    return BUILTIN_BASE, "the built-in default"


def status(session: Session, repo: Path) -> int:
    state = session.state()
    result = check(state, repo, read_proposal(session.proposal_dir))
    open_questions = [item for item in state.questions.values() if item["status"] == "open"]
    print(f"session   {session.directory}")
    print(f"proposal  {session.proposal_dir}")
    print(f"digest    {result['digest']}")
    print(f"thesis    {'set' if state.thesis else 'missing'}")
    print(f"decisions {len(state.current_decisions())} current, {len(open_questions)} open question(s)")
    print(f"phases    {len(state.phases)}")
    print(f"reviews   {len(state.reviews)}")
    verdict = "passes" if result["passed"] else f"{len(result['failures'])} failure(s)"
    print(f"check     {verdict}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        repo = resolve_repository(args.repo)
        session = Session.for_repo(repo, args.session)
        name = args.name or default_name(repo)
        if args.command == "start":
            base, source = load_base(repo, args)
            session.start(repo, base, Identity(name, "operator"))
            write_proposal(session.proposal_dir, render(session.state()))
            print(f"Session started at {session.directory}, base from {source}.")
            print("Point an agent at: buildctl-discover serve" + (f" --session {args.session}"
                                                                  if args.session != "default" else ""))
            return 0
        if args.command == "serve":
            session.state()  # refuse to serve a session that does not exist
            Server(session, repo, Identity(name, args.role)).serve()
            return 0
        if args.command == "status":
            return status(session, repo)
        if args.command == "render":
            with session.lock():
                files = render(session.state())
                write_proposal(session.proposal_dir, files)
            print(f"Rendered {len(files)} files to {session.proposal_dir}")
            return 0
        if args.command == "check":
            result = check(session.state(), repo, read_proposal(session.proposal_dir))
            for failure in result["failures"]:
                targets = f" [{', '.join(failure['targets'])}]" if failure["targets"] else ""
                print(f"{failure['rule']}{targets}: {failure['message']}")
            print(f"digest {result['digest']}: {'passes' if result['passed'] else 'fails'}")
            return 0 if result["passed"] else 1
        if args.command == "finalize":
            result = finalize(session, repo, args.confirm, Identity(name, "operator"))
            print(f"Wrote {len(result['files'])} files from proposal {result['digest'][:12]}.")
            for orphan in result["orphans"]:
                print(f"Not in the proposal, still on disk: {orphan}")
            for step in result["next"]:
                print(f"next: {step}")
            return 0
    except (RecordError, AuditIntegrityError, ValueError, OSError) as exc:
        print(f"buildctl-discover: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    sys.exit(main())
