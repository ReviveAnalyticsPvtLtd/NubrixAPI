"""
storageListing.py

Complete listings of project folders in the AnalyticsHub bucket.
"""

__all__ = ["listProjectFiles"]


_BUCKET = "AnalyticsHub"
_PAGE_SIZE = 1000


def listProjectFiles(client, projectId: str) -> list[dict]:
    """
    List every entry in a project folder.

    The storage client stops at 100 names by default. To check for one named
    file, pass `options={"search": fileName}` to `list` instead of using this.
    """
    bucket = client.storage.from_(_BUCKET)
    files: list[dict] = []
    while True:
        page = bucket.list(
            path=projectId,
            options={"limit": _PAGE_SIZE, "offset": len(files), "sortBy": {"column": "name", "order": "asc"}},
        )
        files.extend(page)
        if len(page) < _PAGE_SIZE:
            return files
