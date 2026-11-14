"""Regression tests for hover labels on prompt-generated and saved maps."""

import asyncio
import json
from unittest.mock import AsyncMock, Mock

import pytest

from api.models import GenerateChartsInParallel, GetData
from api.services.dashboardService import DashboardService
from api.services.reportingService import ReportingService
from nubrix.workflows.parallelReportingToolWorkflow import ParallelReportingToolWorkflow
from nubrix.workflows.reportingToolWorkflow import ReportingToolWorkflow
from test.storageListingFixtures import PROJECT_ID, fakeClient, largeFolder


def scatter_map(points):
    return {"chartType": "geoMap", "map": {"mapType": "scatterMap", "data": {"points": points}}}


def points(chart):
    return chart["map"]["data"]["points"]


def format_output(workflow, chart):
    formatter = object.__new__(workflow)
    return formatter._formatJsonResponse({"codeOutput": json.dumps(chart)})["finalOutput"]


@pytest.mark.parametrize("workflow", [ReportingToolWorkflow, ParallelReportingToolWorkflow])
def test_prompt_map_always_has_a_point_label(workflow):
    original = [
        {"id": "21550", "lat": -22.8579, "long": -43.3526},
        {"id": "a", "lat": 1, "long": 2, "label": "   Revenue: 42  ", "properties": {"city": "Rio"}},
        {"id": "b", "lat": 3, "long": 4, "label": None, "properties": {"city": "Rio Branco"}},
        {"id": "c", "lat": 5, "long": 6, "label": {"invalid": "value"}},
        {"id": "d", "lat": 7, "long": 8, "label": 0.0},
        {"id": "e", "lat": 9, "long": 10, "label": "  "},
    ]
    expected_labels = ["-22.8579, -43.3526", "Revenue: 42", "Rio Branco", "5, 6", "0", "9, 10"]

    formatted = format_output(workflow, scatter_map(original))

    assert points(formatted) == [
        {**point, "label": label} for point, label in zip(original, expected_labels, strict=True)
    ]
    assert format_output(workflow, formatted) == formatted


@pytest.mark.parametrize("workflow", [ReportingToolWorkflow, ParallelReportingToolWorkflow])
def test_formatter_preserves_other_output_shapes(workflow):
    for value in (
        {"chartType": "card", "label": "Revenue", "data": 5},
        {"chartType": "geoMap", "map": None},
        scatter_map([]),
        [{"chartType": "geoMap"}],
    ):
        assert format_output(workflow, value) == value


def test_dashboard_adds_labels_to_legacy_map_on_read_without_writing(monkeypatch):
    saved = {"main": {"name": "Main", "widgets": [
        {"id": "map", **scatter_map([{"id": "21550", "lat": -22.8579, "long": -43.3526}])},
        {"id": "card", "chartType": "card", "label": "Revenue", "data": 5},
    ]}}
    storage = Mock()
    service = object.__new__(DashboardService)
    service.client = storage
    monkeypatch.setattr("api.services.dashboardService.readJson", lambda *_: json.loads(json.dumps(saved)))

    page = service.getData(GetData(projectId=PROJECT_ID, page="main"))

    assert points(page["widgets"][0]) == [
        {"id": "21550", "lat": -22.8579, "long": -43.3526, "label": "-22.8579, -43.3526"}
    ]
    assert page["widgets"][1] == saved["main"]["widgets"][1]
    assert "label" not in points(saved["main"]["widgets"][0])[0]
    assert storage.mock_calls == []


def test_parallel_reporting_saves_the_labeled_map(monkeypatch):
    bucket = largeFolder({
        "dashboardConfig.json": {"main": {"name": "Main", "isAutomatic": True, "widgets": []}},
        "insights.json": {"insights": []},
    })
    service = object.__new__(ReportingService)
    service.client = fakeClient(bucket)
    service._getProjectMetadata = AsyncMock(return_value={})
    monkeypatch.setattr("api.services.reportingService.initParallelWorkflow", lambda mode=None: None)
    monkeypatch.setattr("api.services.reportingService.updateProjectModifiedAt", lambda *_: None)

    async def fetch_json(url):
        return json.loads(bucket.readUrl(url).read())

    service._fetchJson = fetch_json
    chart = scatter_map([{"id": "a", "lat": 1, "long": 2, "label": "Warehouse"}])
    service._generateSingleChartForParallel = Mock(return_value={"finalOutput": chart, "generatedCode": "code"})
    details = GenerateChartsInParallel(projectId=PROJECT_ID, inputQueries=["Map the warehouse"])

    page = asyncio.run(service.generateChartsInParallel(details))

    assert page["widgets"][0]["map"] == chart["map"]
    assert bucket.uploadedJson("dashboardConfig.json")["main"]["widgets"][0]["map"] == chart["map"]
