from __future__ import annotations

import hashlib
import itertools
import json
from typing import Any


UNRESOLVED_PROFILE_KEYS = (
    "mechanisms",
    "targets",
    "have",
    "need",
    "data_model",
    "access_pattern",
    "fidelity",
    "debt",
)


def pair_id(
    candidate_a: str, version_a: int, candidate_b: str, version_b: int
) -> str:
    ordered = sorted(((candidate_a, version_a), (candidate_b, version_b)))
    payload = "|".join(f"{candidate}@{version}" for candidate, version in ordered)
    digest = hashlib.sha256(payload.encode()).hexdigest()[:20]
    return f"match:{digest}"


def enumerate_pairs(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Exhaustively enumerate unordered depth-2 pairs with only evidence-derived facts."""
    result: list[dict[str, Any]] = []
    ordered_candidates = sorted(candidates, key=lambda item: item["subject_id"])

    for left, right in itertools.combinations(ordered_candidates, 2):
        lp = left["state"]["profile"]
        rp = right["state"]["profile"]

        left_languages = set(lp["features"]["languages"]["value"] or [])
        right_languages = set(rp["features"]["languages"]["value"] or [])
        unresolved = sorted(
            key
            for key in UNRESOLVED_PROFILE_KEYS
            if lp[key]["missing"] or rp[key]["missing"]
        )

        observed = {
            "shared_languages": sorted(left_languages & right_languages),
            "left_only_languages": sorted(left_languages - right_languages),
            "right_only_languages": sorted(right_languages - left_languages),
            "tests_observed": [
                not lp["features"]["tests"]["missing"],
                not rp["features"]["tests"]["missing"],
            ],
            "licenses_observed": [
                not lp["features"]["license"]["missing"],
                not rp["features"]["license"]["missing"],
            ],
        }
        semantic_digest = hashlib.sha256(
            json.dumps(observed, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

        result.append(
            {
                "subject_id": pair_id(
                    left["subject_id"],
                    left["version"],
                    right["subject_id"],
                    right["version"],
                ),
                "kind": "candidate_match",
                "state": {
                    "members": sorted((left["subject_id"], right["subject_id"])),
                    "member_versions": {
                        left["subject_id"]: left["version"],
                        right["subject_id"]: right["version"],
                    },
                    "method": "exhaustive_unordered_pair_v1",
                    "direction": "unordered_baseline",
                    "relationship": "UNKNOWN",
                    "observed": observed,
                    "observed_digest": semantic_digest,
                    "unresolved_constraints": unresolved,
                    "proposal_only": True,
                    "tested_improvement": False,
                },
            }
        )
    return result
