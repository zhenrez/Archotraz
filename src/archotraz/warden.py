from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from typing import Any

from .artifacts import ArtifactStore
from .detective import build_snapshot_manifest, inspect_repository
from .kitchen import build_candidate_dossier
from .ledger import IdempotencyConflict, Ledger
from .matcher import enumerate_pairs
from .processor import normalize_profile


EXPECTED_INTAKE_EVIDENCE = {
    "source_inventory",
    "languages",
    "manifests",
    "tests",
    "readme",
    "license",
}


class Warden:
    """Coordinates bounded deterministic work; workers do not self-promote."""

    def __init__(self, ledger: Ledger, artifacts: ArtifactStore):
        self.ledger = ledger
        self.artifacts = artifacts

    def intake_local(
        self, source_path: str | Path, *, request_id: str | None = None
    ) -> dict[str, Any]:
        request_id = request_id or str(uuid.uuid4())
        source = Path(source_path).resolve()
        manifest = build_snapshot_manifest(source)
        artifact_bytes = json.dumps(manifest, sort_keys=True).encode()
        artifact_digest = self.artifacts.put_bytes(artifact_bytes)

        candidate_id = f"candidate:{manifest['tree_sha256'][:20]}"
        idempotency_key = f"intake:{request_id}"
        existing_subject = self.ledger.get_idempotency_subject(idempotency_key)
        if existing_subject is not None and existing_subject != candidate_id:
            raise IdempotencyConflict(idempotency_key)

        try:
            subject = self.ledger.get_subject(candidate_id)
        except KeyError:
            subject = None

        if subject is not None:
            evidence_records = self._ensure_intake_evidence(
                source, candidate_id, subject["state"]["snapshot_manifest_artifact"]
            )
            guards = self.ledger.list_guard_results(candidate_id)
            guard = guards[-1] if guards else self.run_static_intake_guard(candidate_id)
            return {
                "candidate": subject,
                "evidence": evidence_records,
                "guard": guard,
            }

        state = {
            "source": {"kind": "local", "path": str(source)},
            "snapshot_digest": manifest["tree_sha256"],
            "snapshot_manifest_artifact": artifact_digest,
            "snapshot_completeness": "file-hash-manifest",
            "cell": "GEN-POP",
            "cell_reason": "numeric A/B/C assignment policy is unresolved",
            "disposition": "hold",
            "adoption_status": "research",
            "execution_allowed": False,
            "execution_blocker": "sandbox binding unresolved",
        }
        subject = self.ledger.append_transition(
            subject_id=candidate_id,
            kind="repository_candidate",
            event_type="candidate.intake.registered",
            new_state=state,
            actor="warden",
            idempotency_key=idempotency_key,
            expected_version=0,
            payload={"request_id": request_id},
            provenance={"snapshot_manifest_artifact": artifact_digest},
        )

        evidence_records = self._ensure_intake_evidence(
            source, candidate_id, artifact_digest
        )
        guard = self.run_static_intake_guard(candidate_id)
        return {"candidate": subject, "evidence": evidence_records, "guard": guard}

    def _ensure_intake_evidence(
        self, source: Path, candidate_id: str, artifact_digest: str
    ) -> list[dict[str, Any]]:
        existing = self.ledger.list_evidence(candidate_id)
        existing_types = {item["type"] for item in existing}
        for item in inspect_repository(source):
            if item["type"] in existing_types:
                continue
            self.ledger.record_evidence(
                subject_id=candidate_id,
                evidence_type=item["type"],
                result_state=item["result_state"],
                detail=item["detail"],
                artifact_digest=artifact_digest,
            )
        return self.ledger.list_evidence(candidate_id)

    def _ensure_cell_record(
        self,
        *,
        cell_id: str,
        candidate_id: str,
        candidate_version: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        try:
            return self.ledger.get_subject(cell_id)
        except KeyError:
            pass

        cell_state = {
            "candidate_id": candidate_id,
            "block": "GEN-POP",
            "assignment_policy": "unresolved",
            "assignment_reason": "numeric A/B/C policy not recovered",
            "eligibility": "retained",
            "stage": "processor",
            "execution_authorized": False,
        }
        return self.ledger.append_transition(
            subject_id=cell_id,
            kind="cell_record",
            event_type="cell.assignment.recorded",
            new_state=cell_state,
            actor="processor",
            idempotency_key=f"{idempotency_key}:cell",
            expected_version=0,
            payload={"candidate_id": candidate_id},
            provenance={"candidate_version": candidate_version},
        )

    def process_candidate(
        self, candidate_id: str, *, request_id: str | None = None
    ) -> dict[str, Any]:
        request_id = request_id or str(uuid.uuid4())
        idempotency_key = f"process:{request_id}"
        existing_subject = self.ledger.get_idempotency_subject(idempotency_key)
        cell_id = f"cell:{candidate_id.split(':', 1)[-1]}"
        if existing_subject is not None:
            if existing_subject != candidate_id:
                raise IdempotencyConflict(idempotency_key)
            candidate = self.ledger.get_subject(candidate_id)
            return {
                "candidate": candidate,
                "cell": self._ensure_cell_record(
                    cell_id=cell_id,
                    candidate_id=candidate_id,
                    candidate_version=candidate["version"],
                    idempotency_key=idempotency_key,
                ),
            }

        candidate = self.ledger.get_subject(candidate_id)
        evidence = self.ledger.list_evidence(candidate_id)
        profile = normalize_profile(evidence)
        if candidate["state"].get("profile") == profile:
            return {
                "candidate": candidate,
                "cell": self._ensure_cell_record(
                    cell_id=cell_id,
                    candidate_id=candidate_id,
                    candidate_version=candidate["version"],
                    idempotency_key=idempotency_key,
                ),
            }

        new_state = dict(candidate["state"])
        new_state["profile"] = profile
        new_state["processor_status"] = "normalized"
        new_state["cell"] = "GEN-POP"
        new_state["cell_reason"] = "numeric A/B/C assignment policy is unresolved"

        processed = self.ledger.append_transition(
            subject_id=candidate_id,
            kind=candidate["kind"],
            event_type="candidate.processor.normalized",
            new_state=new_state,
            actor="processor",
            idempotency_key=idempotency_key,
            expected_version=candidate["version"],
            payload={"request_id": request_id, "profile_schema_version": 1},
            provenance={
                "evidence_refs": [item["evidence_id"] for item in evidence],
                "snapshot_manifest_artifact": candidate["state"][
                    "snapshot_manifest_artifact"
                ],
            },
        )

        cell = self._ensure_cell_record(
            cell_id=cell_id,
            candidate_id=candidate_id,
            candidate_version=processed["version"],
            idempotency_key=idempotency_key,
        )
        return {"candidate": processed, "cell": cell}

    def build_kitchen_candidates(
        self, *, request_id: str | None = None
    ) -> dict[str, Any]:
        request_id = request_id or str(uuid.uuid4())
        run_id = f"run:matching:{hashlib.sha256(request_id.encode()).hexdigest()[:20]}"
        idempotency_key = f"matching:{request_id}"
        existing_subject = self.ledger.get_idempotency_subject(idempotency_key)
        if existing_subject is not None:
            if existing_subject != run_id:
                raise IdempotencyConflict(idempotency_key)
            run = self.ledger.get_subject(run_id)
            return {
                "run": run,
                "matches": [
                    self.ledger.get_subject(subject_id)
                    for subject_id in run["state"]["match_ids"]
                ],
                "kitchen": [
                    self.ledger.get_subject(subject_id)
                    for subject_id in run["state"]["kitchen_ids"]
                ],
            }

        retained_candidates = [
            item
            for item in self.ledger.list_subjects(kind="repository_candidate")
            if item["state"].get("disposition") != "exclude"
        ]
        candidates = [
            item
            for item in retained_candidates
            if item["state"].get("profile") is not None
        ]
        pairs = enumerate_pairs(candidates)
        match_subjects = []
        kitchen_subjects = []

        for pair in pairs:
            try:
                match = self.ledger.get_subject(pair["subject_id"])
                if match["state"] != pair["state"]:
                    raise RuntimeError(
                        f"semantic collision for persisted match {pair['subject_id']}"
                    )
            except KeyError:
                match = self.ledger.append_transition(
                    subject_id=pair["subject_id"],
                    kind=pair["kind"],
                    event_type="match.proposed",
                    new_state=pair["state"],
                    actor="matcher",
                    idempotency_key=f"match:{pair['subject_id']}",
                    expected_version=0,
                    payload={"originating_request_id": request_id},
                    provenance={"member_versions": pair["state"]["member_versions"]},
                )
            match_subjects.append(match)

            dossier_spec = build_candidate_dossier(match)
            try:
                dossier = self.ledger.get_subject(dossier_spec["subject_id"])
                if dossier["state"] != dossier_spec["state"]:
                    raise RuntimeError(
                        f"semantic collision for Kitchen dossier {dossier_spec['subject_id']}"
                    )
            except KeyError:
                dossier = self.ledger.append_transition(
                    subject_id=dossier_spec["subject_id"],
                    kind=dossier_spec["kind"],
                    event_type="kitchen.candidate.proposed",
                    new_state=dossier_spec["state"],
                    actor="kitchen",
                    idempotency_key=f"kitchen:{dossier_spec['subject_id']}",
                    expected_version=0,
                    payload={"originating_request_id": request_id},
                    provenance={"match_id": match["subject_id"]},
                )
            kitchen_subjects.append(dossier)

        declared = len(candidates) * (len(candidates) - 1) // 2
        run_state = {
            "method": "exhaustive_unordered_pair_v1",
            "retained_count": len(retained_candidates),
            "eligible_count": len(candidates),
            "declared_pair_universe": declared,
            "generated_count": len(match_subjects),
            "excluded_count": 0,
            "pending_count": len(retained_candidates) - len(candidates),
            "match_ids": [item["subject_id"] for item in match_subjects],
            "kitchen_ids": [item["subject_id"] for item in kitchen_subjects],
            "reconciled": declared == len(match_subjects),
            "scoring_policy": "none",
            "model_calls": 0,
        }
        run = self.ledger.append_transition(
            subject_id=run_id,
            kind="matching_run",
            event_type="matching.run.completed",
            new_state=run_state,
            actor="warden",
            idempotency_key=idempotency_key,
            expected_version=0,
            payload={"request_id": request_id},
            provenance={
                "candidate_versions": {
                    item["subject_id"]: item["version"] for item in candidates
                }
            },
        )
        return {
            "run": run,
            "matches": match_subjects,
            "kitchen": kitchen_subjects,
        }

    def run_static_intake_guard(self, candidate_id: str) -> dict[str, Any]:
        subject = self.ledger.get_subject(candidate_id)
        evidence = self.ledger.list_evidence(candidate_id)
        explicit_states = {
            "observed",
            "claimed",
            "inferred",
            "unknown",
            "unsupported",
            "skipped",
            "failed",
            "not_applicable",
        }
        bad_states = [
            e for e in evidence if e["result_state"] not in explicit_states
        ]
        evidence_types = {e["type"] for e in evidence}
        missing_required_types = sorted(EXPECTED_INTAKE_EVIDENCE - evidence_types)
        missing_provenance = [e for e in evidence if not e["artifact_digest"]]
        snapshot_ok = bool(subject["state"].get("snapshot_manifest_artifact"))
        execution_fail_closed = subject["state"].get("execution_allowed") is False
        passed = (
            not bad_states
            and not missing_required_types
            and not missing_provenance
            and snapshot_ok
            and execution_fail_closed
        )
        detail = {
            "explicit_evidence_states": not bad_states,
            "required_evidence_complete": not missing_required_types,
            "missing_required_evidence": missing_required_types,
            "evidence_has_artifact_provenance": not missing_provenance,
            "snapshot_reference_present": snapshot_ok,
            "untrusted_execution_fail_closed": execution_fail_closed,
            "promotion_authorized": False,
        }
        guard_result_id = self.ledger.record_guard(
            subject_id=candidate_id,
            guard_name="static_intake_integrity_v1",
            passed=passed,
            detail=detail,
        )
        return {
            "guard_result_id": guard_result_id,
            "guard": "static_intake_integrity_v1",
            "passed": passed,
            "detail": detail,
        }
