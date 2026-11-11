"""The metadata the chart model sees: one function shared by the reporting service and the evaluation harness."""

import copy
from unittest import mock

import pytest

SAMPLE = {
    "orders": {"shape": [36, 2], "columns": [{"name": "a", "type": "int64"}]},
    "legacy": {"shape": [5, 1], "isActive": False, "columns": [{"name": "b"}]},
    "kept": {"shape": [7, 1], "isActive": True, "columns": [{"name": "c"}]},
    "_meta": {"note": "not a table"},
}


def _classify(projectId, tableName):
    return {"rows_estimate": 36 if tableName == "orders" else 99, "size_class": "small", "hint": f"hint {tableName}"}


def _oldInline(metadata, projectId):
    """The preparation as `ReportingService` did it before it moved: size hints in place, then the active filter."""
    from api.services.managementService import managementService

    for table_name, entry in (metadata or {}).items():
        if not isinstance(entry, dict) or table_name.startswith("_"):
            continue
        cls = _classify(projectId, table_name)
        entry["size_class"] = cls.get("size_class", "unknown")
        entry["size_hint"] = cls.get("hint", "")
        existing_rows = cls.get("rows_estimate")
        if existing_rows is not None and "shape" in entry and isinstance(entry["shape"], list):
            entry["_size_check"] = (existing_rows == entry["shape"][0])
    return managementService.filterActiveTables(metadata)


def test_metadataForModelEqualsWhatTheServiceBuiltInline():
    from utils import modelMetadata

    old = _oldInline(copy.deepcopy(SAMPLE), "p1")
    with mock.patch("utils.initMethods.classify_table_size", _classify):
        new = modelMetadata.metadataForModel(copy.deepcopy(SAMPLE), "p1")
    assert new == old
    assert set(new) == {"orders", "kept"}
    assert new["orders"]["size_class"] == "small" and new["orders"]["_size_check"] is True
    assert new["kept"]["_size_check"] is False


def test_metadataForModelDropsInactiveTablesAndUnderscoreKeys():
    from utils import modelMetadata

    with mock.patch("utils.initMethods.classify_table_size", _classify):
        result = modelMetadata.metadataForModel(copy.deepcopy(SAMPLE), "p1")
    assert "legacy" not in result and "_meta" not in result


def test_theServiceAndTheManagementFilterUseTheSharedFunctions():
    from api.services.managementService import managementService
    from api.services.reportingService import ReportingService
    from utils import modelMetadata

    assert managementService.filterActiveTables(SAMPLE) == modelMetadata.activeTables(SAMPLE)
    with mock.patch("utils.initMethods.classify_table_size", _classify):
        viaService = ReportingService._augment_with_size_hints(copy.deepcopy(SAMPLE), "p1")
        viaUtil = modelMetadata.augmentWithSizeHints(copy.deepcopy(SAMPLE), "p1")
    assert viaService == viaUtil
    assert modelMetadata.activeTables("nope") == {}


def test_augmentWorksInPlaceLikeTheServiceCacheNeeds():
    from utils import modelMetadata

    metadata = copy.deepcopy(SAMPLE)
    with mock.patch("utils.initMethods.classify_table_size", _classify):
        assert modelMetadata.augmentWithSizeHints(metadata, "p1") is metadata
    assert "size_class" in metadata["orders"] and "size_class" not in metadata["_meta"]
