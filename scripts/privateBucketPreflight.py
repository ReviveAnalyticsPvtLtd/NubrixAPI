"""Read-only rollout check for the private AnalyticsHub bucket.

Reads one project's metadata.json and one of its tables, then prints JSON with
counts, cache headers and storage error messages only, never stored content or
a key. Run it before the deploy on a small table and on a large pandas-written
one. Run with --expect-private after the flip, with SUPABASE_ANON_KEY in the
environment.

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

from utils.projectStorage import StorageReadError, authHeaders, objectUrl, readJson, readTablePage

_REFUSED_STATUSES = {400, 401, 403, 404}


def _errorText(error: Exception) -> str:
    """A StorageReadError holds only a status or byte range and a path. Anything else could hold more."""
    return str(error) if isinstance(error, StorageReadError) else type(error).__name__


def _checkAuthenticatedRead(projectId: str) -> dict:
    try:
        readJson(projectId, "metadata.json")
        return {"authenticatedRead": "ok"}
    except Exception as e:
        return {"authenticatedRead": "failed", "authenticatedReadError": _errorText(e)}


def _checkPagedRead(projectId: str, tableName: str) -> dict:
    try:
        _, totalRows = readTablePage(projectId, tableName, offset=0, limit=1)
        return {"pagedReadRows": totalRows}
    except Exception as e:
        return {"pagedReadRows": None, "pagedReadError": _errorText(e)}


def _probe(url: str, headers: dict | None = None) -> str:
    """Ask for one byte, so a table that is still public isn't downloaded."""
    status = httpx.get(url, headers={**(headers or {}), "Range": "bytes=0-0"}, timeout=20).status_code
    if status in (200, 206):
        return "reachable"
    return "refused" if status in _REFUSED_STATUSES else "unknown"


def _combine(probes: list) -> str:
    return next((level for level in ("reachable", "unknown") if level in probes), "refused")


def _checkPublicRoute(urls: list) -> str:
    return _combine([_probe(url.replace("/storage/v1/object/", "/storage/v1/object/public/", 1)) for url in urls])


def _checkAnonKey(urls: list) -> str:
    key = os.environ.get("SUPABASE_ANON_KEY")
    if not key:
        return "notChecked"
    return _combine([_probe(url, {"apikey": key, "Authorization": f"Bearer {key}"}) for url in urls])


def _checkCacheHeaders(url: str) -> dict:
    headers = httpx.head(url, headers=authHeaders(), timeout=20).headers
    return {"cacheControl": headers.get("cache-control"), "cfCacheStatus": headers.get("cf-cache-status")}


def runPreflight(projectId: str, tableName: str, expectPrivate: bool = False) -> dict:
    """Check authenticated reads, the paged table read and that nothing else can read."""
    urls = [objectUrl(projectId, "metadata.json"), objectUrl(projectId, f"{tableName}.parquet")]
    result = {**_checkAuthenticatedRead(projectId), **_checkPagedRead(projectId, tableName), **_checkCacheHeaders(urls[0])}
    result["publicRoute"] = _checkPublicRoute(urls)
    result["anonKeyRead"] = _checkAnonKey(urls)
    closed = result["publicRoute"] == "refused" and result["anonKeyRead"] == "refused"
    result["ok"] = result["authenticatedRead"] == "ok" and result["pagedReadRows"] is not None and (closed or not expectPrivate)
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
