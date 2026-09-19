from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


LANGUAGE_BY_SUFFIX = {
    ".py": "Python",
    ".rs": "Rust",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".js": "JavaScript",
    ".jsx": "JavaScript",
    ".yaml": "YAML",
    ".yml": "YAML",
}
MANIFESTS = {
    "pyproject.toml",
    "requirements.txt",
    "Cargo.toml",
    "package.json",
    "pnpm-lock.yaml",
    "package-lock.json",
    "yarn.lock",
}


def build_snapshot_manifest(root: str | Path) -> dict[str, Any]:
    root = Path(root).resolve()
    if not root.is_dir():
        raise ValueError(f"not a directory: {root}")
    files: list[dict[str, Any]] = []
    for path in sorted(p for p in root.rglob("*") if p.is_file() and ".git" not in p.parts):
        rel = path.relative_to(root).as_posix()
        data = path.read_bytes()
        files.append(
            {"path": rel, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
        )
    canonical = json.dumps(files, sort_keys=True, separators=(",", ":")).encode()
    return {
        "root_name": root.name,
        "tree_sha256": hashlib.sha256(canonical).hexdigest(),
        "file_count": len(files),
        "files": files,
    }


def inspect_repository(root: str | Path) -> list[dict[str, Any]]:
    root = Path(root).resolve()
    paths = [p for p in root.rglob("*") if p.is_file() and ".git" not in p.parts]
    names = {p.name for p in paths}
    languages = sorted(
        {LANGUAGE_BY_SUFFIX[p.suffix.lower()] for p in paths if p.suffix.lower() in LANGUAGE_BY_SUFFIX}
    )
    tests = [p for p in paths if "test" in p.name.lower() or "tests" in p.parts]

    def evidence(kind: str, state: str, **detail: Any) -> dict[str, Any]:
        return {"type": kind, "result_state": state, "detail": detail}

    readme = next((p for p in paths if p.name.lower().startswith("readme")), None)
    license_file = next((p for p in paths if p.name.lower().startswith("license")), None)
    manifests = sorted(name for name in names if name in MANIFESTS)
    return [
        evidence("source_inventory", "observed", file_count=len(paths)),
        evidence("languages", "observed" if languages else "unknown", values=languages),
        evidence("manifests", "observed" if manifests else "unknown", values=manifests),
        evidence("tests", "observed" if tests else "unknown", count=len(tests)),
        evidence(
            "readme",
            "observed" if readme else "unknown",
            path=readme.relative_to(root).as_posix() if readme else None,
        ),
        evidence(
            "license",
            "observed" if license_file else "unknown",
            path=license_file.relative_to(root).as_posix() if license_file else None,
        ),
    ]
