"""Regression tests for extra-attempt grant consumption across every ceiling.

`_escalate` issues an `ExtraAttemptGrant` for any `*_EXHAUSTED` code, and
`retry --authorize-extra-attempt` authorises it — but only the phase-attempt check ever
consumed one. A grant issued for the semantic-review, remediation, review-format or
consecutive-failure ceiling was authorised and then ignored, so the ceiling fired again
immediately and `--authorize-extra-attempt` looped forever.

Worse, once `consecutive_failures` reached its limit `_escalate` rewrote *every* code to
`MAX_CONSECUTIVE_FAILURES_EXHAUSTED`, whose grant nothing could consume — a hard trap with no
exit but a config change. Phase 1 hit exactly that.

All ceilings now route through `_ceiling_reached`, so a grant means the same thing everywhere:
it names one phase and one ceiling, must be authorised with a matching nonce, and is spent on
use. The consecutive-failure ceiling is settled in `retry` because it is a running total with
no call site that could spend it.
"""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from buildctl.buildctl import HumanEscalation, Supervisor
from buildctl.supervisor.state import BuildState, ExtraAttemptGrant, StateStore

CEILINGS = ("PHASE_ATTEMPTS_EXHAUSTED", "REMEDIATION_ATTEMPTS_EXHAUSTED",
            "SEMANTIC_REVIEW_ATTEMPTS_EXHAUSTED", "REVIEW_FORMAT_ATTEMPTS_EXHAUSTED")
CONFIG = {"limits": {"max_consecutive_failures": 3}}
PHASE = SimpleNamespace(number=1)


def supervisor_for(directory: Path) -> Supervisor:
    supervisor = Supervisor.__new__(Supervisor)
    supervisor.store = StateStore(directory)
    supervisor.control_root = directory
    supervisor.config = CONFIG
    return supervisor


def state_with_grant(code, *, authorized=True, consumed=False, phase=1):
    return BuildState(status="running", current_phase=1, consecutive_failures=0,
                      extra_attempt_grant=ExtraAttemptGrant(phase, code, 0, "nonce",
                                                            authorized=authorized, consumed=consumed))


class EveryCeilingHonoursAnAuthorisedGrantTests(unittest.TestCase):
    def test_an_authorised_grant_lets_each_ceiling_continue(self):
        for code in CEILINGS:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                supervisor = supervisor_for(root)
                state = state_with_grant(code)
                supervisor.store.save(state)
                supervisor._ceiling_reached(state, PHASE, code, "detail")   # must not raise
                self.assertTrue(state.extra_attempt_grant.consumed, f"{code} did not spend the grant")
                self.assertEqual(state.status, "running", f"{code} escalated despite a grant")

    def test_the_grant_is_single_use_at_every_ceiling(self):
        for code in CEILINGS:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                supervisor = supervisor_for(root)
                state = state_with_grant(code)
                supervisor.store.save(state)
                supervisor._ceiling_reached(state, PHASE, code, "detail")
                with self.assertRaises(HumanEscalation, msg=f"{code} allowed a second pass"):
                    supervisor._ceiling_reached(state, PHASE, code, "detail")


class GrantScopeIsStillStrictTests(unittest.TestCase):
    def _expect_escalation(self, state, code):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            supervisor = supervisor_for(root)
            supervisor.store.save(state)
            with self.assertRaises(HumanEscalation):
                supervisor._ceiling_reached(state, PHASE, code, "detail")

    def test_no_grant_escalates(self):
        self._expect_escalation(BuildState(status="running", current_phase=1), CEILINGS[0])

    def test_an_unauthorised_grant_escalates(self):
        self._expect_escalation(state_with_grant(CEILINGS[0], authorized=False), CEILINGS[0])

    def test_an_already_consumed_grant_escalates(self):
        self._expect_escalation(state_with_grant(CEILINGS[0], consumed=True), CEILINGS[0])

    def test_a_grant_for_a_different_ceiling_escalates(self):
        """The gap that made this a loop: authorising one ceiling must not open another."""
        self._expect_escalation(state_with_grant("PHASE_ATTEMPTS_EXHAUSTED"),
                                "SEMANTIC_REVIEW_ATTEMPTS_EXHAUSTED")

    def test_a_grant_for_a_different_phase_escalates(self):
        self._expect_escalation(state_with_grant(CEILINGS[0], phase=7), CEILINGS[0])


class ConsecutiveFailureCeilingTests(unittest.TestCase):
    """A running total, so it is settled at authorisation rather than at a call site."""

    def _retry(self, root: Path, state: BuildState, nonce="nonce"):
        store = StateStore(root)
        store.save(state)
        supervisor = supervisor_for(root)
        supervisor.retry(True, nonce)
        return store.load()

    def test_authorising_it_forgives_the_streak_and_spends_the_grant(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            state = BuildState(status="human_escalation", current_phase=1, consecutive_failures=3,
                               escalation_code="MAX_CONSECUTIVE_FAILURES_EXHAUSTED")
            store = StateStore(root); store.save(state)
            state = store.load()
            state.extra_attempt_grant = ExtraAttemptGrant(1, "MAX_CONSECUTIVE_FAILURES_EXHAUSTED",
                                                          state.generation, "nonce")
            state.status = "human_escalation"
            state.escalation_code = "MAX_CONSECUTIVE_FAILURES_EXHAUSTED"
            store.save(state)
            supervisor = supervisor_for(root)
            supervisor.retry(True, "nonce")
            reloaded = store.load()
            self.assertEqual(reloaded.consecutive_failures, 0, "the streak must be forgiven")
            self.assertTrue(reloaded.extra_attempt_grant.consumed)
            self.assertIsNone(reloaded.escalation_code)

    def test_an_ordinary_retry_does_not_touch_the_streak(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            state = BuildState(status="human_escalation", current_phase=1, consecutive_failures=2,
                               escalation_code="REVIEW_BLOCKED")
            reloaded = self._retry(root, state)
            self.assertEqual(reloaded.consecutive_failures, 2,
                             "a non-ceiling retry must not defer the consecutive-failure ceiling")

    def test_a_ceiling_retry_without_authorisation_is_still_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            state = BuildState(status="human_escalation", current_phase=1, consecutive_failures=3,
                               escalation_code="MAX_CONSECUTIVE_FAILURES_EXHAUSTED")
            store = StateStore(root); store.save(state)
            supervisor = supervisor_for(root)
            with self.assertRaisesRegex(RuntimeError, "authorize-extra-attempt"):
                supervisor.retry(False, None)

    def test_a_bad_nonce_is_still_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            state = BuildState(status="human_escalation", current_phase=1, consecutive_failures=3,
                               escalation_code="MAX_CONSECUTIVE_FAILURES_EXHAUSTED")
            store = StateStore(root); store.save(state)
            state = store.load()
            state.extra_attempt_grant = ExtraAttemptGrant(1, "MAX_CONSECUTIVE_FAILURES_EXHAUSTED",
                                                          state.generation, "real")
            state.status = "human_escalation"
            state.escalation_code = "MAX_CONSECUTIVE_FAILURES_EXHAUSTED"
            store.save(state)
            supervisor = supervisor_for(root)
            with self.assertRaisesRegex(RuntimeError, "scope or nonce"):
                supervisor.retry(True, "wrong")
            self.assertEqual(store.load().consecutive_failures, 3, "a refused retry must change nothing")


class EscalationStillIssuesGrantsTests(unittest.TestCase):
    def test_a_ceiling_escalation_issues_a_scoped_grant(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            supervisor = supervisor_for(root)
            state = BuildState(status="running", current_phase=1)
            supervisor.store.save(state)
            with self.assertRaises(HumanEscalation):
                supervisor._ceiling_reached(state, PHASE, "SEMANTIC_REVIEW_ATTEMPTS_EXHAUSTED", "d")
            grant = state.extra_attempt_grant
            self.assertIsNotNone(grant)
            self.assertEqual(grant.ceiling_code, "SEMANTIC_REVIEW_ATTEMPTS_EXHAUSTED")
            self.assertEqual(grant.phase, 1)
            self.assertFalse(grant.authorized)
            self.assertFalse(grant.consumed)

    def test_reaching_the_consecutive_ceiling_still_rewrites_the_code(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            supervisor = supervisor_for(root)
            state = BuildState(status="running", current_phase=1, consecutive_failures=2)
            supervisor.store.save(state)
            with self.assertRaises(HumanEscalation):
                supervisor._ceiling_reached(state, PHASE, "REMEDIATION_ATTEMPTS_EXHAUSTED", "d")
            self.assertEqual(state.escalation_code, "MAX_CONSECUTIVE_FAILURES_EXHAUSTED")
            self.assertEqual(state.extra_attempt_grant.ceiling_code,
                             "MAX_CONSECUTIVE_FAILURES_EXHAUSTED")
