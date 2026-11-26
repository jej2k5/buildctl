"""buildctl-drafts: write release drafts for a finished milestone, check that every claim cites a source that
exists, never publish."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from buildctl.buildctl import control_root_for, resolve_repository

from .checks import TYPES
from .context import ContextError
from .make import make
from .store import DEFAULT_COST_USD, DraftStore, StoreError


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="buildctl-drafts", description=__doc__)
    result.add_argument("--repo", help="governed repository (default: Git toplevel of the current directory)")
    commands = result.add_subparsers(dest="command", required=True)
    make_ = commands.add_parser("make", help="write drafts for a milestone or a commit range")
    span = make_.add_mutually_exclusive_group(required=True)
    span.add_argument("--milestone", help="a milestone named in build.yaml")
    span.add_argument("--since", help="the commit or tag the release starts after")
    make_.add_argument("--until", default="HEAD", help="the release's last commit (default HEAD)")
    make_.add_argument("--only", help=f"comma-separated draft types (default all: {', '.join(TYPES)})")
    make_.add_argument("--cost", type=float, default=DEFAULT_COST_USD,
                       help=f"estimated cost of one writer call, booked as release_draft (default {DEFAULT_COST_USD})")
    record = commands.add_parser("record", help="record the committed demo scenario at a milestone (free, no model call)")
    span = record.add_mutually_exclusive_group(required=True)
    span.add_argument("--milestone", help="a milestone named in build.yaml")
    span.add_argument("--until", help="a commit to record from")
    commands.add_parser("list", help="every draft, newest last")
    for name, text in (("show", "print a draft with its claims ledger"), ("approve", "mark a draft approved"),
                       ("discard", "mark a draft discarded")):
        commands.add_parser(name, help=text).add_argument("draft", help="a draft id from list, or a type for its newest")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        repo = resolve_repository(args.repo)
        control_root = control_root_for(repo)
        store = DraftStore(control_root)
        if args.command == "make":
            kinds = [item.strip() for item in args.only.split(",")] if args.only else list(TYPES)
            unknown = [kind for kind in kinds if kind not in TYPES]
            if unknown:
                raise StoreError(f"unknown draft type: {', '.join(unknown)}; choose from {', '.join(TYPES)}")
            result = make(repo, control_root, kinds=kinds, since=args.since, until=args.until,
                          milestone=args.milestone, cost=args.cost)
            print(f"Drafts are in {control_root / 'drafts' / result['run']}. Nothing was committed or published.")
            return 0
        if args.command == "record":
            from .make import record_demo
            result = record_demo(repo, control_root, milestone=args.milestone, until=args.until or "HEAD")
            for item in result["summary"]["steps"]:
                print(f"  {'ok  ' if item['ok'] else 'FAIL'} {item['step']:>2}. {item['name']}"
                      + ("" if item["ok"] else f"  ({item['problem']})"))
            print(f"Recording in {result['directory']}. Nothing was committed or published.")
            return 0 if result["summary"]["passed"] else 1
        if args.command == "list":
            for item in store.all():
                checks = item["checks"]
                print(f"{item['id']:<36} {item['state']:<10} {'sourced' if checks['passed'] else 'needs you':<10} "
                      f"{checks['claims'] - len(checks['unbacked'])}/{checks['claims']} backed  {item['label']}")
            return 0
        item = store.get(args.draft)
        if args.command == "show":
            print(Path(item["markdown"]).read_text(), end="")
            print(f"\n[{item['id']} · {item['state']}]")
            return 0
        store.record(item["id"], "approved" if args.command == "approve" else "discarded")
        print(f"{item['id']} {'approved' if args.command == 'approve' else 'discarded'}. Recorded, not published.")
        return 0
    except (ContextError, StoreError, ValueError) as exc:  # RecordError arrives as StoreError
        print(f"buildctl-drafts: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
