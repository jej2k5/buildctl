"""One drafts run: gather the milestone, write each draft, check it, regenerate once, store it."""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Any, Callable

import subprocess

from buildctl.supervisor.agents import FORMAT_RETRYABLE_SUFFIXES, AgentError
from buildctl.supervisor.isolation import IsolationError, production_backend, require_fresh_conformance

from . import checks
from .checks import TYPES, parse_voice
from .context import Context, clone_at, gather, show, trusted
from .store import DEFAULT_COST_USD, DraftStore, StoreError, render
from .writer import build_prompt, call_writer, writer_config

WRITER_TIMEOUT = 900.0


def make(repo: Path, control_root: Path, *, kinds: list[str], since: str | None = None, until: str = "HEAD",
         milestone: str | None = None, cost: float = DEFAULT_COST_USD,
         call: Callable[..., dict[str, Any]] = call_writer, echo: Callable[[str], None] = print) -> dict[str, Any]:
    context = gather(repo, control_root, since=since, until=until, milestone=milestone)
    config, anchor = trusted(repo, control_root)
    store = DraftStore(control_root)
    skipped = {}
    if "social" in kinds and not context.operator_voice:
        skipped["social"] = "no operator voice file at ~/.config/buildctl/voice.md"
    selected = [kind for kind in kinds if kind not in skipped]
    limit = float(config["limits"]["max_daily_cost_usd"])
    if call is call_writer:
        # The same rule the kernel applies before any agent call: the sandbox was proven on this machine today.
        try:
            require_fresh_conformance(control_root, production_backend(), anchor["snapshot_digest"])
        except (IsolationError, KeyError) as exc:
            raise StoreError(f"{exc}. Run buildctl isolation verify first.") from exc
    store.ensure_budget(control_root, limit, cost * len(selected))
    run = store.new_run()
    banned = list(config.get("documentation", {}).get("forbidden_phrases", []))
    voices = {"product": parse_voice("product", context.product_voice, banned),
              "operator": parse_voice("operator", context.operator_voice)}
    cache: dict[str, str | None] = {}

    def read(path: str) -> str | None:
        if path not in cache:
            cache[path] = show(context.repo, context.until, path)
        return cache[path]

    meta = {"label": context.label, "since": context.since, "until": context.until, "commits": len(context.commits)}
    results = {"run": run, "drafts": [], "skipped": skipped}
    workspace = Path(tempfile.mkdtemp(prefix="buildctl-drafts-")) / "workspace"
    try:
        clone_at(context.repo, context.until, workspace)
        configured = writer_config(config, anchor)
        for kind in selected:
            voice = voices[TYPES[kind]["voice"]]
            value, report, attempts = None, {"contract": [], "unbacked": [], "lint": [], "claims": 0, "passed": False}, 0
            feedback = None
            for attempt in (1, 2):
                if attempt == 2:
                    try:
                        store.ensure_budget(control_root, limit, cost)
                    except StoreError as exc:
                        report = dict(report, contract=report["contract"] + [f"not rewritten: {exc}"])
                        break
                attempts = attempt
                store.reserve(f"{run}/{kind}", cost)
                output = control_root / "drafts" / run / f"{kind}.result-{attempt}.json"
                try:
                    value = call(context, configured, workspace, build_prompt(kind, context, voice, feedback), output,
                                 WRITER_TIMEOUT)
                except AgentError as exc:
                    code = str(exc).split(":", 1)[0]
                    if not code.endswith(FORMAT_RETRYABLE_SUFFIXES):
                        # A launch, login, sandbox or secret problem fails every draft the same way: stop
                        # instead of paying for each of them twice.
                        raise StoreError(_writer_failure(str(exc))) from exc
                    report = {"passed": False, "contract": [f"the writer's output was not the JSON contract: {exc}"],
                              "unbacked": [], "lint": [], "claims": 0}
                    value = None
                except (subprocess.TimeoutExpired, IsolationError, OSError) as exc:
                    raise StoreError(f"the writer could not run: {exc}") from exc
                else:
                    report = checks.review(kind, value, context, voice, read)
                if report["passed"]:
                    break
                feedback = {key: report[key] for key in ("contract", "unbacked", "lint") if report[key]}
            record = {"kind": kind, "title": TYPES[kind]["title"], "voice": voice.name, "draft": value or {},
                      "checks": report, "attempts": attempts, **meta}
            identity = store.save(run, kind, record, render(kind, TYPES[kind]["title"], value or {}, report, meta))
            results["drafts"].append({"id": identity, "kind": kind, "passed": report["passed"],
                                      "claims": report["claims"], "unbacked": len(report["unbacked"]),
                                      "lint": len(report["lint"]), "attempts": attempts})
            echo(summary_line(results["drafts"][-1]))
    finally:
        shutil.rmtree(workspace.parent, ignore_errors=True)
    for kind, why in skipped.items():
        echo(f"  {kind:<14} skipped: {why}")
    return results


def _writer_failure(message: str) -> str:
    if "SECRET_OUTPUT" in message:
        return ("the writer's output contained something shaped like a credential, so nothing was kept "
                f"({message.split(':', 1)[-1]}). A quoted config line is the usual cause.")
    if "EXECUTABLE_INTEGRITY_FAILURE" in message:
        return "the reviewer CLI changed since trust init (usually an update). Run buildctl trust init again."
    if "EXECUTABLE_UNAVAILABLE" in message:
        return "the reviewer CLI is not where trust init pinned it."
    return f"the writer failed: {message}"


def summary_line(item: dict[str, Any]) -> str:
    backed = item["claims"] - item["unbacked"]
    state = "sourced" if item["passed"] else "needs you"
    detail = f"{backed} of {item['claims']} claims backed"
    if item["lint"]:
        detail += f", {item['lint']} voice problem{'s' if item['lint'] > 1 else ''}"
    tries = " after a rewrite" if item["attempts"] > 1 else ""
    return f"  {item['kind']:<14} {state:<10} {detail}{tries}   {item['id']}"
