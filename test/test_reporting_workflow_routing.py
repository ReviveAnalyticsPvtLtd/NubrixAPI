"""The Python reporting workflow's rewrite of eager reads of large tables into lazy scans, and the `knownMetadata` a
caller with the metadata in hand (the evaluation harness) can supply for it. A served request never supplies it."""

from unittest import mock

import pytest

from nubrix.workflows.reportingToolWorkflow import ReportingToolWorkflow, State

PID = "p1"
CODE = 'df = fetch_data_pl("p1", "big")\nsmall = fetch_data_pl("p1", "tiny")\n'
KNOWN = {"big": {"shape": [1_500_000, 3], "columns": []}, "tiny": {"shape": [10, 1], "columns": []}}


@pytest.fixture
def noRedis(monkeypatch):
    """Redis is down and nothing is cached in the process: the cold-start answer of `classify_table_size`."""
    from utils import initMethods

    def refuse(*args, **kwargs):
        raise ConnectionError("no Redis")

    monkeypatch.setattr(initMethods.redis, "Redis", refuse)
    monkeypatch.setattr(initMethods, "_redis_pool", lambda: None)
    monkeypatch.setattr(initMethods, "_DF_SHAPES", {})
    monkeypatch.setattr(initMethods, "_cache_get", lambda key: None)


def test_knownMetadataIsAnOptionalStateKey():
    assert "knownMetadata" in State.__optional_keys__


def test_aServedCallAsksForTheSizeWithTwoArguments():
    calls = []

    def spy(*args, **kwargs):
        calls.append((args, kwargs))
        return {"rows_estimate": None}

    with mock.patch("utils.initMethods.classify_table_size", spy):
        assert ReportingToolWorkflow._route_large_tables_to_scan(CODE, PID) == CODE
    assert calls == [((PID, "big"), {}), ((PID, "tiny"), {})]


def _workflow(response):
    workflow = ReportingToolWorkflow.__new__(ReportingToolWorkflow)
    chain = mock.Mock()
    chain.invoke.return_value = response
    workflow.codeGeneratorChain = chain
    workflow.codeDebuggerChain = chain
    return workflow


STATE = {"projectId": PID, "inputQuery": "q", "metadata": {}, "rephrasedQuery": "r", "generatedCode": "x", "codeOutput": "err"}


@pytest.mark.parametrize("node", ["_generateCode", "_codeDebugger"])
def test_theNodesPassTheStatesKnownMetadataAndAServedStateCarriesNone(node):
    calls = []

    def spy(*args, **kwargs):
        calls.append((args, kwargs))
        return {"rows_estimate": None}

    workflow = _workflow(CODE)
    with mock.patch("utils.initMethods.classify_table_size", spy):
        getattr(workflow, node)(dict(STATE))
        served = list(calls)
        calls.clear()
        getattr(workflow, node)({**STATE, "knownMetadata": KNOWN})
    assert served == [((PID, "big"), {}), ((PID, "tiny"), {})]
    assert calls == [((PID, "big"), {"knownMetadata": KNOWN}), ((PID, "tiny"), {"knownMetadata": KNOWN})]


@pytest.mark.parametrize("node", ["_generateCode", "_codeDebugger"])
def test_aLargeTableInKnownMetadataIsScannedLazily(noRedis, node):
    result = getattr(_workflow(CODE), node)({**STATE, "knownMetadata": KNOWN})
    assert 'scan_data("p1", "big"' in result["generatedCode"]
    assert 'fetch_data_pl("p1", "tiny"' in result["generatedCode"]


def test_withoutKnownMetadataAndWithRedisDownNothingIsRewritten(noRedis):
    assert ReportingToolWorkflow._route_large_tables_to_scan(CODE, PID) == CODE
    result = _workflow(CODE)._generateCode(dict(STATE))
    assert "scan_data" not in result["generatedCode"]
