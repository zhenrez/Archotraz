from __future__ import annotations

from typing import Any


FEATURE_TYPES = (
    "source_inventory",
    "languages",
    "manifests",
    "tests",
    "readme",
    "license",
)


def normalize_profile(evidence: list[dict[str, Any]]) -> dict[str, Any]:
    """Normalize already-recorded evidence without inventing new facts or scores."""
    by_type = {item["type"]: item for item in evidence}
    features: dict[str, dict[str, Any]] = {}

    for feature_type in FEATURE_TYPES:
        item = by_type.get(feature_type)
        if item is None:
            features[feature_type] = {
                "value": None,
                "missing": True,
                "result_state": "unknown",
                "evidence_refs": [],
            }
            continue

        detail = item["detail"]
        result_state = item["result_state"]
        missing = result_state != "observed"

        if feature_type in {"languages", "manifests"}:
            value = detail.get("values") if not missing else None
        elif feature_type in {"source_inventory", "tests"}:
            key = "file_count" if feature_type == "source_inventory" else "count"
            value = detail.get(key) if not missing else None
        else:
            value = detail.get("path") if not missing else None

        features[feature_type] = {
            "value": value,
            "missing": missing,
            "result_state": result_state,
            "evidence_refs": [item["evidence_id"]],
        }

    language_values = features["languages"]["value"] or []
    representation = {
        "language_multi_hot": {language: 1 for language in language_values},
        "presence": {
            "tests": None if features["tests"]["missing"] else 1,
            "readme": None if features["readme"]["missing"] else 1,
            "license": None if features["license"]["missing"] else 1,
        },
        "missingness": {
            key: int(value["missing"]) for key, value in sorted(features.items())
        },
    }

    return {
        "schema_version": 1,
        "features": features,
        "representation": representation,
        "mechanisms": {"value": None, "missing": True},
        "targets": {"value": None, "missing": True},
        "have": {"value": None, "missing": True},
        "need": {"value": None, "missing": True},
        "data_model": {"value": None, "missing": True},
        "access_pattern": {"value": None, "missing": True},
        "fidelity": {"value": None, "missing": True},
        "debt": {"value": None, "missing": True},
    }
