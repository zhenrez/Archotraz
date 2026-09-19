from __future__ import annotations

import hashlib
import itertools
import json
import os
import sqlite3
import tempfile
import threading
import uuid
import zipfile
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any, Callable

from .errors import IdempotencyConflict, IntegrityFailure, NotFound, VersionConflict

EVIDENCE_STATES = {
    "observed",
    "claimed",
    "inferred",
    "unknown",
    "unsupported",
    "skipped",
    "failed",
    "not_applicable",
}
CELLS = {"A", "B", "C", "GEN_POP", "AD_SEG"}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class Warden:
    """Deterministic control shell around ARCHOTRAZ's canonical evidence ledger.

    The SQLite ledger and content-addressed artifact store are authoritative.
    Matching and Kitchen dossiers are proposals only; no code execution, model
    call, automatic scoring, or automatic cell assignment occurs here.
    """

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.artifacts_root = self.root / "artifacts" / "sha256"
        self.artifacts_root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "archotraz.sqlite3"
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._init_schema()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @staticmethod
    def sha256(payload: bytes) -> str:
        return hashlib.sha256(payload).hexdigest()

    @staticmethod
    def _stable_id(prefix: str, *parts: str) -> str:
        material = "\0".join(parts).encode("utf-8")
        return f"{prefix}_{hashlib.sha256(material).hexdigest()[:24]}"

    def _init_schema(self) -> None:
        schema = """
        CREATE TABLE IF NOT EXISTS candidates (
            id TEXT PRIMARY KEY,
            source_uri TEXT NOT NULL UNIQUE,
            current_snapshot_id TEXT,
            eligible INTEGER NOT NULL DEFAULT 1,
            exclusion_reason TEXT,
            cell TEXT,
            version INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS snapshots (
            id TEXT PRIMARY KEY,
            candidate_id TEXT NOT NULL REFERENCES candidates(id),
            sha256 TEXT NOT NULL,
            size INTEGER NOT NULL,
            filename TEXT NOT NULL,
            artifact_path TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(candidate_id, sha256)
        );

        CREATE TABLE IF NOT EXISTS evidence (
            id TEXT PRIMARY KEY,
            candidate_id TEXT NOT NULL REFERENCES candidates(id),
            snapshot_id TEXT NOT NULL REFERENCES snapshots(id),
            state TEXT NOT NULL,
            kind TEXT NOT NULL,
            value_json TEXT NOT NULL,
            provenance TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS events (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL UNIQUE,
            candidate_id TEXT,
            event_type TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS idempotency (
            key TEXT PRIMARY KEY,
            operation TEXT NOT NULL,
            request_hash TEXT NOT NULL,
            response_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS matches (
            id TEXT PRIMARY KEY,
            left_candidate_id TEXT NOT NULL REFERENCES candidates(id),
            right_candidate_id TEXT NOT NULL REFERENCES candidates(id),
            left_snapshot_id TEXT NOT NULL REFERENCES snapshots(id),
            right_snapshot_id TEXT NOT NULL REFERENCES snapshots(id),
            status TEXT NOT NULL,
            compatibility TEXT NOT NULL,
            dossier_path TEXT NOT NULL,
            dossier_sha256 TEXT NOT NULL,
            generated_at TEXT NOT NULL
        );
        """
        with self._lock, self._conn:
            self._conn.executescript(schema)
            match_columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(matches)")}
            if "dossier_sha256" not in match_columns:
                self._conn.execute("ALTER TABLE matches ADD COLUMN dossier_sha256 TEXT")

    def _request_hash(self, operation: str, payload: dict[str, Any]) -> str:
        encoded = _canonical_json({"operation": operation, "payload": payload}).encode("utf-8")
        return self.sha256(encoded)

    def _idempotency_lookup(self, key: str, operation: str, request_hash: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT operation, request_hash, response_json FROM idempotency WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return None
        if row["operation"] != operation or row["request_hash"] != request_hash:
            raise IdempotencyConflict(
                f"idempotency key {key!r} was already used for a different request"
            )
        return json.loads(row["response_json"])

    def _idempotency_store(
        self, key: str, operation: str, request_hash: str, response: dict[str, Any]
    ) -> None:
        self._conn.execute(
            "INSERT INTO idempotency(key, operation, request_hash, response_json, created_at) VALUES(?,?,?,?,?)",
            (key, operation, request_hash, _canonical_json(response), _utc_now()),
        )

    def _record_event(
        self, event_type: str, payload: dict[str, Any], candidate_id: str | None = None
    ) -> str:
        event_id = f"evt_{uuid.uuid4().hex}"
        self._conn.execute(
            "INSERT INTO events(event_id, candidate_id, event_type, payload_json, created_at) VALUES(?,?,?,?,?)",
            (event_id, candidate_id, event_type, _canonical_json(payload), _utc_now()),
        )
        return event_id

    def _artifact_path(self, digest: str) -> Path:
        return self.artifacts_root / digest[:2] / digest

    def _write_verified_artifact(self, payload: bytes, digest: str) -> Path:
        path = self._artifact_path(digest)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            existing = path.read_bytes()
            if len(existing) != len(payload) or self.sha256(existing) != digest:
                raise IntegrityFailure(f"existing artifact at {path} failed digest verification")
            return path

        fd, temp_name = tempfile.mkstemp(prefix=f".{digest}.", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            temp_path = Path(temp_name)
            written = temp_path.read_bytes()
            if len(written) != len(payload) or self.sha256(written) != digest:
                raise IntegrityFailure("artifact failed verification before commit")
            os.replace(temp_path, path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
        return path

    def _store_json_artifact(self, value: dict[str, Any]) -> tuple[Path, str]:
        encoded = (_canonical_json(value) + "\n").encode("utf-8")
        digest = self.sha256(encoded)
        return self._write_verified_artifact(encoded, digest), digest

    def _read_verified_artifact(self, path: Path, digest: str, *, expected_size: int | None = None) -> bytes:
        if not path.exists():
            raise IntegrityFailure(f"referenced artifact is missing: {path}")
        payload = path.read_bytes()
        if expected_size is not None and len(payload) != expected_size:
            raise IntegrityFailure(f"referenced artifact has wrong size: {path}")
        if self.sha256(payload) != digest:
            raise IntegrityFailure(f"referenced artifact is corrupt: {path}")
        return payload

    def _static_evidence(self, payload: bytes) -> list[tuple[str, str, Any, str]]:
        items: list[tuple[str, str, Any, str]] = []
        try:
            with zipfile.ZipFile(BytesIO(payload)) as archive:
                names = sorted(info.filename for info in archive.infolist() if not info.is_dir())
        except (zipfile.BadZipFile, OSError):
            items.append(("observed", "snapshot_format", "opaque", "snapshot-bytes"))
            items.extend(
                [
                    ("unknown", "repository_structure", None, "static-inspection"),
                    ("unknown", "runtime_verified", None, "not-executed"),
                    ("unknown", "performance_verified", None, "not-measured"),
                    ("unknown", "compatibility_verified", None, "not-tested"),
                ]
            )
            return items

        suffix_languages = {
            ".py": "Python",
            ".rs": "Rust",
            ".ts": "TypeScript",
            ".tsx": "TypeScript",
            ".js": "JavaScript",
            ".jsx": "JavaScript",
            ".yaml": "YAML",
            ".yml": "YAML",
        }
        languages: dict[str, int] = {}
        for name in names:
            language = suffix_languages.get(Path(name).suffix.lower())
            if language:
                languages[language] = languages.get(language, 0) + 1

        manifest_names = {
            "pyproject.toml",
            "requirements.txt",
            "package.json",
            "cargo.toml",
            "go.mod",
            "pom.xml",
        }
        manifests = [name for name in names if Path(name).name.lower() in manifest_names]
        tests_present = any(
            Path(name).name.lower().startswith("test_")
            or Path(name).name.lower().endswith(("_test.py", ".spec.ts", ".test.ts", ".spec.js", ".test.js"))
            or "tests/" in name.lower()
            for name in names
        )
        readme_present = any(Path(name).name.lower().startswith("readme") for name in names)

        items.extend(
            [
                ("observed", "snapshot_format", "zip", "zip-directory"),
                ("observed", "file_count", len(names), "zip-directory"),
                ("observed", "languages_by_file_count", languages, "filename-suffixes"),
                ("observed", "manifests", manifests, "zip-directory"),
                ("observed", "tests_present", tests_present, "path-patterns"),
                ("observed", "readme_present", readme_present, "path-patterns"),
                ("unknown", "runtime_verified", None, "not-executed"),
                ("unknown", "performance_verified", None, "not-measured"),
                ("unknown", "compatibility_verified", None, "not-tested"),
            ]
        )
        return items

    def ingest_snapshot(
        self,
        *,
        source_uri: str,
        snapshot_bytes: bytes,
        filename: str,
        description: str = "",
        idempotency_key: str,
    ) -> dict[str, Any]:
        if not source_uri.strip():
            raise ValueError("source_uri is required")
        if not idempotency_key.strip():
            raise ValueError("idempotency_key is required")
        digest = self.sha256(snapshot_bytes)
        request = {
            "source_uri": source_uri,
            "sha256": digest,
            "filename": filename,
            "description": description,
        }
        request_hash = self._request_hash("ingest_snapshot", request)

        with self._lock:
            prior = self._idempotency_lookup(idempotency_key, "ingest_snapshot", request_hash)
            if prior is not None:
                return prior

            artifact_path = self._write_verified_artifact(snapshot_bytes, digest)
            now = _utc_now()
            candidate_id = self._stable_id("cand", source_uri)
            snapshot_id = self._stable_id("snap", candidate_id, digest)

            with self._conn:
                existing_candidate = self._conn.execute(
                    "SELECT id FROM candidates WHERE source_uri = ?", (source_uri,)
                ).fetchone()
                if existing_candidate is None:
                    self._conn.execute(
                        "INSERT INTO candidates(id, source_uri, current_snapshot_id, eligible, exclusion_reason, cell, version, created_at, updated_at) "
                        "VALUES(?,?,NULL,1,NULL,NULL,0,?,?)",
                        (candidate_id, source_uri, now, now),
                    )
                else:
                    candidate_id = existing_candidate["id"]
                    snapshot_id = self._stable_id("snap", candidate_id, digest)

                existing_snapshot = self._conn.execute(
                    "SELECT id FROM snapshots WHERE candidate_id = ? AND sha256 = ?",
                    (candidate_id, digest),
                ).fetchone()
                if existing_snapshot is None:
                    relative_artifact = artifact_path.relative_to(self.root).as_posix()
                    self._conn.execute(
                        "INSERT INTO snapshots(id, candidate_id, sha256, size, filename, artifact_path, created_at) VALUES(?,?,?,?,?,?,?)",
                        (snapshot_id, candidate_id, digest, len(snapshot_bytes), filename, relative_artifact, now),
                    )
                    evidence = self._static_evidence(snapshot_bytes)
                    for index, (state, kind, value, provenance) in enumerate(evidence):
                        if state not in EVIDENCE_STATES:
                            raise ValueError(f"invalid evidence state: {state}")
                        evidence_id = self._stable_id("ev", snapshot_id, str(index), state, kind)
                        self._conn.execute(
                            "INSERT INTO evidence(id, candidate_id, snapshot_id, state, kind, value_json, provenance, created_at) VALUES(?,?,?,?,?,?,?,?)",
                            (evidence_id, candidate_id, snapshot_id, state, kind, _canonical_json(value), provenance, now),
                        )

                submitted_claim = description.strip()
                if submitted_claim:
                    claim_id = self._stable_id("evclaim", snapshot_id, submitted_claim)
                    self._conn.execute(
                        "INSERT OR IGNORE INTO evidence(id, candidate_id, snapshot_id, state, kind, value_json, provenance, created_at) VALUES(?,?,?,?,?,?,?,?)",
                        (
                            claim_id,
                            candidate_id,
                            snapshot_id,
                            "claimed",
                            "submitted_description",
                            _canonical_json(submitted_claim),
                            "manual-entry",
                            now,
                        ),
                    )

                previous = self._conn.execute(
                    "SELECT current_snapshot_id FROM candidates WHERE id = ?", (candidate_id,)
                ).fetchone()["current_snapshot_id"]
                changed = previous != snapshot_id
                if changed:
                    self._conn.execute(
                        "UPDATE candidates SET current_snapshot_id = ?, version = version + 1, updated_at = ? WHERE id = ?",
                        (snapshot_id, now, candidate_id),
                    )

                response = {
                    "candidate_id": candidate_id,
                    "snapshot_id": snapshot_id,
                    "sha256": digest,
                    "size": len(snapshot_bytes),
                    "source_uri": source_uri,
                    "snapshot_changed": changed,
                }
                self._record_event(
                    "snapshot.ingested",
                    {
                        "snapshot_id": snapshot_id,
                        "sha256": digest,
                        "size": len(snapshot_bytes),
                        "filename": filename,
                        "snapshot_changed": changed,
                    },
                    candidate_id,
                )
                self._idempotency_store(idempotency_key, "ingest_snapshot", request_hash, response)
            return response

    def read_snapshot_bytes(self, snapshot_id: str) -> bytes:
        with self._lock:
            row = self._conn.execute(
                "SELECT sha256, size, artifact_path FROM snapshots WHERE id = ?", (snapshot_id,)
            ).fetchone()
            if row is None:
                raise NotFound(f"snapshot {snapshot_id!r} not found")
            path = self.root / row["artifact_path"]
            return self._read_verified_artifact(path, row["sha256"], expected_size=row["size"])

    def count(self, table: str) -> int:
        allowed = {"candidates", "snapshots", "evidence", "events", "idempotency", "matches"}
        if table not in allowed:
            raise ValueError(f"unsupported table: {table}")
        with self._lock:
            return int(self._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])

    def _candidate_transition(
        self,
        *,
        candidate_id: str,
        operation: str,
        event_type: str,
        idempotency_key: str,
        expected_version: int,
        payload: dict[str, Any],
        update: Callable[[str, int], int],
    ) -> dict[str, Any]:
        request_payload = {"candidate_id": candidate_id, "expected_version": expected_version, **payload}
        request_hash = self._request_hash(operation, request_payload)
        with self._lock:
            prior = self._idempotency_lookup(idempotency_key, operation, request_hash)
            if prior is not None:
                return prior
            if self._conn.execute("SELECT 1 FROM candidates WHERE id = ?", (candidate_id,)).fetchone() is None:
                raise NotFound(f"candidate {candidate_id!r} not found")
            with self._conn:
                changed = update(_utc_now(), expected_version)
                if changed != 1:
                    current = self._conn.execute(
                        "SELECT version FROM candidates WHERE id = ?", (candidate_id,)
                    ).fetchone()
                    if current is None:
                        raise NotFound(f"candidate {candidate_id!r} not found")
                    raise VersionConflict(
                        f"candidate {candidate_id!r} is version {current['version']}; expected {expected_version}"
                    )
                event_payload = {**payload, "expected_version": expected_version}
                self._record_event(event_type, event_payload, candidate_id)
                response = self.get_candidate(candidate_id)
                self._idempotency_store(idempotency_key, operation, request_hash, response)
            return response

    def exclude_candidate(
        self,
        candidate_id: str,
        *,
        reason: str,
        expected_version: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        if not reason.strip():
            raise ValueError("exclusion reason is required")

        def update(now: str, version: int) -> int:
            cursor = self._conn.execute(
                "UPDATE candidates SET eligible = 0, exclusion_reason = ?, version = version + 1, updated_at = ? "
                "WHERE id = ? AND version = ?",
                (reason.strip(), now, candidate_id, version),
            )
            return cursor.rowcount

        return self._candidate_transition(
            candidate_id=candidate_id,
            operation="exclude_candidate",
            event_type="candidate.excluded",
            idempotency_key=idempotency_key,
            expected_version=expected_version,
            payload={"reason": reason.strip()},
            update=update,
        )

    def restore_candidate(
        self, candidate_id: str, *, expected_version: int, idempotency_key: str
    ) -> dict[str, Any]:
        def update(now: str, version: int) -> int:
            cursor = self._conn.execute(
                "UPDATE candidates SET eligible = 1, exclusion_reason = NULL, version = version + 1, updated_at = ? "
                "WHERE id = ? AND version = ?",
                (now, candidate_id, version),
            )
            return cursor.rowcount

        return self._candidate_transition(
            candidate_id=candidate_id,
            operation="restore_candidate",
            event_type="candidate.restored",
            idempotency_key=idempotency_key,
            expected_version=expected_version,
            payload={},
            update=update,
        )

    def assign_cell(
        self,
        candidate_id: str,
        *,
        cell: str,
        expected_version: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        normalized = cell.upper().replace("-", "_")
        if normalized not in CELLS:
            raise ValueError(f"cell must be one of {sorted(CELLS)}")

        def update(now: str, version: int) -> int:
            cursor = self._conn.execute(
                "UPDATE candidates SET cell = ?, version = version + 1, updated_at = ? "
                "WHERE id = ? AND version = ?",
                (normalized, now, candidate_id, version),
            )
            return cursor.rowcount

        return self._candidate_transition(
            candidate_id=candidate_id,
            operation="assign_cell",
            event_type="candidate.cell_assigned",
            idempotency_key=idempotency_key,
            expected_version=expected_version,
            payload={"cell": normalized, "assignment_mode": "manual"},
            update=update,
        )

    @staticmethod
    def _candidate_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "source_uri": row["source_uri"],
            "current_snapshot_id": row["current_snapshot_id"],
            "eligible": bool(row["eligible"]),
            "exclusion_reason": row["exclusion_reason"],
            "cell": row["cell"],
            "version": row["version"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def get_candidate(self, candidate_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM candidates WHERE id = ?", (candidate_id,)).fetchone()
            if row is None:
                raise NotFound(f"candidate {candidate_id!r} not found")
            return self._candidate_dict(row)

    def list_candidates(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM candidates ORDER BY created_at, id").fetchall()
            return [self._candidate_dict(row) for row in rows]

    def candidate_history(self, candidate_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT seq, event_id, event_type, payload_json, created_at FROM events WHERE candidate_id = ? ORDER BY seq",
                (candidate_id,),
            ).fetchall()
            return [
                {
                    "seq": row["seq"],
                    "event_id": row["event_id"],
                    "event_type": row["event_type"],
                    "payload": json.loads(row["payload_json"]),
                    "created_at": row["created_at"],
                }
                for row in rows
            ]

    def get_dossier(self, candidate_id: str) -> dict[str, Any]:
        candidate = self.get_candidate(candidate_id)
        snapshot_id = candidate["current_snapshot_id"]
        if snapshot_id is None:
            raise NotFound(f"candidate {candidate_id!r} has no current snapshot")
        with self._lock:
            snapshot_row = self._conn.execute("SELECT * FROM snapshots WHERE id = ?", (snapshot_id,)).fetchone()
            evidence_rows = self._conn.execute(
                "SELECT id, state, kind, value_json, provenance, created_at FROM evidence WHERE snapshot_id = ? ORDER BY id",
                (snapshot_id,),
            ).fetchall()
        return {
            "candidate": candidate,
            "snapshot": {
                "id": snapshot_row["id"],
                "sha256": snapshot_row["sha256"],
                "size": snapshot_row["size"],
                "filename": snapshot_row["filename"],
                "artifact_path": snapshot_row["artifact_path"],
                "created_at": snapshot_row["created_at"],
            },
            "evidence": [
                {
                    "id": row["id"],
                    "state": row["state"],
                    "kind": row["kind"],
                    "value": json.loads(row["value_json"]),
                    "provenance": row["provenance"],
                    "created_at": row["created_at"],
                }
                for row in evidence_rows
            ],
        }

    def generate_matches(self, *, idempotency_key: str) -> dict[str, Any]:
        with self._lock:
            candidates = self.list_candidates()
            current = [c for c in candidates if c["current_snapshot_id"]]
            fingerprint = [
                {
                    "id": c["id"],
                    "snapshot": c["current_snapshot_id"],
                    "eligible": c["eligible"],
                    "version": c["version"],
                }
                for c in current
            ]
            request_hash = self._request_hash("generate_matches", {"population": fingerprint})
            prior = self._idempotency_lookup(idempotency_key, "generate_matches", request_hash)
            if prior is not None:
                return prior

            declared_pair_universe = len(current) * (len(current) - 1) // 2
            generated: list[dict[str, Any]] = []
            excluded: list[dict[str, Any]] = []
            now = _utc_now()
            snapshot_ids = [str(c["current_snapshot_id"]) for c in current]
            if snapshot_ids:
                placeholders = ",".join("?" for _ in snapshot_ids)
                snapshot_rows = self._conn.execute(
                    f"SELECT id, sha256 FROM snapshots WHERE id IN ({placeholders})", snapshot_ids
                ).fetchall()
                snapshot_digests = {row["id"]: row["sha256"] for row in snapshot_rows}
            else:
                snapshot_digests = {}

            for left, right in itertools.combinations(current, 2):
                if not left["eligible"] or not right["eligible"]:
                    held = [c["id"] for c in (left, right) if not c["eligible"]]
                    excluded.append(
                        {
                            "left_candidate_id": left["id"],
                            "right_candidate_id": right["id"],
                            "reason": "ineligible_candidate",
                            "held_candidate_ids": held,
                        }
                    )
                    continue
                ordered = sorted([left, right], key=lambda c: c["id"])
                left2, right2 = ordered
                match_id = self._stable_id(
                    "match",
                    left2["id"],
                    str(left2["current_snapshot_id"]),
                    right2["id"],
                    str(right2["current_snapshot_id"]),
                )
                dossier = {
                    "schema_version": "archotraz.kitchen-dossier/v1",
                    "match_id": match_id,
                    "status": "PROPOSED_NOT_VALIDATED",
                    "compatibility": "UNKNOWN",
                    "improvement_claimed": False,
                    "directionality": "UNSPECIFIED",
                    "adapter_contract": "UNKNOWN",
                    "components": [
                        {
                            "candidate_id": left2["id"],
                            "snapshot_id": left2["current_snapshot_id"],
                            "snapshot_sha256": snapshot_digests[str(left2["current_snapshot_id"])],
                        },
                        {
                            "candidate_id": right2["id"],
                            "snapshot_id": right2["current_snapshot_id"],
                            "snapshot_sha256": snapshot_digests[str(right2["current_snapshot_id"])],
                        },
                    ],
                    "unresolved_requirements": [
                        "baseline",
                        "acceptance_thresholds",
                        "adapter_contract",
                        "sandbox_binding",
                    ],
                    "authority": {
                        "automatic_scoring": "disabled",
                        "automatic_cell_assignment": "disabled",
                        "untrusted_execution": "disabled",
                    },
                }
                path, dossier_digest = self._store_json_artifact(dossier)
                generated.append(
                    {
                        "id": match_id,
                        "left_candidate_id": left2["id"],
                        "right_candidate_id": right2["id"],
                        "left_snapshot_id": left2["current_snapshot_id"],
                        "right_snapshot_id": right2["current_snapshot_id"],
                        "dossier_path": path.relative_to(self.root).as_posix(),
                        "dossier_sha256": dossier_digest,
                    }
                )

            report = {
                "declared_pair_universe": declared_pair_universe,
                "generated_pairs": len(generated),
                "excluded_pairs": len(excluded),
                "excluded": excluded,
                "compatibility_assumption": "UNKNOWN_UNTIL_TESTED",
            }
            with self._conn:
                self._conn.execute("DELETE FROM matches")
                for item in generated:
                    self._conn.execute(
                        "INSERT INTO matches(id, left_candidate_id, right_candidate_id, left_snapshot_id, right_snapshot_id, status, compatibility, dossier_path, dossier_sha256, generated_at) "
                        "VALUES(?,?,?,?,?,'PROPOSED','UNKNOWN',?,?,?)",
                        (
                            item["id"],
                            item["left_candidate_id"],
                            item["right_candidate_id"],
                            item["left_snapshot_id"],
                            item["right_snapshot_id"],
                            item["dossier_path"],
                            item["dossier_sha256"],
                            now,
                        ),
                    )
                self._record_event("matching.generated", report)
                self._idempotency_store(idempotency_key, "generate_matches", request_hash, report)
            return report

    def list_kitchen_dossiers(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT dossier_path, dossier_sha256 FROM matches ORDER BY id"
            ).fetchall()
        dossiers: list[dict[str, Any]] = []
        for row in rows:
            if not row["dossier_sha256"]:
                raise IntegrityFailure(f"Kitchen dossier {row['dossier_path']!r} has no recorded digest")
            path = self.root / row["dossier_path"]
            payload = self._read_verified_artifact(path, row["dossier_sha256"])
            dossiers.append(json.loads(payload.decode("utf-8")))
        return dossiers

    def state_summary(self) -> dict[str, Any]:
        candidates = self.list_candidates()
        return {
            "counts": {
                "candidates": len(candidates),
                "eligible": sum(1 for item in candidates if item["eligible"]),
                "held": sum(1 for item in candidates if not item["eligible"]),
                "snapshots": self.count("snapshots"),
                "evidence": self.count("evidence"),
                "matches": self.count("matches"),
                "events": self.count("events"),
            },
            "candidates": candidates,
            "capabilities": {
                "untrusted_execution": "disabled",
                "automatic_scoring": "disabled",
                "automatic_cell_assignment": "disabled",
                "model_api_required": "disabled",
            },
        }
