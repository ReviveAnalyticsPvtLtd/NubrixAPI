"""Read-only rollout check for the private AnalyticsHub bucket.

Reads one project's metadata.json and one of its tables, then prints JSON with
counts only, never stored content. Run with --expect-private after the flip.

    python scripts/privateBucketPreflight.py --project-id <id> --table <name> [--expect-private]
"""

__all__ = ["runPreflight"]


import argparse
import json
import os
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from dotenv import load_dotenv
import httpx

from utils.projectStorage import readJson, readTablePage


def _checkAuthenticatedRead(projectId: str) -> dict:
    try:
        readJson(projectId, "metadata.json")
        return {"authenticatedRead": "ok"}
    except Exception as e:
        return {"authenticatedRead": "failed", "authenticatedReadError": type(e).__name__}


def _checkPagedRead(projectId: str, tableName: str) -> dict:
    try:
        _, totalRows = readTablePage(projectId, tableName, offset=0, limit=1)
        return {"pagedReadRows": totalRows}
    except Exception as e:
        return {"pagedReadRows": None, "pagedReadError": type(e).__name__}


def _checkPublicRoute(projectId: str) -> str:
    url = f"{os.environ['SUPABASE_URL'].rstrip('/')}/storage/v1/object/public/AnalyticsHub/{projectId}/metadata.json"
    return "reachable" if httpx.get(url, timeout=20).status_code == 200 else "refused"


def runPreflight(projectId: str, tableName: str, expectPrivate: bool = False) -> dict:
    """Check authenticated reads, the paged table read and the public route."""
    result = {**_checkAuthenticatedRead(projectId), **_checkPagedRead(projectId, tableName)}
    result["publicRoute"] = _checkPublicRoute(projectId)
    publicRouteOk = result["publicRoute"] == "refused" or not expectPrivate
    result["ok"] = result["authenticatedRead"] == "ok" and result["pagedReadRows"] is not None and publicRouteOk
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--table", required=True)
    parser.add_argument("--expect-private", action="store_true")
    args = parser.parse_args()
    load_dotenv(REPOSITORY_ROOT / ".env")
    result = runPreflight(args.project_id, args.table, expectPrivate=args.expect_private)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
