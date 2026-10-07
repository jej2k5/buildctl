import hashlib
import unittest

from buildctl.supervisor.review import (ReviewValidationError, accumulate_findings, reduce_review,
                                     stable_finding_id, validate_review)


def finding(severity="HIGH", treatment="fix", status="open", kind="correctness"):
    value={"id":"", "severity":severity,"kind":kind,"path":"src/a.py","line":1,"claim_key":"auth-bypass",
           "claim":"bad","rationale":"evidence","required_change":"fix","evidence_digest":"blob",
           "violated_invariant":"auth","treatment":treatment,"status":status}
    value["id"]=stable_finding_id(value); return value


def report(findings=None, verdict="REMEDIATE"):
    return {"schema_version":2,"protocol_version":"buildctl-review-v2","governance_sha":"g","snapshot_digest":"s","base_sha":"b","phase":1,
            "delta_id":"e","evidence_digest":"e","reviewer":{},"attempt":1,"verdict":verdict,"findings":findings or [],
            "coverage":[{"path":"src/a.py","digest":"blob","change_type":"modified","disposition":"finding_refs" if findings else "clear",
                         "finding_refs":[x["id"] for x in findings or []],"unreviewed":False}]}


class ReviewControlTests(unittest.TestCase):
    def expected(self):
        return {"governance_sha":"g","snapshot_digest":"s","base_sha":"b","phase":1,"delta_id":"e",
                "evidence_digest":"e","reviewer":{},"coverage":{("src/a.py","blob","modified")}}

    def test_pass_with_critical_escalates(self):
        value=report([finding("CRITICAL","human")], "PASS"); validate_review(value,self.expected())
        transition=reduce_review(report=value,findings=value["findings"],mechanical_passed=True,coverage_valid=True,
            policy={"max_remediation_attempts":2,"debate_enabled":True,"max_debate_attempts":2},attempts={"remediation":0,"debate":0},budget_available=True,approvals_valid=True)
        self.assertEqual((transition.disposition,transition.rule_id),("HUMAN_ESCALATION","R1_VERDICT_MISMATCH"))

    def test_malformed_and_missing_coverage_fail(self):
        value=report(); value["coverage"]=[]
        with self.assertRaises(ReviewValidationError): validate_review(value,self.expected())
        self.assertEqual(reduce_review(report=None,findings=[],mechanical_passed=True,coverage_valid=False,
            policy={},attempts={},budget_available=True,approvals_valid=True).rule_id,"R1")

    def test_architecture_high_reaches_debate_before_remediation(self):
        value=report([finding(kind="architecture")],"DEBATE")
        transition=reduce_review(report=value,findings=value["findings"],mechanical_passed=True,coverage_valid=True,
            policy={"max_remediation_attempts":2,"debate_enabled":True,"max_debate_attempts":2},attempts={"remediation":0,"debate":0},budget_available=True,approvals_valid=True)
        self.assertEqual(transition.disposition,"DEBATE")

    def test_findings_union_rejects_downward_regrade(self):
        register={}; first=report([finding("CRITICAL","human")],"HUMAN_ESCALATION")
        accumulate_findings(register,first,"p")
        second=report([finding("HIGH","fix")],"REMEDIATE")
        with self.assertRaises(ReviewValidationError): accumulate_findings(register,second,"p")

    def test_replay_is_deterministic(self):
        value=report([],"PASS"); kwargs=dict(report=value,findings=[],mechanical_passed=True,coverage_valid=True,
            policy={"max_remediation_attempts":2,"debate_enabled":True,"max_debate_attempts":2},attempts={"remediation":0,"debate":0},budget_available=True,approvals_valid=True)
        self.assertEqual(reduce_review(**kwargs),reduce_review(**kwargs))

    def test_prior_finding_carries_until_new_evidence_explicitly_resolves_it(self):
        register = {}
        first = report([finding()], "REMEDIATE")
        accumulate_findings(register, first, "p")
        changed = finding(status="resolved")
        changed["evidence_digest"] = "new-blob"
        changed["id"] = stable_finding_id(changed)
        second = report([changed], "PASS")
        second["evidence_digest"] = "new-evidence"
        second["coverage"][0]["digest"] = "new-blob"
        accumulate_findings(register, second, "p")
        self.assertTrue(all(item["status"] == "resolved" for item in register.values()))

    def test_rejected_report_cannot_mutate_finding_register(self):
        register={}; accumulate_findings(register,report([finding("CRITICAL","human")],"HUMAN_ESCALATION"),"p")
        before={key:dict(value) for key,value in register.items()}
        invalid=finding("HIGH","fix",status="resolved")
        with self.assertRaises(ReviewValidationError):
            accumulate_findings(register,report([invalid],"PASS"),"p")
        self.assertEqual(register,before)
