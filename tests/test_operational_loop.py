from __future__ import annotations

import io
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from archotraz.core import Warden  # type: ignore  # noqa: E402
from archotraz.errors import (  # type: ignore  # noqa: E402
    IdempotencyConflict,
    IntegrityFailure,
    VersionConflict,
)


def make_repo_zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, content in sorted(files.items()):
            zf.writestr(name, content)
    return buf.getvalue()


class OperationalEvidenceLoopTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.warden = Warden(self.root)

    def tearDown(self) -> None:
        self.warden.close()
        self.tmp.cleanup()

    def ingest(self, source: str, *, key: str, description: str = "claim") -> dict[str, object]:
        return self.warden.ingest_snapshot(
            source_uri=source,
            snapshot_bytes=make_repo_zip({"README.md": source.encode()}),
            filename="repo.zip",
            description=description,
            idempotency_key=key,
        )

    def test_snapshot_bytes_are_preserved_exactly(self) -> None:
        payload = make_repo_zip({"README.md": b"hello\r\nworld\x00\xff", "pyproject.toml": b"[project]\nname='x'\n"})
        result = self.warden.ingest_snapshot(
            source_uri="manual://repo-one",
            snapshot_bytes=payload,
            filename="repo-one.zip",
            description="submitted claim",
            idempotency_key="ingest-1",
        )
        stored = self.warden.read_snapshot_bytes(result["snapshot_id"])
        self.assertEqual(stored, payload)
        self.assertEqual(result["sha256"], self.warden.sha256(payload))

    def test_retry_is_deduplicated_and_conflicting_retry_is_rejected(self) -> None:
        payload = make_repo_zip({"README.md": b"one"})
        first = self.warden.ingest_snapshot(
            source_uri="manual://repo-one",
            snapshot_bytes=payload,
            filename="one.zip",
            description="claim",
            idempotency_key="same-key",
        )
        second = self.warden.ingest_snapshot(
            source_uri="manual://repo-one",
            snapshot_bytes=payload,
            filename="one.zip",
            description="claim",
            idempotency_key="same-key",
        )
        self.assertEqual(first, second)
        self.assertEqual(self.warden.count("snapshots"), 1)
        with self.assertRaises(IdempotencyConflict):
            self.warden.ingest_snapshot(
                source_uri="manual://repo-one",
                snapshot_bytes=make_repo_zip({"README.md": b"changed"}),
                filename="one.zip",
                description="claim",
                idempotency_key="same-key",
            )

    def test_exclusions_are_recoverable_history_not_deletion(self) -> None:
        result = self.ingest("manual://repo-one", key="ingest-exclusion")
        candidate_id = str(result["candidate_id"])
        initial = self.warden.get_candidate(candidate_id)
        excluded = self.warden.exclude_candidate(
            candidate_id,
            reason="insufficient evidence",
            expected_version=initial["version"],
            idempotency_key="exclude-1",
        )
        self.assertFalse(excluded["eligible"])
        self.assertEqual(excluded["exclusion_reason"], "insufficient evidence")
        restored = self.warden.restore_candidate(
            candidate_id,
            expected_version=excluded["version"],
            idempotency_key="restore-1",
        )
        self.assertTrue(restored["eligible"])
        history = self.warden.candidate_history(candidate_id)
        self.assertEqual([e["event_type"] for e in history[-2:]], ["candidate.excluded", "candidate.restored"])

    def test_stale_expected_version_is_rejected_without_new_event(self) -> None:
        result = self.ingest("manual://versioned", key="version-ingest")
        candidate_id = str(result["candidate_id"])
        initial = self.warden.get_candidate(candidate_id)
        updated = self.warden.assign_cell(
            candidate_id,
            cell="A",
            expected_version=initial["version"],
            idempotency_key="version-cell",
        )
        event_count = len(self.warden.candidate_history(candidate_id))
        with self.assertRaises(VersionConflict):
            self.warden.exclude_candidate(
                candidate_id,
                reason="stale tab",
                expected_version=initial["version"],
                idempotency_key="version-stale",
            )
        current = self.warden.get_candidate(candidate_id)
        self.assertEqual(current["version"], updated["version"])
        self.assertEqual(current["cell"], "A")
        self.assertTrue(current["eligible"])
        self.assertEqual(len(self.warden.candidate_history(candidate_id)), event_count)

    def test_pair_counts_reconcile_with_generated_and_excluded(self) -> None:
        candidate_ids = []
        for idx in range(4):
            result = self.ingest(f"manual://repo-{idx}", key=f"ingest-{idx}", description=f"repo {idx}")
            candidate_ids.append(str(result["candidate_id"]))
        held = self.warden.get_candidate(candidate_ids[-1])
        self.warden.exclude_candidate(
            candidate_ids[-1],
            reason="held",
            expected_version=held["version"],
            idempotency_key="exclude-last",
        )
        report = self.warden.generate_matches(idempotency_key="match-1")
        self.assertEqual(report["declared_pair_universe"], 6)
        self.assertEqual(report["generated_pairs"], 3)
        self.assertEqual(report["excluded_pairs"], 3)
        self.assertEqual(report["generated_pairs"] + report["excluded_pairs"], report["declared_pair_universe"])


    def test_feature_projection_preserves_observed_false_and_unknown(self) -> None:
        payload = make_repo_zip({
            "README.md": b"# Repo\n",
            "pyproject.toml": b"[project]\nname='feature-proof'\n",
        })
        result = self.warden.ingest_snapshot(
            source_uri="manual://feature-proof",
            snapshot_bytes=payload,
            filename="feature-proof.zip",
            idempotency_key="feature-proof-ingest",
        )
        projection = self.warden.candidate_feature_projection(str(result["candidate_id"]))
        self.assertEqual(projection["schema_version"], "archotraz.feature-projection/v1")
        self.assertEqual(projection["raw_primitives"]["tests_present"], False)
        self.assertEqual(projection["missingness"]["tests_present"], "OBSERVED")
        self.assertIsNone(projection["raw_primitives"]["runtime_verified"])
        self.assertEqual(projection["missingness"]["runtime_verified"], "UNKNOWN")
        self.assertEqual(projection["evidence_state"]["runtime_verified"], "unknown")
        self.assertEqual(projection["automatic_scoring"], "disabled")
        self.assertEqual(projection["automatic_cell_assignment"], "disabled")

    def test_feature_projection_distinguishes_not_retrieved(self) -> None:
        result = self.ingest("manual://not-retrieved", key="not-retrieved-ingest")
        candidate_id = str(result["candidate_id"])
        with self.warden._conn:  # noqa: SLF001 - bounded evidence-fixture mutation
            self.warden._conn.execute(  # noqa: SLF001
                "DELETE FROM evidence WHERE candidate_id = ? AND kind = ?",
                (candidate_id, "performance_verified"),
            )
        projection = self.warden.candidate_feature_projection(candidate_id)
        self.assertEqual(projection["missingness"]["performance_verified"], "NOT_RETRIEVED")
        self.assertEqual(projection["evidence_state"]["performance_verified"], "not_retrieved")
        self.assertEqual(projection["missingness"]["runtime_verified"], "UNKNOWN")

    def test_kitchen_dossiers_include_raw_feature_projections_without_scoring(self) -> None:
        for idx in range(2):
            self.ingest(f"manual://feature-pair-{idx}", key=f"feature-pair-{idx}")
        report = self.warden.generate_matches(idempotency_key="feature-pair-match")
        self.assertEqual(report["declared_pair_universe"], 1)
        dossier = self.warden.list_kitchen_dossiers()[0]
        self.assertEqual(dossier["status"], "PROPOSED_NOT_VALIDATED")
        self.assertEqual(dossier["compatibility"], "UNKNOWN")
        for component in dossier["components"]:
            projection = component["feature_projection"]
            self.assertEqual(projection["candidate_id"], component["candidate_id"])
            self.assertEqual(projection["snapshot_sha256"], component["snapshot_sha256"])
            self.assertEqual(projection["automatic_scoring"], "disabled")
            self.assertEqual(projection["automatic_cell_assignment"], "disabled")


    def test_feature_comparison_is_unscored_and_preserves_missingness(self) -> None:
        left = self.warden.ingest_snapshot(
            source_uri="manual://compare-left",
            snapshot_bytes=make_repo_zip({
                "README.md": b"# Left\n",
                "tests/test_left.py": b"def test_left(): pass\n",
            }),
            filename="left.zip",
            idempotency_key="compare-left",
        )
        right = self.warden.ingest_snapshot(
            source_uri="manual://compare-right",
            snapshot_bytes=make_repo_zip({"README.md": b"# Right\n"}),
            filename="right.zip",
            idempotency_key="compare-right",
        )
        with self.warden._conn:  # noqa: SLF001 - bounded missingness fixture
            self.warden._conn.execute(  # noqa: SLF001
                "DELETE FROM evidence WHERE candidate_id = ? AND kind = ?",
                (str(right["candidate_id"]), "performance_verified"),
            )
        self.warden.generate_matches(idempotency_key="compare-match")
        dossier = self.warden.list_kitchen_dossiers()[0]
        comparison = dossier["feature_comparison"]
        self.assertEqual(comparison["schema_version"], "archotraz.feature-comparison/v1")
        self.assertIsNone(comparison["automatic_score"])
        self.assertEqual(comparison["scoring_status"], "DISABLED")
        self.assertEqual(comparison["compatibility"], "UNKNOWN")
        tests_feature = comparison["features"]["tests_present"]
        self.assertEqual(tests_feature["left_value"], True)
        self.assertEqual(tests_feature["left_missingness"], "OBSERVED")
        self.assertEqual(tests_feature["right_value"], False)
        self.assertEqual(tests_feature["right_missingness"], "OBSERVED")
        performance = comparison["features"]["performance_verified"]
        self.assertEqual(performance["left_missingness"], "UNKNOWN")
        self.assertEqual(performance["right_missingness"], "NOT_RETRIEVED")

    def test_kitchen_dossiers_are_proposals_not_claimed_improvements(self) -> None:
        for idx in range(2):
            self.ingest(f"manual://repo-{idx}", key=f"dossier-{idx}", description=f"repo {idx}")
        self.warden.generate_matches(idempotency_key="match-dossier")
        dossiers = self.warden.list_kitchen_dossiers()
        self.assertEqual(len(dossiers), 1)
        dossier = dossiers[0]
        self.assertEqual(dossier["status"], "PROPOSED_NOT_VALIDATED")
        self.assertEqual(dossier["compatibility"], "UNKNOWN")
        self.assertFalse(dossier["improvement_claimed"])
        self.assertIn("baseline", dossier["unresolved_requirements"])
        self.assertEqual(dossier["directionality"], "UNSPECIFIED")
        self.assertEqual(dossier["adapter_contract"], "UNKNOWN")
        self.assertTrue(all(component["snapshot_sha256"] for component in dossier["components"]))

    def test_kitchen_dossier_corruption_is_detected(self) -> None:
        for idx in range(2):
            self.ingest(f"manual://integrity-{idx}", key=f"integrity-{idx}")
        self.warden.generate_matches(idempotency_key="integrity-match")
        row = self.warden._conn.execute(  # noqa: SLF001 - integrity assertion over canonical metadata
            "SELECT dossier_path, dossier_sha256 FROM matches"
        ).fetchone()
        self.assertIsNotNone(row)
        path = self.root / row["dossier_path"]
        self.assertEqual(self.warden.sha256(path.read_bytes()), row["dossier_sha256"])
        path.write_bytes(path.read_bytes() + b"corrupt")
        with self.assertRaises(IntegrityFailure):
            self.warden.list_kitchen_dossiers()

    def test_same_snapshot_can_accumulate_distinct_manual_claims(self) -> None:
        payload = make_repo_zip({"README.md": b"same bytes"})
        first = self.warden.ingest_snapshot(
            source_uri="manual://claims",
            snapshot_bytes=payload,
            filename="claims.zip",
            description="first claim",
            idempotency_key="claim-one",
        )
        second = self.warden.ingest_snapshot(
            source_uri="manual://claims",
            snapshot_bytes=payload,
            filename="claims.zip",
            description="second claim",
            idempotency_key="claim-two",
        )
        self.assertEqual(first["snapshot_id"], second["snapshot_id"])
        dossier = self.warden.get_dossier(first["candidate_id"])
        claims = {
            item["value"]
            for item in dossier["evidence"]
            if item["state"] == "claimed" and item["kind"] == "submitted_description"
        }
        self.assertEqual(claims, {"first claim", "second claim"})

    def test_restart_preserves_candidate_history_and_artifact_integrity(self) -> None:
        result = self.ingest("manual://persistent", key="persist-ingest", description="persistent claim")
        candidate = self.warden.get_candidate(str(result["candidate_id"]))
        self.warden.exclude_candidate(
            str(result["candidate_id"]),
            reason="temporary hold",
            expected_version=candidate["version"],
            idempotency_key="persist-hold",
        )
        snapshot_bytes = self.warden.read_snapshot_bytes(str(result["snapshot_id"]))
        self.warden.close()
        self.warden = Warden(self.root)

        candidate = self.warden.get_candidate(str(result["candidate_id"]))
        self.assertFalse(candidate["eligible"])
        self.assertEqual(candidate["exclusion_reason"], "temporary hold")
        self.assertEqual(self.warden.read_snapshot_bytes(str(result["snapshot_id"])), snapshot_bytes)
        self.assertEqual(
            [event["event_type"] for event in self.warden.candidate_history(str(result["candidate_id"]))][-2:],
            ["snapshot.ingested", "candidate.excluded"],
        )

    def test_detective_distinguishes_claims_observations_and_unknowns(self) -> None:
        payload = make_repo_zip({
            "README.md": b"# Repo\nClaims to be very fast.\n",
            "pyproject.toml": b"[project]\nname='example'\n",
            "src/example.py": b"def hi(): return 'hi'\n",
            "tests/test_example.py": b"def test_hi(): pass\n",
        })
        result = self.warden.ingest_snapshot(
            source_uri="manual://evidence",
            snapshot_bytes=payload,
            filename="evidence.zip",
            description="User says it is fast",
            idempotency_key="evidence-1",
        )
        dossier = self.warden.get_dossier(result["candidate_id"])
        states = {item["state"] for item in dossier["evidence"]}
        self.assertIn("claimed", states)
        self.assertIn("observed", states)
        self.assertIn("unknown", states)


if __name__ == "__main__":
    unittest.main()
