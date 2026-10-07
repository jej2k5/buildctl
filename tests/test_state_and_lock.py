import json
import tempfile
import unittest
from pathlib import Path

from buildctl.supervisor.state import BuildState, Operation, StateConflict, StateStore, SupervisorLocked
from buildctl.supervisor.audit import AuditIntegrityError


class StateAndLockTests(unittest.TestCase):
    def _seed_audit(self, directory: str, count: int = 5) -> StateStore:
        store = StateStore(Path(directory)); state = BuildState(); store.save(state)
        for number in range(count):
            store.event(state, "event", value=number)
        return store

    def test_round_trip_generation_and_compare_swap(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory))
            state = BuildState(status="running")
            store.save(state)
            self.assertEqual(state.generation, 1)
            stale = store.load()
            current = store.load(); current.status = "idle"; store.save(current, expected_generation=1)
            with self.assertRaises(StateConflict):
                store.save(stale, expected_generation=1)

    def test_duplicate_supervisor_process_is_locked(self):
        with tempfile.TemporaryDirectory() as directory:
            first, second = StateStore(Path(directory)), StateStore(Path(directory))
            with first.lock():
                with self.assertRaises(SupervisorLocked):
                    with second.lock():
                        pass

    def test_crash_during_external_call_becomes_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory))
            state = BuildState(status="running", operations={"x": Operation("x", "implement", 1, status="started")})
            store.save(state)
            loaded = store.load()
            self.assertTrue(store.recover_stale_run(loaded))
            self.assertEqual(store.load().operations["x"].status, "unknown")
            self.assertEqual(store.load().status, "human_escalation")

    def test_completed_operation_survives_crash_without_reinvocation(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory))
            op = Operation("x", "review", 2, status="completed", output_digest="abc")
            state = BuildState(status="running", operations={"x": op})
            store.save(state); loaded = store.load(); store.recover_stale_run(loaded)
            self.assertEqual(store.load().operations["x"].status, "completed")

    def test_legacy_state_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory); (path / "state.json").write_text('{"schema_version":1}')
            with self.assertRaises(RuntimeError):
                StateStore(path).load()

    def test_all_operation_states_and_limit_counters_survive_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory))
            operations = {status: Operation(status, "review", 3, status=status)
                          for status in ("pending", "started", "completed", "failed", "unknown")}
            state = BuildState(phase_attempts={"3": 3}, review_format_attempts={"3": 2},
                               semantic_review_attempts={"3": 4}, remediation_attempts={"3": 2},
                               debate_attempts={"3": 6}, consecutive_failures=3, operations=operations)
            store.save(state)
            loaded = StateStore(Path(directory)).load()
            self.assertEqual({key: value.status for key, value in loaded.operations.items()},
                             {key: key for key in operations})
            self.assertEqual((loaded.phase_attempts, loaded.review_format_attempts,
                              loaded.semantic_review_attempts, loaded.remediation_attempts,
                              loaded.debate_attempts, loaded.consecutive_failures),
                             ({"3": 3}, {"3": 2}, {"3": 4}, {"3": 2}, {"3": 6}, 3))

    def test_audit_chain_detects_corruption_on_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            store=StateStore(Path(directory)); state=BuildState(); store.save(state); store.event(state,"one",value=1)
            store.audit.path.write_text(store.audit.path.read_text().replace('"value": 1','"value": 2'))
            with self.assertRaises(AuditIntegrityError): StateStore(Path(directory)).load()

    def test_audit_head_allows_clean_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self._seed_audit(directory)
            loaded = StateStore(Path(directory)).load()
            self.assertEqual(store.audit.verify(), (loaded.audit_sequence, loaded.audit_head_hash))

    def test_audit_head_detects_tail_truncation(self):
        for removed in (1, 3):
            with self.subTest(removed=removed), tempfile.TemporaryDirectory() as directory:
                store = self._seed_audit(directory)
                lines = store.audit.path.read_text().splitlines()
                store.audit.path.write_text("\n".join(lines[:-removed]) + "\n")
                with self.assertRaisesRegex(RuntimeError, "AUDIT_HEAD_MISMATCH"):
                    StateStore(Path(directory)).load()

    def test_audit_head_detects_valid_but_uncommitted_append(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self._seed_audit(directory)
            store.audit.append("2026-01-01T00:00:00+00:00", "forged", {"value": "forged"})
            with self.assertRaisesRegex(RuntimeError, "AUDIT_HEAD_MISMATCH"):
                StateStore(Path(directory)).load()

    def test_audit_head_detects_replaced_tail(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self._seed_audit(directory)
            lines = store.audit.path.read_text().splitlines()
            tail = json.loads(lines[-1]); tail["payload"]["value"] = "replacement"
            lines[-1] = json.dumps(tail, sort_keys=True)
            store.audit.path.write_text("\n".join(lines) + "\n")
            with self.assertRaises(AuditIntegrityError):
                StateStore(Path(directory)).load()

    def test_audit_head_detects_stale_canonical_head(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self._seed_audit(directory)
            value = json.loads(store.path.read_text()); value["audit_head_hash"] = "0" * 64
            store.path.write_text(json.dumps(value))
            with self.assertRaisesRegex(RuntimeError, "AUDIT_HEAD_MISMATCH"):
                StateStore(Path(directory)).load()

    def test_audit_ahead_of_state_after_crash_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self._seed_audit(directory)
            store.audit.append("2026-01-01T00:00:00+00:00", "state_saved", {"generation": 999})
            with self.assertRaisesRegex(RuntimeError, "AUDIT_HEAD_MISMATCH"):
                StateStore(Path(directory)).load()

    def test_state_ahead_of_audit_after_crash_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self._seed_audit(directory)
            value = json.loads(store.path.read_text())
            value["audit_sequence"] += 1; value["audit_head_hash"] = "f" * 64
            store.path.write_text(json.dumps(value))
            with self.assertRaisesRegex(RuntimeError, "AUDIT_HEAD_MISMATCH"):
                StateStore(Path(directory)).load()
