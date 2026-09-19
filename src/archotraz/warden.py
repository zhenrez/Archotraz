from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from .artifacts import ArtifactStore
from .detective import build_snapshot_manifest, inspect_repository
from .ledger import Ledger


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
        state = {
            "source": {"kind": "local", "path": str(source)},
            "snapshot_digest": manifest["tree_sha256"],
            "snapshot_manifest_artifact": artifact_digest,
            "snapshot_completeness": "file-hash-manifest",
            "cell": "GEN-POP",
            "cell_reason": "A/B/C policy binding is intentionally unresolved",
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
            idempotency_key=f"intake:{request_id}",
            expected_version=0,
            payload={"request_id": request_id},
            provenance={"snapshot_manifest_artifact": artifact_digest},
        )

        evidence_records = []
        for item in inspect_repository(source):
            evidence_id = self.ledger.record_evidence(
                subject_id=candidate_id,
                evidence_type=item["type"],
                result_state=item["result_state"],
                detail=item["detail"],
                artifact_digest=artifact_digest,
            )
            evidence_records.append({"evidence_id": evidence_id, **item})

        guard = self.run_static_intake_guard(candidate_id)
        return {"candidate": subject, "evidence": evidence_records, "guard": guard}

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
        missing_provenance = [e for e in evidence if not e["artifact_digest"]]
        snapshot_ok = bool(subject["state"].get("snapshot_manifest_artifact"))
        execution_fail_closed = subject["state"].get("execution_allowed") is False
        passed = (
            not bad_states
            and not missing_provenance
            and snapshot_ok
            and execution_fail_closed
        )
        detail = {
            "explicit_evidence_states": not bad_states,
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
