from __future__ import annotations

import tempfile
from concurrent.futures import ThreadPoolExecutor
import unittest
from pathlib import Path

from archotraz.artifacts import ArtifactStore
from archotraz.bindings import BOPO, binding_status
from archotraz.ledger import Ledger, VersionConflict
from archotraz.warden import Warden


class OperationalLoopTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        self.repo = self.root / "sample_repo"
        self.repo.mkdir()
        (self.repo / "README.md").write_text("# Sample\n", encoding="utf-8")
        (self.repo / "LICENSE").write_text("MIT\n", encoding="utf-8")
        (self.repo / "pyproject.toml").write_text(
            "[project]\nname='sample'\n", encoding="utf-8"
        )
        (self.repo / "sample.py").write_text("VALUE = 1\n", encoding="utf-8")
        (self.repo / "test_sample.py").write_text(
            "def test_value(): assert True\n", encoding="utf-8"
        )
        self.ledger = Ledger(self.state / "archotraz.sqlite3")
        self.artifacts = ArtifactStore(self.state / "artifacts")
        self.warden = Warden(self.ledger, self.artifacts)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_first_operational_loop_is_deterministic_and_fail_closed(self) -> None:
        result = self.warden.intake_local(self.repo, request_id="req-001")
        candidate = result["candidate"]
        state = candidate["state"]
        self.assertEqual(state["cell"], "GEN-POP")
        self.assertFalse(state["execution_allowed"])
        self.assertEqual(
            state["execution_blocker"], "sandbox binding unresolved"
        )
        self.assertTrue(result["guard"]["passed"])
        states = {item["result_state"] for item in result["evidence"]}
        self.assertTrue(states <= {"observed", "unknown"})
        self.artifacts.verify(state["snapshot_manifest_artifact"])

        again = self.warden.intake_local(self.repo, request_id="req-001")
        self.assertEqual(
            again["candidate"]["subject_id"], candidate["subject_id"]
        )
        self.assertEqual(again["candidate"]["version"], 1)
        self.assertEqual(len(again["evidence"]), len(result["evidence"]))
        self.assertEqual(
            again["guard"]["guard_result_id"], result["guard"]["guard_result_id"]
        )

    def test_state_directory_is_excluded_from_snapshot_identity(self) -> None:
        nested_state = self.repo / ".archotraz"
        nested_ledger = Ledger(nested_state / "archotraz.sqlite3")
        nested_artifacts = ArtifactStore(nested_state / "artifacts")
        nested_warden = Warden(nested_ledger, nested_artifacts)

        first = nested_warden.intake_local(self.repo, request_id="state-exclusion-1")
        first_id = first["candidate"]["subject_id"]

        (nested_state / "noise.txt").write_text("should never affect source identity\n", encoding="utf-8")
        second = nested_warden.intake_local(self.repo, request_id="state-exclusion-2")

        self.assertEqual(second["candidate"]["subject_id"], first_id)
        manifest_digest = second["candidate"]["state"]["snapshot_manifest_artifact"]
        manifest = nested_artifacts.get_bytes(manifest_digest).decode()
        self.assertNotIn(".archotraz", manifest)

    def test_concurrent_identical_artifact_writes_converge(self) -> None:
        payload = b"same immutable artifact"
        with ThreadPoolExecutor(max_workers=4) as pool:
            digests = list(pool.map(self.artifacts.put_bytes, [payload] * 8))
        self.assertEqual(len(set(digests)), 1)
        self.artifacts.verify(digests[0])

    def test_recovered_bindings_remain_fail_closed(self) -> None:
        status = binding_status()
        self.assertEqual(BOPO.repository, "bopodev/bopo")
        self.assertEqual(BOPO.provider_type, "shell")
        self.assertFalse(BOPO.execution_authorized)
        self.assertFalse(status["untrusted_execution_allowed"])
        self.assertEqual(status["cell_threshold_policy"], "unresolved")
        self.assertEqual(len(status["sandbox_core"]), 3)

    def test_transition_rejects_stale_expected_version(self) -> None:
        result = self.warden.intake_local(self.repo, request_id="req-002")
        candidate_id = result["candidate"]["subject_id"]
        with self.assertRaises(VersionConflict):
            self.ledger.append_transition(
                subject_id=candidate_id,
                kind="repository_candidate",
                event_type="candidate.test",
                new_state=result["candidate"]["state"],
                actor="test",
                idempotency_key="stale-transition",
                expected_version=0,
            )


if __name__ == "__main__":
    unittest.main()
