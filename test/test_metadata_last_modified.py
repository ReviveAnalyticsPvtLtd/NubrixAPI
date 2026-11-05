"""Tests for how metadata regeneration reads storage timestamps."""

import io
import json
import os
from unittest import mock

import pytest

from api.services.managementService import ManagementService


def _regenerate(metadataTime, oldTime, newTime):
    service = object.__new__(ManagementService)
    service.client = mock.Mock()
    service.client.storage.from_.return_value.list.return_value = [
        {"name": "metadata.json", "metadata": {"lastModified": metadataTime}},
        {"name": "old.parquet", "metadata": {"lastModified": oldTime}},
        {"name": "new.parquet", "metadata": {"lastModified": newTime}},
    ]
    stored = {"old": {"isActive": False}}
    with (
        mock.patch.dict(os.environ, {"FILE_URL": "https://files.example.test/{projectId}/{fileName}.parquet"}),
        mock.patch("api.services.managementService.readJson", return_value=stored),
        mock.patch("api.services.managementService.updateProjectModifiedAt"),
        mock.patch.object(service, "_generateMetadata", return_value={"new": {"shape": [1, 1]}}) as generate,
    ):
        result = service.generateMetadata("project_1")
    return generate, result


@pytest.mark.parametrize("metadataTime, oldTime, newTime", [
    ("2026-11-03T10:00:00.123Z", "2026-11-03T09:59:59.999Z", "2026-11-03T10:00:00.456Z"),
    ("2026-11-03T10:00:00.000Z", "2026-11-03T09:00:00.000Z", "2026-11-03T11:00:00.000Z"),
    ("2026-11-03T10:00:00Z", "2026-11-03T09:00:00Z", "2026-11-03T10:00:01Z"),
], ids=["milliseconds", "wholeSeconds", "noFraction"])
def test_onlyTablesChangedSinceTheMetadataAreRegenerated(metadataTime, oldTime, newTime):
    generate, result = _regenerate(metadataTime, oldTime, newTime)

    generate.assert_called_once_with(projectId="project_1", userId=None, tableNames=["new"])
    assert result["old"] == {"isActive": False}
    assert result["new"]["isActive"] is True
