from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from archotraz.artifacts import ArtifactStore
from archotraz.ledger import Ledger
from archotraz.warden import Warden


class MatchingPipelineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        self.ledger = Ledger(self.state / "archotraz.sqlite3")
        self.artifacts = ArtifactStore(self.state / "artifacts")
        self.warden = Warden(self.ledger, self.artifacts)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def make_repo(self, name: str, language_file: str) -> Path:
        repo = self.root / name
        repo.mkdir()
        (repo / "README.md").write_text(f"# {name}\n", encoding="utf-8")
        (repo / "LICENSE").write_text("MIT\n", encoding="utf-8")
        (repo / language_file).write_text("value = 1\n", encoding="utf-8")
        return repo

    def test_processor_persists_explicit_missingness_and_real_cell_record(self) -> None:
        repo = self.make_repo("alpha", "alpha.py")
        intake = self.warden.intake_local(repo, request_id="alpha-intake")
        processed = self.warden.process_candidate(
            intake["candidate"]["subject_id"], request_id="alpha-process"
        )

        profile = processed["candidate"]["state"]["profile"]
        self.assertEqual(profile["schema_version"], 1)
        self.assertEqual(profile["features"]["languages"]["value"], ["Python"])
        self.assertFalse(profile["features"]["languages"]["missing"])
        self.assertTrue(profile["features"]["tests"]["missing"])
        self.assertIsNone(profile["features"]["tests"]["value"])
        self.assertIsNone(profile["representation"]["presence"]["tests"])

        cell = processed["cell"]
        self.assertEqual(cell["kind"], "cell_record")
        self.assertEqual(cell["state"]["block"], "GEN-POP")
        self.assertEqual(cell["state"]["candidate_id"], intake["candidate"]["subject_id"])
        self.assertEqual(cell["state"]["assignment_policy"], "unresolved")
        self.assertFalse(cell["state"]["execution_authorized"])

        repeated = self.warden.process_candidate(
            intake["candidate"]["subject_id"], request_id="alpha-process-fresh"
        )
        self.assertEqual(
            repeated["candidate"]["version"], processed["candidate"]["version"]
        )
        self.assertEqual(repeated["cell"]["subject_id"], cell["subject_id"])

    def test_exhaustive_pair_run_reconciles_universe_and_kitchen_is_unvalidated(self) -> None:
        alpha = self.make_repo("alpha", "alpha.py")
        beta = self.make_repo("beta", "beta.py")
        gamma = self.make_repo("gamma", "gamma.rs")

        for index, repo in enumerate((alpha, beta, gamma), start=1):
            intake = self.warden.intake_local(repo, request_id=f"intake-{index}")
            self.warden.process_candidate(
                intake["candidate"]["subject_id"], request_id=f"process-{index}"
            )

        result = self.warden.build_kitchen_candidates(request_id="match-001")

        self.assertEqual(result["run"]["state"]["eligible_count"], 3)
        self.assertEqual(result["run"]["state"]["declared_pair_universe"], 3)
        self.assertEqual(result["run"]["state"]["generated_count"], 3)
        self.assertEqual(result["run"]["state"]["excluded_count"], 0)
        self.assertEqual(len(result["matches"]), 3)
        self.assertEqual(len(result["kitchen"]), 3)

        for match in result["matches"]:
            state = match["state"]
            self.assertEqual(state["relationship"], "UNKNOWN")
            self.assertEqual(state["direction"], "unordered_baseline")
            self.assertEqual(state["method"], "exhaustive_unordered_pair_v1")
            self.assertIn("mechanisms", state["unresolved_constraints"])
            self.assertIn("data_model", state["unresolved_constraints"])

        for dossier in result["kitchen"]:
            state = dossier["state"]
            self.assertEqual(state["status"], "proposed_unvalidated")
            self.assertFalse(state["promotion_authorized"])
            self.assertFalse(state["execution_authorized"])
            self.assertEqual(
                state["required_validation"],
                [
                    "ablation",
                    "counterfactual",
                    "null_baseline",
                    "constraint_check",
                    "robustness_test",
                ],
            )

        replay = self.warden.build_kitchen_candidates(request_id="match-001")
        self.assertEqual(replay["run"]["subject_id"], result["run"]["subject_id"])
        self.assertEqual(
            [m["subject_id"] for m in replay["matches"]],
            [m["subject_id"] for m in result["matches"]],
        )

        fresh_run = self.warden.build_kitchen_candidates(request_id="match-002")
        self.assertNotEqual(fresh_run["run"]["subject_id"], result["run"]["subject_id"])
        self.assertEqual(
            [m["subject_id"] for m in fresh_run["matches"]],
            [m["subject_id"] for m in result["matches"]],
        )
        self.assertEqual(
            [d["subject_id"] for d in fresh_run["kitchen"]],
            [d["subject_id"] for d in result["kitchen"]],
        )


if __name__ == "__main__":
    unittest.main()
