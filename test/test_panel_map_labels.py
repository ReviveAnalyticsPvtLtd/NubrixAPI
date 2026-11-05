"""Verify the hover label contract for Reporting panel scatter maps."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pandas as pd
import polars as pl
import pytest

from api.models import PanelChartDetails
from api.services.reportingService import ReportingService
from nubrix.utils import readYaml
from test.storageListingFixtures import FILE_URL, PROJECT_ID, fakeClient, largeFolder
from utils.codeExecutor import _remove_code_fences
from utils.initMethods import serializer


@pytest.fixture
def table_loader(monkeypatch):
    tables = {}

    def fetch_data(project_id, table_name):
        assert project_id == PROJECT_ID
        return tables[table_name]

    monkeypatch.setattr("api.services.reportingService.fetch_data", fetch_data)
    monkeypatch.setattr("utils.initMethods.classify_table_size", lambda *_: {"size_class": "small"})
    return tables, fetch_data


@pytest.fixture
def geocode(monkeypatch):
    locations = {
        "Pune": SimpleNamespace(latitude=18.52, longitude=73.86),
        "00123": SimpleNamespace(latitude=41.9, longitude=12.5),
    }
    lookup = Mock(side_effect=locations.get)
    monkeypatch.setattr("geopy.geocoders.Nominatim.geocode", lookup)
    monkeypatch.setattr("geopy.extra.rate_limiter.RateLimiter", lambda func, **_: func)
    return lookup


def panel_map(**kwargs):
    return ReportingService._generatePanelChart(
        projectId=PROJECT_ID,
        chartType="geoMap",
        xAxis=kwargs.pop("xAxis", "latitude"),
        yAxis=kwargs.pop("yAxis", "longitude"),
        aggregationMetric=None,
        dataSourceName="locations",
        tablesUsed="locations",
        **kwargs,
    )


def point_values(result):
    assert result["chartType"] == "geoMap"
    assert result["map"]["mapType"] == "scatterMap"
    values = []
    for point in result["map"]["data"]["points"]:
        assert set(point) == {"id", "lat", "long", "label"}
        assert isinstance(point["id"], str) and point["id"]
        assert isinstance(point["label"], str) and point["label"]
        values.append((point["lat"], point["long"], point["label"]))
    return values


def test_coordinate_points_label_the_filtered_row(table_loader):
    tables, _ = table_loader
    tables["locations"] = pd.DataFrame({
        "latitude": [19.076, 18.52, None],
        "longitude": [72.8777, 73.86, 72.8],
        "region": ["west", "east", "west"],
        "label": ["  Mumbai warehouse  ", "Filtered warehouse", "Missing latitude"],
    })

    assert point_values(panel_map(filters=[{"locations.region": "west"}])) == [
        (19.076, 72.8777, "Mumbai warehouse"),
    ]
    assert point_values(panel_map(filters=[{"locations.region": "south"}])) == []


@pytest.mark.parametrize(
    ("label_column", "expected"),
    [
        ({}, "19.076, 72.8777"),
        ({"label": [None]}, "19.076, 72.8777"),
        ({"label": [pd.NA]}, "19.076, 72.8777"),
        ({"label": [float("nan")]}, "19.076, 72.8777"),
        ({"label": ["  "]}, "19.076, 72.8777"),
        ({"label": [["nested", "text"]]}, "19.076, 72.8777"),
        ({"label": [0]}, "0"),
        ({"label": [0.0]}, "0"),
        ({"label": [False]}, "False"),
        ({"label": ["0.0"]}, "0.0"),
    ],
)
def test_coordinate_points_handle_missing_labels(table_loader, label_column, expected):
    tables, _ = table_loader
    tables["locations"] = pd.DataFrame({"latitude": [19.076], "longitude": [72.8777], **label_column})

    assert point_values(panel_map()) == [(19.076, 72.8777, expected)]


@pytest.mark.parametrize("size_class", ["small", "large", "massive"])
def test_geocoded_points_use_the_source_value(table_loader, geocode, monkeypatch, size_class):
    tables, fetch_data = table_loader
    tables["locations"] = pd.DataFrame({
        "place": ["Pune", "00123", "Pune", "Unresolved"],
        "label": [None, "", "  Pune warehouse  ", "Missing coordinates"],
    })
    monkeypatch.setattr("utils.initMethods.classify_table_size", lambda *_: {"size_class": size_class})
    monkeypatch.setattr(
        "api.services.reportingService.scan_data",
        lambda project_id, table_name: pl.from_pandas(fetch_data(project_id, table_name)).lazy(),
    )

    assert point_values(panel_map(xAxis=None, yAxis=None, geoCodeColumn="place")) == [
        (18.52, 73.86, "Pune"),
        (41.9, 12.5, "00123"),
        (18.52, 73.86, "Pune warehouse"),
    ]
    assert [call.args for call in geocode.call_args_list] == [("Pune",), ("00123",), ("Unresolved",)]


@pytest.mark.parametrize(
    ("data_source", "custom_label", "first_label"),
    [
        ("locations", "Direct warehouse", "Direct warehouse"),
        ("combined", "Joined warehouse", "Joined warehouse"),
        ("combined", 0, "0"),
    ],
    ids=["direct", "blend-text", "blend-zero"],
)
@pytest.mark.parametrize("location_mode", ["coordinates", "geocoding"])
def test_generated_panel_map_code_matches_response(
    table_loader, geocode, monkeypatch, capsys, data_source, custom_label, first_label, location_mode,
):
    tables, fetch_data = table_loader
    locations = pd.DataFrame({
        "row_id": [1, 2],
        "latitude": [19.076, 18.52],
        "longitude": [72.8777, 73.86],
        "place": ["00123", "Pune"],
    })
    if data_source == "combined":
        labels = pd.DataFrame({"row_id": [1], "label": [custom_label]})
        tables.update(locations=locations, labels=labels)
        bucket = largeFolder({"blendConfig.json": {
            "combined": {
                "tables": ["locations", "labels"],
                "blendOn": ["row_id"],
                "joinTypes": ["left"],
            },
        }})
    else:
        tables["locations"] = locations.assign(label=[custom_label, None])
        bucket = largeFolder({"locations.parquet": {}})

    service = object.__new__(ReportingService)
    service.client = fakeClient(bucket)
    service.codeTemplates = readYaml(str(Path(__file__).resolve().parents[1] / "codeTemplates.yaml"))

    async def fetch_json(url):
        return json.loads(bucket.readUrl(url).read())

    service._fetchJson = fetch_json
    monkeypatch.setenv("FILE_URL", FILE_URL)
    monkeypatch.setattr("api.services.reportingService.updateProjectModifiedAt", lambda *_: None)

    if location_mode == "geocoding":
        options = {"zipCodeColumn": "place"}
        expected = [(41.9, 12.5, first_label), (18.52, 73.86, "Pune")]
    else:
        options = {"xAxis": "latitude", "yAxis": "longitude"}
        expected = [(19.076, 72.8777, first_label), (18.52, 73.86, "18.52, 73.86")]
    details = PanelChartDetails(projectId=PROJECT_ID, chartType="geoMap", dataSource=data_source, **options)
    response = asyncio.run(service.generatePanelChart(details))
    capsys.readouterr()
    exec(_remove_code_fences(response["generatedCode"]), {"fetch_data": fetch_data, "serializer": serializer})
    rendered = json.loads(capsys.readouterr().out)

    assert point_values(response) == point_values(rendered) == expected
