"""Server-only operator commands for durable user-erasure requests."""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from dotenv import load_dotenv

load_dotenv(REPOSITORY_ROOT / ".env", override=False)


def _requestId(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("request ID must be a UUID") from exc


def buildParser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage user-erasure requests.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    retry = subparsers.add_parser(
        "retry", help="Resume a partially failed erasure request."
    )
    retry.add_argument("--request-id", required=True, type=_requestId)
    status = subparsers.add_parser('status',help='Report account deletion and explicitly retained original audit data.')
    status.add_argument('--request-id',required=True,type=_requestId)
    return parser


def _defaultEnqueue(requestId: str) -> None:
    from nubrix.triggers.celery import runUserErasure

    runUserErasure.delay(requestId)


def main(argv=None, repository=None, enqueue=None) -> int:
    args = buildParser().parse_args(argv)
    if repository is None:
        from api.services.userErasureRepository import getUserErasureRepository

        repository = getUserErasureRepository()
    enqueue = enqueue or _defaultEnqueue

    request = repository.getRequest(args.request_id)
    if not request:
        print("Erasure request not found.", file=sys.stderr)
        return 1
    if args.command=='status':
        print(json.dumps({'requestId':args.request_id,'status':request.get('status'),
            'completedAt':str(request['completed_at']) if request.get('completed_at') else None,
            'lastErrorCode':request.get('last_error_code'),
            'steps':[{key:step.get(key) for key in ('step_name','status','last_error_code')}
                for step in request.get('steps',[])],
            'retention':request.get('retention_manifest') or {}},sort_keys=True))
        return 0
    if request.get("status") != "PARTIALLY_FAILED":
        print("Erasure request cannot be retried in its current state.", file=sys.stderr)
        return 1
    if not repository.retryRequest(args.request_id):
        print("Erasure request retry lost to another operator.", file=sys.stderr)
        return 1
    enqueue(args.request_id)
    print(f"Queued erasure request {args.request_id}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
