import io
import json
import os
import sys
import types
import unittest
from unittest import mock
from urllib.error import URLError


def _importContextBuilder():
    """
    Imports InsightContextBuilder behind the minimum stubs it needs.

    `utils.logger` is stubbed for the session because its logtail dependency is
    not installed. `api.commons` needs live Supabase credentials, so it is
    stubbed only for the duration of this import and then restored -- leaving it
    in place would hide names (verifyUser, verifyToken) that later test modules
    import from the real module. `utils.exceptionHandler` imports cleanly and is
    deliberately left alone for the same reason.
    """
    loggerModule = types.ModuleType("utils.logger")
    loggerModule.logger = types.SimpleNamespace(
        info=lambda *_args, **_kwargs: None,
        warning=lambda *_args, **_kwargs: None,
        error=lambda *_args, **_kwargs: None,
    )
    sys.modules["utils.logger"] = loggerModule

    savedCommons = sys.modules.get("api.commons")
    commonsModule = types.ModuleType("api.commons")
    commonsModule.client = object()
    sys.modules["api.commons"] = commonsModule
    try:
        from nubrix.components.insightContextBuilder import InsightContextBuilder

        return InsightContextBuilder
    finally:
        if savedCommons is None:
            sys.modules.pop("api.commons", None)
        else:
            sys.modules["api.commons"] = savedCommons


InsightContextBuilder = _importContextBuilder()

DASHBOARD_CONFIG = {
    "page_1": {
        "name": "Overview",
        "widgets": [
            {
                "id": "widget-a",
                "title": "Revenue by Region",
                "chartType": "bar",
                "data": {"labels": ["East"], "datasets": [{"label": "revenue", "data": [1]}]},
                "xLabels": "region",
                "yLabels": "revenue",
                "label": None,
                "map": None,
                "generatedCode": "panelChart(xAxis = 'region')",
            }
        ],
    },
    "page_2": {
        "name": "Detail",
        "widgets": [{"id": "widget-b", "title": "Tickets", "chartType": "table", "data": []}],
    },
}


class ExtractChartDataTests(unittest.TestCase):
    def setUp(self):
        self.builder = object.__new__(InsightContextBuilder)

    def test_widget_carries_generated_code_and_id(self):
        chartData = self.builder._extractChartData(DASHBOARD_CONFIG, "page_1")

        self.assertEqual("widget-a", chartData[0]["id"])
        self.assertEqual("panelChart(xAxis = 'region')", chartData[0]["generatedCode"])

    def test_widgets_receive_sequential_references(self):
        chartData = self.builder._extractChartData(DASHBOARD_CONFIG, "page_1")

        self.assertEqual(["W1"], [widget["ref"] for widget in chartData])

    def test_widget_records_its_page(self):
        chartData = self.builder._extractChartData(DASHBOARD_CONFIG, "page_1")

        self.assertEqual("page_1", chartData[0]["pageId"])

    def test_page_filter_limits_widgets_to_that_page(self):
        chartData = self.builder._extractChartData(DASHBOARD_CONFIG, "page_2")

        self.assertEqual(1, len(chartData))
        self.assertEqual("widget-b", chartData[0]["id"])

    def test_unknown_page_yields_no_widgets(self):
        self.assertEqual([], self.builder._extractChartData(DASHBOARD_CONFIG, "missing"))

    def test_empty_config_yields_no_widgets(self):
        self.assertEqual([], self.builder._extractChartData({}, "page_1"))


class _FakeBucket:
    PAGE_LIMIT = 100

    def __init__(self, fileNames):
        self.fileNames = sorted(fileNames)

    def list(self, path=None, options=None):
        search = (options or {}).get("search", "")
        matches = [name for name in self.fileNames if name.startswith(search)]
        return [{"name": name} for name in matches[:self.PAGE_LIMIT]]


class _FakeStorage:
    def __init__(self, fileNames):
        self.bucket = _FakeBucket(fileNames)

    def from_(self, _bucketName):
        return self.bucket


class LoadJsonFromStorageTests(unittest.TestCase):
    STORED = {"sales": {"isActive": True}}

    def setUp(self):
        self.builder = object.__new__(InsightContextBuilder)
        self.builder.config = types.SimpleNamespace(storageBucket="AnalyticsHub", domainBucket="DomainSpecificKpis")
        self.builder.client = types.SimpleNamespace(storage=_FakeStorage(["metadata.json", "dashboardConfig.json"]))
        self.builder._loadDomainContext = lambda _projectId: {}
        self.logger = mock.Mock()
        self.urlopen = mock.Mock(side_effect=lambda _url: io.BytesIO(json.dumps(self.STORED).encode("utf-8")))
        # Patched through the function's own globals: sibling test modules swap
        # this module's entry in sys.modules while the suite runs.
        builderGlobals = InsightContextBuilder._loadJsonFromStorage.__globals__
        for patcher in (
            mock.patch.dict(builderGlobals, {"logger": self.logger, "urlopen": self.urlopen}),
            mock.patch.dict(os.environ, {"FILE_URL": "https://files.example.test/{projectId}/{fileName}.parquet"}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_missing_file_is_none(self):
        self.assertIsNone(self.builder._loadJsonFromStorage("project_1", "insights.json"))
        self.urlopen.assert_not_called()

    def test_stored_file_is_returned(self):
        self.assertEqual(self.STORED, self.builder._loadJsonFromStorage("project_1", "metadata.json"))

    def test_file_past_the_first_page_of_a_listing_is_still_found(self):
        earlierFiles = [f"a_table_{index:03}.parquet" for index in range(150)]
        self.builder.client = types.SimpleNamespace(storage=_FakeStorage([*earlierFiles, "metadata.json"]))

        self.assertEqual(self.STORED, self.builder._loadJsonFromStorage("project_1", "metadata.json"))

    def test_failed_read_raises_and_is_logged(self):
        self.urlopen.side_effect = URLError("storage unavailable")

        with self.assertRaises(URLError):
            self.builder._loadJsonFromStorage("project_1", "metadata.json")

        self.logger.error.assert_called_once()
        self.assertIn("project_1", str(self.logger.error.call_args))

    def test_failed_read_stops_the_context_instead_of_emptying_it(self):
        for fileName in ("metadata.json", "dashboardConfig.json"):
            with self.subTest(fileName=fileName):
                def failOnlyThisFile(url, failing=fileName):
                    if failing in url:
                        raise URLError("storage unavailable")
                    return io.BytesIO(b"{}")

                self.urlopen.side_effect = failOnlyThisFile

                with self.assertRaises(Exception) as caught:
                    self.builder.buildContext("project_1", "page_1")

                self.assertIn("storage unavailable", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
