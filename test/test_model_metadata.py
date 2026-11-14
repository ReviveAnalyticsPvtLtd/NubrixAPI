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


# The harness can hand the metadata it already read to the size classification (a warm served request gets it from Redis)

PROJECT_ID = "p1"
KNOWN = {"orders": {"shape": [36, 2], "columns": []}, "big": {"shape": [5_000_000, 2], "columns": []}}


class _FakeRedis:
    """Stands in for `redis.Redis`: answers GET `<project>::metadata` from `answer` and records every command."""

    answer = None
    commands = []

    def __init__(self, *args, **kwargs):
        pass

    def get(self, key):
        type(self).commands.append(("get", key))
        if type(self).answer is None:
            raise ConnectionError("no Redis")
        return type(self).answer


@pytest.fixture
def fakeRedis(monkeypatch):
    from utils import initMethods

    _FakeRedis.answer, _FakeRedis.commands = None, []
    monkeypatch.setattr(initMethods.redis, "Redis", _FakeRedis)
    monkeypatch.setattr(initMethods, "_redis_pool", lambda: None)
    monkeypatch.setattr(initMethods, "_DF_SHAPES", {})
    monkeypatch.setattr(initMethods, "_cache_get", lambda key: None)
    return _FakeRedis


def test_aServedCallStillReadsRedisAsBefore(fakeRedis):
    import orjson

    from utils.initMethods import classify_table_size

    fakeRedis.answer = orjson.dumps(KNOWN)
    assert classify_table_size(PROJECT_ID, "orders")["size_class"] == "small"
    assert classify_table_size(PROJECT_ID, "big")["size_class"] in ("large", "massive")
    assert fakeRedis.commands == [("get", "p1::metadata")] * 2
    fakeRedis.answer = None
    assert classify_table_size(PROJECT_ID, "orders")["size_class"] == "unknown"


def test_aServedAugmentCallPassesNothingExtraToTheClassification():
    from utils import modelMetadata

    calls = []

    def spy(*args, **kwargs):
        calls.append((args, kwargs))
        return {"rows_estimate": None, "size_class": "unknown", "hint": ""}

    with mock.patch("utils.initMethods.classify_table_size", spy):
        modelMetadata.augmentWithSizeHints(copy.deepcopy(SAMPLE), "p1")
        modelMetadata.metadataForModel(copy.deepcopy(SAMPLE), "p1")
    assert calls and all(kwargs == {} and len(args) == 2 for args, kwargs in calls)


def test_knownMetadataGivesTheWarmHintsWithoutAskingRedis(fakeRedis):
    from utils import modelMetadata

    fakeRedis.answer = None  # a Redis that is down: the cold-start value
    cold = modelMetadata.metadataForModel(copy.deepcopy(KNOWN), PROJECT_ID)
    assert cold["orders"]["size_class"] == "unknown" and "_size_check" not in cold["orders"]
    fakeRedis.commands.clear()
    warm = modelMetadata.metadataForModel(copy.deepcopy(KNOWN), PROJECT_ID, knownMetadata=KNOWN)
    assert fakeRedis.commands == []
    assert warm["orders"]["size_class"] == "small" and warm["orders"]["_size_check"] is True
    assert warm["big"]["size_class"] in ("large", "massive")
    # What a warm served request gives for the same metadata.
    import orjson

    fakeRedis.answer = orjson.dumps(KNOWN)
    served = modelMetadata.metadataForModel(copy.deepcopy(KNOWN), PROJECT_ID)
    assert served == warm


def test_knownMetadataWithoutTheTableFallsBackLikeAServedCall(fakeRedis):
    from utils.initMethods import classify_table_size

    assert classify_table_size(PROJECT_ID, "missing", knownMetadata=KNOWN)["size_class"] == "unknown"
