from __future__ import annotations

import argparse
import json
import uuid
from pathlib import Path

from .console import serve
from .core import Warden


def _json(value: object) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="archotraz", description="ARCHOTRAZ Warden local control shell")
    parser.add_argument("--data-dir", type=Path, default=Path(".archotraz"), help="canonical local state directory")
    sub = parser.add_subparsers(dest="command", required=True)

    serve_p = sub.add_parser("serve", help="run the local operations console")
    serve_p.add_argument("--host", default="127.0.0.1")
    serve_p.add_argument("--port", type=int, default=8765)
    serve_p.add_argument("--open-browser", action="store_true")

    ingest = sub.add_parser("ingest", help="ingest a local immutable snapshot without executing it")
    ingest.add_argument("snapshot", type=Path)
    ingest.add_argument("--source-uri", required=True)
    ingest.add_argument("--description", default="")
    ingest.add_argument("--idempotency-key")

    sub.add_parser("status", help="show canonical state summary")

    matches = sub.add_parser("matches", help="generate proposed pair matches")
    matches.add_argument("--idempotency-key")

    hold = sub.add_parser("hold", help="place a candidate on a recoverable hold")
    hold.add_argument("candidate_id")
    hold.add_argument("--reason", required=True)
    hold.add_argument("--expected-version", type=int)
    hold.add_argument("--idempotency-key")

    restore = sub.add_parser("restore", help="restore a held candidate")
    restore.add_argument("candidate_id")
    restore.add_argument("--expected-version", type=int)
    restore.add_argument("--idempotency-key")

    cell = sub.add_parser("assign-cell", help="manually assign a candidate to a cell")
    cell.add_argument("candidate_id")
    cell.add_argument("cell")
    cell.add_argument("--expected-version", type=int)
    cell.add_argument("--idempotency-key")
    return parser


def _expected_version(warden: Warden, candidate_id: str, supplied: int | None) -> int:
    if supplied is not None:
        return supplied
    return int(warden.get_candidate(candidate_id)["version"])


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    warden = Warden(args.data_dir)
    try:
        if args.command == "serve":
            serve(warden, host=args.host, port=args.port, open_browser=args.open_browser)
            return 0
        if args.command == "ingest":
            payload = args.snapshot.read_bytes()
            _json(
                warden.ingest_snapshot(
                    source_uri=args.source_uri,
                    snapshot_bytes=payload,
                    filename=args.snapshot.name,
                    description=args.description,
                    idempotency_key=args.idempotency_key or f"cli-ingest-{uuid.uuid4().hex}",
                )
            )
            return 0
        if args.command == "status":
            _json(warden.state_summary())
            return 0
        if args.command == "matches":
            _json(warden.generate_matches(idempotency_key=args.idempotency_key or f"cli-match-{uuid.uuid4().hex}"))
            return 0
        if args.command == "hold":
            _json(
                warden.exclude_candidate(
                    args.candidate_id,
                    reason=args.reason,
                    expected_version=_expected_version(warden, args.candidate_id, args.expected_version),
                    idempotency_key=args.idempotency_key or f"cli-hold-{uuid.uuid4().hex}",
                )
            )
            return 0
        if args.command == "restore":
            _json(
                warden.restore_candidate(
                    args.candidate_id,
                    expected_version=_expected_version(warden, args.candidate_id, args.expected_version),
                    idempotency_key=args.idempotency_key or f"cli-restore-{uuid.uuid4().hex}",
                )
            )
            return 0
        if args.command == "assign-cell":
            _json(
                warden.assign_cell(
                    args.candidate_id,
                    cell=args.cell,
                    expected_version=_expected_version(warden, args.candidate_id, args.expected_version),
                    idempotency_key=args.idempotency_key or f"cli-cell-{uuid.uuid4().hex}",
                )
            )
            return 0
        raise AssertionError(f"unhandled command {args.command}")
    finally:
        warden.close()
