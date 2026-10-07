from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any


SEVERITY = {"LOW": 1, "MEDIUM": 2, "HIGH": 3, "CRITICAL": 4}
TREATMENT = {"debt": 1, "fix": 2, "debate": 3, "human": 4, "accept_risk": 4}
REDUCER_VERSION = "2"
# Wire identifier shared with the governed repository: its pinned reviewer prompt and
# REVIEW_RESULT template must carry this exact string. It keeps the name it was born with
# in the original project until prompts and templates move into the kernel.
REVIEW_PROTOCOL_VERSION = "buildctl-review-v2"

# Single source of truth for the coverage vocabulary. The reviewer prompt and the pinned
# output contract are generated from these names, so the instructions handed to the model
# can never drift from what the validator accepts.
CLEAN_COVERAGE_DISPOSITION = "clear"
FINDING_COVERAGE_DISPOSITION = "finding_refs"
DELETED_COVERAGE_DISPOSITION = "deletion_acknowledged"
COVERAGE_DISPOSITIONS = (CLEAN_COVERAGE_DISPOSITION, FINDING_COVERAGE_DISPOSITION,
                         DELETED_COVERAGE_DISPOSITION)
COVERAGE_RECORD_KEYS = ("path", "digest", "change_type", "disposition", "finding_refs", "unreviewed")

# Statuses a reviewer may author. The supervisor-owned statuses below are deliberately
# absent: neither can be claimed by a model report.
REVIEWER_FINDING_STATUSES = ("open", "resolved", "still_open", "unverified")
SUPERSEDED_STATUS = "superseded"
DEFERRED_STATUS = "deferred"
# Terminal. A finding here has been decided and never returns. `superseded` remains
# recognised because registers written before deferral existed still carry it.
CLOSED_FINDING_STATUSES = frozenset({"resolved", SUPERSEDED_STATUS})
# Not blocking *this* review. `deferred` is the reversible member: the finding stands, but
# no report the validator would accept can cite it against the delta in front of it.
NON_BLOCKING_FINDING_STATUSES = CLOSED_FINDING_STATUSES | {DEFERRED_STATUS}


class ReviewValidationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Transition:
    disposition: str
    rule_id: str
    rationale: str


def stable_finding_id(finding: dict[str, Any]) -> str:
    value = [finding["path"], finding["claim_key"], finding["evidence_digest"], finding["violated_invariant"]]
    return hashlib.sha256(json.dumps(value, separators=(",", ":")).encode()).hexdigest()


FINDING_IDENTITY_FIELDS = ("path", "claim_key", "evidence_digest", "violated_invariant")


def normalize_finding_identities(report: dict[str, Any]) -> dict[str, str]:
    """Replace model-authored finding labels with supervisor-computed canonical ids.

    The reviewer authors the substantive identity fields; the supervisor owns the
    deterministic hash. A model may supply a short local label (``f1``) or a hash it
    computed itself — either is treated as a reference only, never as authority, and
    ``stable_finding_id`` remains the sole source of a finding's identity. Coverage
    references are remapped through the same table. Returns the label mapping.

    Fails closed on duplicate substantive identities, on a coverage reference that
    cannot be resolved, and on any label that would resolve ambiguously.
    """
    findings = report.get("findings")
    coverage = report.get("coverage")
    if not isinstance(findings, list) or not isinstance(coverage, list):
        return {}                                   # schema validation reports the real problem
    canonical_by_label: dict[str, str] = {}
    canonical_ids: set[str] = set()
    for finding in findings:
        if not isinstance(finding, dict):
            raise ReviewValidationError("finding must be an object")
        if any(not isinstance(finding.get(field), str) or not finding[field]
               for field in FINDING_IDENTITY_FIELDS):
            raise ReviewValidationError(
                "finding is missing substantive identity fields "
                f"({', '.join(FINDING_IDENTITY_FIELDS)})")
        canonical = stable_finding_id(finding)
        if canonical in canonical_ids:
            raise ReviewValidationError(
                f"duplicate finding identity for {finding['path']!r}/{finding['claim_key']!r}; "
                "each substantive claim may appear once")
        canonical_ids.add(canonical)
        label = finding.get("id")
        if isinstance(label, str) and label:
            if canonical_by_label.get(label, canonical) != canonical:
                raise ReviewValidationError(f"finding label {label!r} refers to two different findings")
            canonical_by_label[label] = canonical
        finding["id"] = canonical
    for label, canonical in canonical_by_label.items():
        if label != canonical and label in canonical_ids:
            raise ReviewValidationError(
                f"finding label {label!r} collides with another finding's canonical id")
    for item in coverage:
        if not isinstance(item, dict) or not isinstance(item.get("finding_refs"), list):
            continue
        resolved: list[str] = []
        for reference in item["finding_refs"]:
            if isinstance(reference, str) and reference in canonical_by_label:
                resolved.append(canonical_by_label[reference])
            elif isinstance(reference, str) and reference in canonical_ids:
                resolved.append(reference)
            else:
                raise ReviewValidationError(
                    f"coverage on {item.get('path')!r} references an unresolvable finding {reference!r}")
        if len(set(resolved)) != len(resolved):
            raise ReviewValidationError(f"coverage on {item.get('path')!r} references a finding twice")
        item["finding_refs"] = resolved
    return canonical_by_label


def validate_review(report: dict[str, Any], expected: dict[str, Any]) -> None:
    required = {"schema_version", "protocol_version", "governance_sha", "snapshot_digest", "base_sha", "phase",
                "delta_id", "evidence_digest", "reviewer", "attempt", "verdict", "findings", "coverage"}
    if set(report) != required or report["schema_version"] != 2 or report["protocol_version"] != REVIEW_PROTOCOL_VERSION:
        raise ReviewValidationError("invalid ReviewReport keys or version")
    for key in ("governance_sha", "snapshot_digest", "base_sha", "phase", "delta_id", "evidence_digest"):
        if report[key] != expected[key]:
            raise ReviewValidationError(f"ReviewReport identity mismatch: {key}")
    if report["verdict"] not in {"PASS", "REMEDIATE", "DEBATE", "HUMAN_ESCALATION"}:
        raise ReviewValidationError("invalid advisory verdict")
    if report["reviewer"] != expected.get("reviewer"):
        raise ReviewValidationError("reviewer identity or execution boundary mismatch")
    if not isinstance(report["findings"], list) or not isinstance(report["coverage"], list):
        raise ReviewValidationError("findings and coverage must be lists")
    # The supervisor owns deterministic identity: replace whatever label the reviewer used
    # with the canonical hash and remap coverage references before anything is validated
    # against it, so no downstream consumer ever sees a model-authored id.
    normalize_finding_identities(report)
    expected_coverage = expected["coverage"]
    actual = {(item.get("path"), item.get("digest"), item.get("change_type")): item for item in report["coverage"]}
    if set(actual) != set(expected_coverage):
        raise ReviewValidationError("coverage is incomplete or contains unknown bytes")
    for key, item in actual.items():
        if set(item) != set(COVERAGE_RECORD_KEYS):
            raise ReviewValidationError("invalid coverage schema")
        if item.get("disposition") not in COVERAGE_DISPOSITIONS or item.get("unreviewed"):
            raise ReviewValidationError(
                f"coverage disposition must be one of {'/'.join(COVERAGE_DISPOSITIONS)} and not unreviewed; "
                f"got {item.get('disposition')!r} for {item.get('path')!r}")
        if not isinstance(item.get("finding_refs"), list):
            raise ReviewValidationError("coverage finding_refs must be a list")
        if (key[2] == "deleted") != (item["disposition"] == DELETED_COVERAGE_DISPOSITION):
            raise ReviewValidationError("deleted paths require explicit deletion acknowledgement")
    ids: set[str] = set()
    for finding in report["findings"]:
        keys = {"id", "severity", "kind", "path", "line", "claim_key", "claim", "rationale",
                "required_change", "evidence_digest", "violated_invariant", "treatment", "status"}
        if set(finding) != keys or finding["severity"] not in SEVERITY or finding["treatment"] not in TREATMENT:
            raise ReviewValidationError("invalid finding schema")
        if finding["status"] not in REVIEWER_FINDING_STATUSES:
            raise ReviewValidationError("invalid finding status")
        if finding["id"] != stable_finding_id(finding):
            raise ReviewValidationError("finding identity mismatch")
        if finding["id"] in ids or not any(key[0] == finding["path"] and key[1] == finding["evidence_digest"] for key in expected_coverage):
            raise ReviewValidationError("duplicate finding or unresolved citation")
        ids.add(finding["id"])
    referenced = {ref for item in report["coverage"] for ref in item.get("finding_refs", [])}
    if not referenced.issubset(ids):
        raise ReviewValidationError("coverage references an unknown finding")


def open_finding_obligations(register: dict[str, dict[str, Any]], claim_limit: int = 600) -> list[dict[str, Any]]:
    """The still-blocking findings a reviewer must adjudicate, in a stable order.

    A finding is only ever cleared by a later report that re-states it with the same
    `path` and `claim_key` and a status of `resolved`. The reviewer therefore has to be
    told the exact identity of every finding still outstanding; without this it cannot
    retire work it has verified as fixed, and the register only ever grows.
    """
    obligations = []
    for entry in register.values():
        if entry.get("status", "open") in NON_BLOCKING_FINDING_STATUSES:
            continue
        obligations.append({"path": entry.get("path"), "claim_key": entry.get("claim_key"),
                            "severity": entry.get("severity"), "kind": entry.get("kind"),
                            "status": entry.get("status", "open"),
                            "claim": str(entry.get("claim", ""))[:claim_limit]})
    return sorted(obligations, key=lambda item: (item["path"] or "", item["claim_key"] or ""))


def realign_findings_to_delta(register: dict[str, dict[str, Any]],
                              coverage_paths: Any) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Park the findings this delta cannot adjudicate, and un-park the ones it can again.

    `validate_review` refuses any finding citing a path outside the delta's coverage, so a
    finding whose path is absent cannot be marked `resolved`, `still_open` or `unverified`
    by any report the validator will accept. Left blocking it traps the phase forever while
    looking like an ordinary open finding.

    Retiring it instead — which is what this did until phase 17 — trades that deadlock for a
    quieter failure. `superseded` is terminal, so a finding was destroyed by the accident of
    which files happened to be in one delta. Two costs were paid for that. Within a phase, a
    finding was retired against the *first* delta and never came back when a later
    remediation lap touched its file: phase 16 lost four `governance.py` and `delegation.py`
    findings that way, one of them the very carry-over the phase existed to discharge.
    Across phases, `approvals-unauthenticated-and-perpetual` was retired at 15:26:59 because
    phase 17's delta did not include `actions.py`, and the reviewer independently
    rediscovered the identical defect in `playbooks.py` six minutes later. The register
    cannot carry a lesson it keeps deleting.

    So absence from the delta is treated as what it actually is — a statement about *this*
    delta, not about the defect. A parked finding keeps the status it held and reclaims it
    the moment its file is reviewable again, whether that is a later lap of the same phase
    or a later phase entirely. Only a reviewer may resolve a finding; only evidence may
    close one.

    Returns the paths parked and the paths restored, for the audit trail.
    """
    citable = {path for path in coverage_paths if isinstance(path, str)}
    parked: list[str] = []
    restored: list[str] = []
    for entry in register.values():
        status = entry.get("status", "open")
        if status in CLOSED_FINDING_STATUSES:
            continue
        path = entry.get("path")
        if not isinstance(path, str) or not path:
            continue
        if path not in citable:
            if status != DEFERRED_STATUS:
                entry["deferred_from"] = status
                entry["deferred_reason"] = "cited path is not in the reviewable delta"
                entry["status"] = DEFERRED_STATUS
                parked.append(path)
        elif status == DEFERRED_STATUS:
            entry["status"] = entry.pop("deferred_from", "open")
            entry.pop("deferred_reason", None)
            restored.append(path)
    return tuple(sorted(set(parked))), tuple(sorted(set(restored)))


def accumulate_findings(register: dict[str, dict[str, Any]], report: dict[str, Any], policy_digest: str) -> None:
    candidate = {key: dict(value) for key, value in register.items()}
    evidence = report["evidence_digest"]
    for finding in report["findings"]:
        related = [item for item in candidate.values()
                   if item.get("path") == finding["path"] and item.get("claim_key") == finding["claim_key"]]
        if finding["status"] == "resolved":
            for previous in related:
                previous["status"] = "resolved"
                previous["resolved_by_evidence"] = evidence
        key = f"{evidence}:{policy_digest}:{REDUCER_VERSION}:{finding['id']}"
        previous = candidate.get(key)
        if previous:
            if SEVERITY[finding["severity"]] < SEVERITY[previous["severity"]] or TREATMENT[finding["treatment"]] < TREATMENT[previous["treatment"]]:
                raise ReviewValidationError("finding downgrade on identical evidence is forbidden")
            merged = dict(finding)
            merged["severity"] = max((previous["severity"], finding["severity"]), key=SEVERITY.get)
            merged["treatment"] = max((previous["treatment"], finding["treatment"]), key=TREATMENT.get)
            candidate[key] = merged
        else:
            stored = dict(finding)
            stored["occurrences"] = 1 + max((int(item.get("occurrences", 1)) for item in related), default=0)
            candidate[key] = stored
    register.clear()
    register.update(candidate)


def is_strategic_finding(item: dict[str, Any]) -> bool:
    """Does this finding call for a judgement rather than a fix? (the R4 predicate)

    Contested, or a HIGH that is architectural in kind or has recurred across reviews. A
    defect that survives remediation and comes back is a disagreement about what the right
    design is, not a bug someone forgot to fix.
    """
    return bool(item.get("contested")) or (
        item["severity"] == "HIGH"
        and (item["kind"] == "architecture" or int(item.get("occurrences", 1)) > 1))


def strategic_findings(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The open findings that route a phase to debate, in a stable order.

    The debate exists to settle exactly these. Handing an arbiter the whole accumulated
    register instead buries the question among findings that are resolved, superseded, or
    merely advisory, and makes any resulting decision record claim more than it decided.
    """
    return [item for item in findings
            if item.get("status", "open") not in NON_BLOCKING_FINDING_STATUSES
            and is_strategic_finding(item)]


def reduce_review(*, report: dict[str, Any] | None, findings: list[dict[str, Any]], mechanical_passed: bool,
                  coverage_valid: bool, policy: dict[str, Any], attempts: dict[str, int],
                  budget_available: bool, approvals_valid: bool) -> Transition:
    if report is None or not coverage_valid:
        return Transition("HUMAN_ESCALATION", "R1", "review missing, malformed, or incompletely covered")
    if not mechanical_passed:
        if attempts["remediation"] < policy["max_remediation_attempts"]:
            return Transition("REMEDIATE", "R2", "mechanical gates failed")
        return Transition("HUMAN_ESCALATION", "R2_EXHAUSTED", "mechanical remediation exhausted")
    open_findings = [item for item in findings
                     if item.get("status", "open") not in NON_BLOCKING_FINDING_STATUSES]
    if any(item["severity"] == "CRITICAL" for item in open_findings):
        derived = Transition("HUMAN_ESCALATION", "R3", "critical finding")
    elif any(is_strategic_finding(item) for item in open_findings):
        derived = Transition("DEBATE" if policy["debate_enabled"] and attempts["debate"] < policy["max_debate_attempts"] else "HUMAN_ESCALATION",
                             "R4", "strategic or contested finding")
    elif any(item["severity"] == "HIGH" for item in open_findings):
        derived = Transition("REMEDIATE" if attempts["remediation"] < policy["max_remediation_attempts"] else "HUMAN_ESCALATION",
                             "R5", "high fixable finding")
    elif budget_available and approvals_valid:
        derived = Transition("ADVANCE", "R7", "all machine and semantic predicates clear")
    else:
        derived = Transition("HUMAN_ESCALATION", "R7_FAIL_CLOSED", "advance conjunction incomplete")
    if report["verdict"] == "PASS" and derived.disposition != "ADVANCE":
        return Transition("HUMAN_ESCALATION", "R1_VERDICT_MISMATCH", "unsafe advisory verdict mismatch")
    if report["verdict"] != "PASS" and derived.disposition == "ADVANCE":
        return Transition("HUMAN_ESCALATION", "R1_UNEXPLAINED_HOLD", "restrictive verdict lacks a blocking finding")
    return derived
