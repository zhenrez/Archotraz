from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


class VersionConflict(RuntimeError):
    pass


class IdempotencyConflict(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Ledger:
    """Canonical SQLite authority for events plus current subject state."""

    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    def _initialize(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS subjects (
                    subject_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    state_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    event_type TEXT NOT NULL,
                    subject_id TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    recorded_at TEXT NOT NULL,
                    schema_version INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    provenance_json TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    FOREIGN KEY(subject_id) REFERENCES subjects(subject_id)
                );
                CREATE TABLE IF NOT EXISTS evidence (
                    evidence_id TEXT PRIMARY KEY,
                    subject_id TEXT NOT NULL,
                    evidence_type TEXT NOT NULL,
                    result_state TEXT NOT NULL,
                    artifact_digest TEXT,
                    detail_json TEXT NOT NULL,
                    recorded_at TEXT NOT NULL,
                    FOREIGN KEY(subject_id) REFERENCES subjects(subject_id)
                );
                CREATE TABLE IF NOT EXISTS guard_results (
                    guard_result_id TEXT PRIMARY KEY,
                    subject_id TEXT NOT NULL,
                    guard_name TEXT NOT NULL,
                    passed INTEGER NOT NULL CHECK (passed IN (0, 1)),
                    detail_json TEXT NOT NULL,
                    recorded_at TEXT NOT NULL,
                    FOREIGN KEY(subject_id) REFERENCES subjects(subject_id)
                );
                """
            )

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def append_transition(
        self,
        *,
        subject_id: str,
        kind: str,
        event_type: str,
        new_state: dict[str, Any],
        actor: str,
        idempotency_key: str,
        expected_version: int | None,
        payload: dict[str, Any] | None = None,
        provenance: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Atomically append an event and update the materialized subject state."""
        with self.transaction() as conn:
            existing_event = conn.execute(
                "SELECT subject_id, payload_json FROM events WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if existing_event:
                if existing_event["subject_id"] != subject_id:
                    raise IdempotencyConflict(idempotency_key)
                return self.get_subject(subject_id, conn=conn)

            current = conn.execute(
                "SELECT version FROM subjects WHERE subject_id = ?", (subject_id,)
            ).fetchone()
            actual_version = current["version"] if current else 0
            required = 0 if expected_version is None else expected_version
            if actual_version != required:
                raise VersionConflict(
                    f"{subject_id}: expected version {required}, observed {actual_version}"
                )
            next_version = actual_version + 1
            timestamp = _now()
            encoded_state = json.dumps(new_state, sort_keys=True, separators=(",", ":"))
            if current:
                conn.execute(
                    "UPDATE subjects SET kind=?, version=?, state_json=?, updated_at=? WHERE subject_id=?",
                    (kind, next_version, encoded_state, timestamp, subject_id),
                )
            else:
                conn.execute(
                    "INSERT INTO subjects(subject_id, kind, version, state_json, updated_at) VALUES(?,?,?,?,?)",
                    (subject_id, kind, next_version, encoded_state, timestamp),
                )
            conn.execute(
                """
                INSERT INTO events(
                    event_id,event_type,subject_id,actor,recorded_at,schema_version,
                    payload_json,provenance_json,idempotency_key
                ) VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (
                    str(uuid.uuid4()),
                    event_type,
                    subject_id,
                    actor,
                    timestamp,
                    1,
                    json.dumps(payload or {}, sort_keys=True),
                    json.dumps(provenance or {}, sort_keys=True),
                    idempotency_key,
                ),
            )
            return {
                "subject_id": subject_id,
                "kind": kind,
                "version": next_version,
                "state": new_state,
                "updated_at": timestamp,
            }

    def get_idempotency_subject(self, idempotency_key: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT subject_id FROM events WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
        return row["subject_id"] if row else None

    def record_evidence(
        self,
        *,
        subject_id: str,
        evidence_type: str,
        result_state: str,
        detail: dict[str, Any],
        artifact_digest: str | None = None,
    ) -> str:
        evidence_id = str(uuid.uuid4())
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO evidence VALUES(?,?,?,?,?,?,?)",
                (
                    evidence_id,
                    subject_id,
                    evidence_type,
                    result_state,
                    artifact_digest,
                    json.dumps(detail, sort_keys=True),
                    _now(),
                ),
            )
        return evidence_id

    def record_guard(
        self,
        *,
        subject_id: str,
        guard_name: str,
        passed: bool,
        detail: dict[str, Any],
    ) -> str:
        guard_result_id = str(uuid.uuid4())
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO guard_results VALUES(?,?,?,?,?,?)",
                (
                    guard_result_id,
                    subject_id,
                    guard_name,
                    int(passed),
                    json.dumps(detail, sort_keys=True),
                    _now(),
                ),
            )
        return guard_result_id

    def list_guard_results(self, subject_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM guard_results WHERE subject_id=? ORDER BY recorded_at",
                (subject_id,),
            ).fetchall()
        return [
            {
                "guard_result_id": row["guard_result_id"],
                "guard": row["guard_name"],
                "passed": bool(row["passed"]),
                "detail": json.loads(row["detail_json"]),
            }
            for row in rows
        ]

    def get_subject(
        self, subject_id: str, *, conn: sqlite3.Connection | None = None
    ) -> dict[str, Any]:
        owns = conn is None
        conn = conn or self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM subjects WHERE subject_id = ?", (subject_id,)
            ).fetchone()
            if row is None:
                raise KeyError(subject_id)
            return {
                "subject_id": row["subject_id"],
                "kind": row["kind"],
                "version": row["version"],
                "state": json.loads(row["state_json"]),
                "updated_at": row["updated_at"],
            }
        finally:
            if owns:
                conn.close()

    def list_evidence(self, subject_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM evidence WHERE subject_id=? ORDER BY recorded_at",
                (subject_id,),
            ).fetchall()
        return [
            {
                "evidence_id": row["evidence_id"],
                "type": row["evidence_type"],
                "result_state": row["result_state"],
                "artifact_digest": row["artifact_digest"],
                "detail": json.loads(row["detail_json"]),
            }
            for row in rows
        ]
