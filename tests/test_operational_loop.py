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


    def test_kitchen_dry_run_validation_is_fail_closed_and_durable(self) -> None:
        for idx in range(2):
            self.ingest(f"manual://dry-run-{idx}", key=f"dry-run-ingest-{idx}")
        self.warden.generate_matches(idempotency_key="dry-run-match")
        dossier = self.warden.list_kitchen_dossiers()[0]

        result = self.warden.validate_match_dry_run(
            dossier["match_id"],
            idempotency_key="dry-run-validate",
        )

        self.assertEqual(result["mode"], "DRY_RUN")
        self.assertEqual(result["status"], "BLOCKED")
        self.assertFalse(result["executed"])
        self.assertEqual(result["compatibility"], "UNKNOWN")
        self.assertIn("baseline", result["unresolved_requirements"])
        self.assertIn("acceptance_thresholds", result["unresolved_requirements"])
        self.assertIn("adapter_contract", result["unresolved_requirements"])
        self.assertIn("sandbox_binding", result["unresolved_requirements"])

        repeated = self.warden.validate_match_dry_run(
            dossier["match_id"],
            idempotency_key="dry-run-validate",
        )
        self.assertEqual(repeated, result)
        self.assertEqual(self.warden.count("validations"), 1)

        self.warden.close()
        self.warden = Warden(self.root)
        persisted = self.warden.list_validations(dossier["match_id"])
        self.assertEqual(len(persisted), 1)
        self.assertEqual(persisted[0], result)


    def test_match_regeneration_preserves_validation_history(self) -> None:
        for idx in range(2):
            self.ingest(f"manual://regen-{idx}", key=f"regen-ingest-{idx}")
        self.warden.generate_matches(idempotency_key="regen-match-1")
        dossier = self.warden.list_kitchen_dossiers()[0]
        validation = self.warden.validate_match_dry_run(
            dossier["match_id"], idempotency_key="regen-validation"
        )

        regenerated = self.warden.generate_matches(idempotency_key="regen-match-2")

        self.assertEqual(regenerated["generated_pairs"], 1)
        self.assertEqual(
            self.warden.list_validations(dossier["match_id"]),
            [validation],
        )
        self.assertEqual(len(self.warden.list_kitchen_dossiers()), 1)


    def test_validation_contract_resolves_dry_run_blockers_without_execution(self) -> None:
        for idx in range(2):
            self.ingest(f"manual://contract-{idx}", key=f"contract-ingest-{idx}")
        self.warden.generate_matches(idempotency_key="contract-match")
        dossier = self.warden.list_kitchen_dossiers()[0]

        contract = self.warden.bind_validation_contract(
            dossier["match_id"],
            baseline={"command": ["python", "-m", "unittest"]},
            acceptance_thresholds={"exit_code": 0},
            adapter_contract={"kind": "command"},
            sandbox_binding={"kind": "external", "name": "test-sandbox"},
            idempotency_key="contract-bind",
        )
        self.assertEqual(contract["status"], "BOUND")

        result = self.warden.validate_match_dry_run(
            dossier["match_id"], idempotency_key="contract-dry-run"
        )
        self.assertEqual(result["status"], "READY_FOR_AUTHORIZED_VALIDATION")
        self.assertEqual(result["unresolved_requirements"], [])
        self.assertFalse(result["executed"])
        self.assertEqual(result["compatibility"], "UNKNOWN")


    def test_observed_validation_is_adjudicated_against_bound_thresholds(self) -> None:
        for idx in range(2):
            self.ingest(f"manual://observed-{idx}", key=f"observed-ingest-{idx}")
        self.warden.generate_matches(idempotency_key="observed-match")
        dossier = self.warden.list_kitchen_dossiers()[0]
        self.warden.bind_validation_contract(
            dossier["match_id"],
            baseline={"command": ["python", "-m", "unittest"]},
            acceptance_thresholds={"exit_code": 0, "tests_failed": 0},
            adapter_contract={"kind": "command"},
            sandbox_binding={"kind": "external", "name": "test-sandbox"},
            idempotency_key="observed-contract",
        )

        result = self.warden.record_validation_observation(
            dossier["match_id"],
            observed={"exit_code": 0, "tests_failed": 0, "tests_run": 15},
            idempotency_key="observed-result",
        )

        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["compatibility"], "COMPATIBLE")
        self.assertFalse(result["executed_by_archotraz"])
        self.assertEqual(result["failed_thresholds"], [])


    def test_execution_request_is_integrity_bound_and_contains_no_implicit_execution(self) -> None:
        for idx in range(2):
            self.ingest(f"manual://execution-{idx}", key=f"execution-ingest-{idx}")
        self.warden.generate_matches(idempotency_key="execution-match")
        dossier = self.warden.list_kitchen_dossiers()[0]
        self.warden.bind_validation_contract(
            dossier["match_id"],
            baseline={"command": ["python", "-m", "unittest"]},
            acceptance_thresholds={"exit_code": 0},
            adapter_contract={"kind": "command", "timeout_seconds": 30},
            sandbox_binding={"kind": "external", "name": "test-sandbox"},
            idempotency_key="execution-contract",
        )

        request = self.warden.prepare_execution_request(
            dossier["match_id"], idempotency_key="execution-request"
        )

        self.assertEqual(request["status"], "READY")
        self.assertFalse(request["executed"])
        self.assertEqual(request["sandbox_binding"]["kind"], "external")
        self.assertEqual(len(request["components"]), 2)
        self.assertTrue(request["contract_sha256"])
        self.assertTrue(request["request_sha256"])

        repeated = self.warden.prepare_execution_request(
            dossier["match_id"], idempotency_key="execution-request"
        )
        self.assertEqual(repeated, request)


    def test_runner_receipt_must_match_prepared_execution_request(self) -> None:
        for idx in range(2):
            self.ingest(f"manual://receipt-{idx}", key=f"receipt-ingest-{idx}")
        self.warden.generate_matches(idempotency_key="receipt-match")
        dossier = self.warden.list_kitchen_dossiers()[0]
        self.warden.bind_validation_contract(
            dossier["match_id"],
            baseline={"command": ["python", "-m", "unittest"]},
            acceptance_thresholds={"exit_code": 0},
            adapter_contract={"kind": "command"},
            sandbox_binding={"kind": "external", "name": "test-sandbox"},
            idempotency_key="receipt-contract",
        )
        request = self.warden.prepare_execution_request(
            dossier["match_id"], idempotency_key="receipt-request"
        )

        result = self.warden.record_runner_receipt(
            dossier["match_id"],
            request_sha256=request["request_sha256"],
            observed={"exit_code": 0},
            runner={"name": "test-sandbox", "isolated": True},
            idempotency_key="receipt-result",
        )

        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["compatibility"], "COMPATIBLE")
        self.assertTrue(result["executed_by_runner"])
        self.assertEqual(result["request_sha256"], request["request_sha256"])

        with self.assertRaises(IntegrityFailure):
            self.warden.record_runner_receipt(
                dossier["match_id"],
                request_sha256="0" * 64,
                observed={"exit_code": 0},
                runner={"name": "test-sandbox", "isolated": True},
                idempotency_key="receipt-wrong-request",
            )


    def test_runner_receipt_must_attest_bound_isolated_runner(self) -> None:
        for idx in range(2):
            self.ingest(f"manual://attest-{idx}", key=f"attest-ingest-{idx}")
        self.warden.generate_matches(idempotency_key="attest-match")
        dossier = self.warden.list_kitchen_dossiers()[0]
        self.warden.bind_validation_contract(
            dossier["match_id"],
            baseline={"command": ["python", "-m", "unittest"]},
            acceptance_thresholds={"exit_code": 0},
            adapter_contract={"kind": "command"},
            sandbox_binding={"kind": "external", "name": "trusted-sandbox"},
            idempotency_key="attest-contract",
        )
        request = self.warden.prepare_execution_request(
            dossier["match_id"], idempotency_key="attest-request"
        )

        with self.assertRaises(IntegrityFailure):
            self.warden.record_runner_receipt(
                dossier["match_id"],
                request_sha256=request["request_sha256"],
                observed={"exit_code": 0},
                runner={"name": "other-sandbox", "isolated": True},
                idempotency_key="attest-wrong-runner",
            )

        with self.assertRaises(IntegrityFailure):
            self.warden.record_runner_receipt(
                dossier["match_id"],
                request_sha256=request["request_sha256"],
                observed={"exit_code": 0},
                runner={"name": "trusted-sandbox", "isolated": False},
                idempotency_key="attest-not-isolated",
            )


    def test_work_release_gate_requires_compatible_runner_evidence(self) -> None:
        for idx in range(2):
            self.ingest(f"manual://release-{idx}", key=f"release-ingest-{idx}")
        self.warden.generate_matches(idempotency_key="release-match")
        dossier = self.warden.list_kitchen_dossiers()[0]
        blocked = self.warden.work_release_gate(dossier["match_id"])
        self.assertEqual(blocked["status"], "BLOCKED")

        self.warden.bind_validation_contract(
            dossier["match_id"],
            baseline={"command": ["python", "-m", "unittest"]},
            acceptance_thresholds={"exit_code": 0},
            adapter_contract={"kind": "command"},
            sandbox_binding={"kind": "external", "name": "test-sandbox"},
            idempotency_key="release-contract",
        )
        request = self.warden.prepare_execution_request(
            dossier["match_id"], idempotency_key="release-request"
        )
        self.warden.record_runner_receipt(
            dossier["match_id"],
            request_sha256=request["request_sha256"],
            observed={"exit_code": 0},
            runner={"name": "test-sandbox", "isolated": True},
            idempotency_key="release-receipt",
        )
        ready = self.warden.work_release_gate(dossier["match_id"])
        self.assertEqual(ready["status"], "ELIGIBLE")
        self.assertFalse(ready["released"])
        self.assertEqual(ready["compatibility"], "COMPATIBLE")


if __name__ == "__main__":
    unittest.main()
