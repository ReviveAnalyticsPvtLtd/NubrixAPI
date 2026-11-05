"""Fake project storage folder for tests of file lookups in large folders."""

import io
import json
from unittest import mock

from storage3.constants import DEFAULT_SEARCH_OPTIONS

PROJECT_ID = "project_1"
FILE_URL = "https://files.example.test/{projectId}/{fileName}.parquet"
OLDER = "2026-01-01T00:00:00.000Z"
NEWER = "2026-02-01T00:00:00.000Z"
MAX_REMOVE_PATHS = 1000


def earlierTables(count=150):
    """Parquet names that sort before every JSON file a project keeps."""
    return [f"a_table_{index:04}.parquet" for index in range(count)]


class FakeBucket:
    """Answers `list` as storage does: sorted names, 100 by default, `search` as a name prefix."""

    def __init__(self, storedJson=None, fileNames=(), listError=None, modified=None):
        self.storedJson = dict(storedJson or {})
        self.fileNames = sorted({*fileNames, *self.storedJson})
        self.listError = listError
        self.modified = modified or {}
        self.uploads = {}
        self.removeCalls = []

    def list(self, path=None, options=None):
        if self.listError:
            raise self.listError
        settings = {**DEFAULT_SEARCH_OPTIONS, **(options or {})}
        search = settings.get("search", "").lower()
        matches = [name for name in self.fileNames if name.lower().startswith(search)]
        page = matches[settings["offset"]:settings["offset"] + settings["limit"]]
        return [{"name": name, "metadata": {"lastModified": self.modified.get(name, OLDER)}} for name in page]

    def upload(self, path, file, file_options=None):
        self.uploads[path.rsplit("/", 1)[1]] = file

    def remove(self, paths):
        if len(paths) > MAX_REMOVE_PATHS:
            raise ValueError("Storage refuses more than 1000 paths in one remove.")
        self.removeCalls.append(list(paths))

    def uploadedJson(self, fileName):
        return json.loads(self.uploads[fileName])

    def readUrl(self, url):
        """Stands in for urlopen on a FILE_URL address."""
        fileName = url.split("?")[0].rsplit("/", 1)[1]
        return io.BytesIO(json.dumps(self.storedJson[fileName]).encode("utf-8"))

    def readJson(self, projectId, fileName):
        return json.loads(json.dumps(self.storedJson[fileName]))


def fakeClient(bucket):
    client = mock.MagicMock()
    client.storage.from_.return_value = bucket
    return client


def largeFolder(storedJson=None, **kwargs):
    """A folder whose stored JSON files all sit past the first 100 names."""
    return FakeBucket(storedJson, fileNames=earlierTables(), **kwargs)
