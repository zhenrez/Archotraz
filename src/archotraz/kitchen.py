from __future__ import annotations

import hashlib
from typing import Any


REQUIRED_VALIDATION = [
    "ablation",
    "counterfactual",
    "null_baseline",
    "constraint_check",
    "robustness_test",
]


def kitchen_id(match_id: str) -> str:
    digest = hashlib.sha256(match_id.encode()).hexdigest()[:20]
    return f"kitchen:{digest}"


def build_candidate_dossier(match: dict[str, Any]) -> dict[str, Any]:
    state = match["state"]
    return {
        "subject_id": kitchen_id(match["subject_id"]),
        "kind": "kitchen_candidate",
        "state": {
            "match_id": match["subject_id"],
            "members": state["members"],
            "status": "proposed_unvalidated",
            "hypothesis": {
                "relationship": state["relationship"],
                "basis": "exhaustive pair enumeration plus evidence-derived observations",
                "observed": state["observed"],
            },
            "unresolved_constraints": state["unresolved_constraints"],
            "required_validation": REQUIRED_VALIDATION,
            "promotion_authorized": False,
            "execution_authorized": False,
            "tested_improvement": False,
        },
    }
