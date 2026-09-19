from __future__ import annotations

import argparse
import json
from pathlib import Path

from .artifacts import ArtifactStore
from .bindings import binding_status
from .ledger import Ledger
from .warden import Warden


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="archotraz")
    parser.add_argument(
        "--state-dir", default=".archotraz", help="canonical local state directory"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    intake = sub.add_parser(
        "intake-local", help="register and statically inspect a local repository snapshot"
    )
    intake.add_argument("path")
    intake.add_argument("--request-id")
    sub.add_parser(
        "bindings", help="show recovered runtime, sandbox, and cell-policy bindings"
    )
    process = sub.add_parser(
        "process", help="normalize persisted evidence and record the candidate cell"
    )
    process.add_argument("candidate_id")
    process.add_argument("--request-id")
    match = sub.add_parser(
        "match-kitchen",
        help="exhaustively enumerate processed pairs and create Kitchen proposals",
    )
    match.add_argument("--request-id")
    show = sub.add_parser("show", help="show a candidate state and evidence")
    show.add_argument("candidate_id")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "bindings":
        result = binding_status()
    else:
        state_dir = Path(args.state_dir)
        ledger = Ledger(state_dir / "archotraz.sqlite3")
        artifacts = ArtifactStore(state_dir / "artifacts")
        warden = Warden(ledger, artifacts)

        if args.command == "intake-local":
            result = warden.intake_local(args.path, request_id=args.request_id)
        elif args.command == "process":
            result = warden.process_candidate(
                args.candidate_id, request_id=args.request_id
            )
        elif args.command == "match-kitchen":
            result = warden.build_kitchen_candidates(request_id=args.request_id)
        else:
            result = {
                "candidate": ledger.get_subject(args.candidate_id),
                "evidence": ledger.list_evidence(args.candidate_id),
            }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
