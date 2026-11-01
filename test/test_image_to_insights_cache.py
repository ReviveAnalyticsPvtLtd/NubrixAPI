import importlib
import io
import json
import os
import sys
import types
import unittest
from unittest import mock
from urllib.error import URLError


def _install_import_stubs():
    logger_module = types.ModuleType("utils.logger")
    logger_module.logger = types.SimpleNamespace(
        info=lambda *_args, **_kwargs: None,
        warning=lambda *_args, **_kwargs: None,
        error=lambda *_args, **_kwargs: None,
    )
    sys.modules["utils.logger"] = logger_module

    commons_module = types.ModuleType("api.commons")
    commons_module.client = object()
    sys.modules["api.commons"] = commons_module

    # Several sibling test modules replace `requests` with a bare stub at import
    # time, which pytest executes during collection. Drop that stub so the import
    # chain below gets the real package back.
    stubbed_requests = sys.modules.get("requests")
    if stubbed_requests is not None and not hasattr(stubbed_requests, "HTTPError"):
        for name in [key for key in sys.modules if key == "requests" or key.startswith("requests.")]:
            del sys.modules[name]

    celery_module = types.ModuleType("nubrix.triggers.celery")
    celery_module.celeryApp = types.SimpleNamespace(send_task=lambda *_args, **_kwargs: None)
    sys.modules["nubrix.triggers.celery"] = celery_module

    celery_result_module = types.ModuleType("celery.result")
    celery_result_module.AsyncResult = type("AsyncResult", (), {})
    sys.modules["celery.result"] = celery_result_module

    seaborn_module = types.ModuleType("seaborn")
    seaborn_module.load_dataset = lambda *_args, **_kwargs: []
    sys.modules["seaborn"] = seaborn_module

    # UtilityService instantiates a module-level singleton at import time, so the
    # stubs must expose whatever its __init__ reads. ImageToInsights publishes the
    # payload budget knobs the DashboardPayloadBuilder is constructed from.
    component_stubs = (
        ("nubrix.components.insightContextBuilder", "InsightContextBuilder", {}),
        (
            "nubrix.components.imageToInsights",
            "ImageToInsights",
            {"fullRowLimit": 40, "compactRowLimit": 10, "payloadTokenBudget": 12000},
        ),
        ("nubrix.components.signalEngine", "SignalEngine", {}),
        ("nubrix.components.speechToText", "SpeechToText", {}),
        (
            "nubrix.components.dashboardPayloadBuilder",
            "DashboardPayloadBuilder",
            {"__init__": lambda self, **_kwargs: None},
        ),
    )
    for module_name, class_name, attributes in component_stubs:
        module = types.ModuleType(module_name)
        setattr(module, class_name, type(class_name, (), dict(attributes)))
        sys.modules[module_name] = module


_STUB_KEYS = (
    "api.commons",
    "api.services.utilityService",
    "celery.result",
    "nubrix.components.dashboardPayloadBuilder",
    "nubrix.components.imageToInsights",
    "nubrix.components.insightContextBuilder",
    "nubrix.components.signalEngine",
    "nubrix.components.speechToText",
    "nubrix.triggers.celery",
    "seaborn",
    "utils.logger",
)


def _reload_utility_service():
    _install_import_stubs()
    sys.modules.pop("api.services.utilityService", None)
    return importlib.import_module("api.services.utilityService")


def _insights(**marker):
    """
    Schema-shaped insights payload. Only records carrying the documented keys are
    served from cache, so fixtures that stand in for real insights must have them;
    the marker keys just identify the fixture in assertions.
    """
    return {
        "diagnostic_insights": [],
        "prescriptive_actions": [],
        "missing_data": [],
        **marker,
    }


class _FakeContextBuilder:
    def __init__(self):
        self.calls = []

    def buildContext(self, projectId, pageId):
        self.calls.append({"projectId": projectId, "pageId": pageId})
        return {"chartData": [{"ref": "W1", "title": "Revenue"}], "dashboard": {"pageId": pageId}}


class _FakeSignalEngine:
    def __init__(self):
        self.calls = []

    def buildStatisticalSummary(self, chartData):
        self.calls.append(chartData)
        return {"widgetCount": len(chartData), "perWidget": {}}


class _FakePayloadBuilder:
    def __init__(self, payload="## dashboard\npage: Overview"):
        self.payload = payload
        self.calls = []

    def build(self, context):
        self.calls.append(context)
        return self.payload


class _FakeImageToInsights:
    payloadTokenBudget = 12000
    fullRowLimit = 40
    compactRowLimit = 10

    def __init__(self, generated):
        self.generated = generated
        self.calls = []

    def getInsights(self, payloadText, context=None, callbacks=None):
        self.calls.append({"payloadText": payloadText, "context": context})
        return self.generated


class _FakeStorageBucket:
    PAGE_LIMIT = 100

    def __init__(self, fileNames=(), listError=None):
        self.uploads = []
        self.fileNames = sorted(fileNames)
        self.listError = listError

    def from_(self, bucket):
        self.bucket = bucket
        return self

    def list(self, path=None, options=None):
        """Mirrors the storage client: one page of names, narrowed by `search`."""
        if self.listError:
            raise self.listError
        search = (options or {}).get("search", "")
        matches = [name for name in self.fileNames if name.startswith(search)]
        return [{"name": name} for name in matches[:self.PAGE_LIMIT]]

    def upload(self, path, file, file_options=None):
        self.uploads.append({"path": path, "file": file, "file_options": file_options})


class _FakeClient:
    def __init__(self, fileNames=(), listError=None):
        self.storage = _FakeStorageBucket(fileNames, listError)


class DashboardInsightsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._savedModules = {name: sys.modules.get(name) for name in _STUB_KEYS}
        cls.module = _reload_utility_service()
        cls.ImageToInsightsModel = importlib.import_module("api.models").ImageToInsightsModel

    @classmethod
    def tearDownClass(cls):
        for name, module in cls._savedModules.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module

    def _service(self, existing_records, generated=None, payload="## dashboard\npage: Overview"):
        service = object.__new__(self.module.UtilityService)
        service.insightContextBuilder = _FakeContextBuilder()
        service.signalEngine = _FakeSignalEngine()
        service.payloadBuilder = _FakePayloadBuilder(payload)
        service.imageToInsightsModule = _FakeImageToInsights(generated or {"fresh": True})
        service.client = _FakeClient()
        service._loadDashboardInsightsFile = lambda _project_id: list(existing_records)
        return service

    def _payload(self, refresh=False, pageId="page_1"):
        return self.ImageToInsightsModel(
            projectId="project_1",
            pageId=pageId,
            refresh=refresh,
        )

    def _uploaded_records(self, service):
        upload = service.client.storage.uploads[-1]
        self.assertEqual("project_1/dashboardInsights.json", upload["path"])
        self.assertEqual({"upsert": "true"}, upload["file_options"])
        return json.loads(upload["file"].decode("utf-8"))

    def _cache_key(self, service, pageId="page_1"):
        return service._computeCacheKey(pageId, service.payloadBuilder.payload)

    def test_payload_text_is_forwarded_to_the_model(self):
        service = self._service(existing_records=[])

        service.getInsightsFromImage(self._payload())

        self.assertEqual("## dashboard\npage: Overview", service.imageToInsightsModule.calls[0]["payloadText"])

    def test_first_call_generates_and_persists_a_page_scoped_record(self):
        service = self._service(existing_records=[], generated={"fresh": "insight"})

        result = service.getInsightsFromImage(self._payload())

        self.assertEqual({"fresh": "insight"}, result["insights"])
        self.assertEqual("generated", result["source"])
        self.assertFalse(result["cacheHit"])

        records = self._uploaded_records(service)
        self.assertEqual(1, len(records))
        self.assertEqual("page_1", records[0]["pageId"])
        self.assertEqual(2, records[0]["cacheVersion"])
        self.assertEqual(self._cache_key(service), records[0]["cacheKey"])

    def test_matching_cache_key_returns_cached_record_without_model_call(self):
        service = self._service(existing_records=[])
        cached = {
            "id": "insight_1",
            "pageId": "page_1",
            "cacheKey": self._cache_key(service),
            "generatedAt": "2026-06-09T00:00:00+00:00",
            "insights": _insights(cached=True),
        }
        service._loadDashboardInsightsFile = lambda _project_id: [cached]

        result = service.getInsightsFromImage(self._payload())

        self.assertEqual(_insights(cached=True), result["insights"])
        self.assertTrue(result["cacheHit"])
        self.assertEqual("insight_1", result["insightId"])
        self.assertEqual([], service.imageToInsightsModule.calls)
        self.assertEqual([], service.client.storage.uploads)

    def test_changed_dashboard_data_invalidates_the_cache(self):
        service = self._service(existing_records=[])
        stale = {
            "id": "insight_1",
            "pageId": "page_1",
            "cacheKey": service._computeCacheKey("page_1", "## dashboard\npage: Something Else"),
            "insights": _insights(stale=True),
        }
        service._loadDashboardInsightsFile = lambda _project_id: [stale]

        result = service.getInsightsFromImage(self._payload())

        self.assertEqual("generated", result["source"])
        self.assertEqual(1, len(service.imageToInsightsModule.calls))

    def test_other_page_cached_record_is_not_served(self):
        service = self._service(existing_records=[])
        otherPage = {
            "id": "insight_other",
            "pageId": "page_2",
            "cacheKey": service._computeCacheKey("page_2", service.payloadBuilder.payload),
            "insights": _insights(other=True),
        }
        service._loadDashboardInsightsFile = lambda _project_id: [otherPage]

        result = service.getInsightsFromImage(self._payload(pageId="page_1"))

        self.assertEqual("generated", result["source"])

    def test_persisting_one_page_preserves_other_pages_records(self):
        service = self._service(existing_records=[])
        otherPage = {"id": "insight_other", "pageId": "page_2", "cacheKey": "x", "insights": {"other": True}}
        service._loadDashboardInsightsFile = lambda _project_id: [otherPage]

        service.getInsightsFromImage(self._payload(pageId="page_1"))

        records = self._uploaded_records(service)
        self.assertEqual({"page_1", "page_2"}, {record["pageId"] for record in records})

    def test_refresh_bypasses_a_matching_cache_entry(self):
        service = self._service(existing_records=[])
        cached = {
            "id": "insight_1",
            "pageId": "page_1",
            "cacheKey": self._cache_key(service),
            "insights": _insights(cached=True),
        }
        service._loadDashboardInsightsFile = lambda _project_id: [cached]

        result = service.getInsightsFromImage(self._payload(refresh=True))

        self.assertEqual({"fresh": True}, result["insights"])
        self.assertEqual("generated", result["source"])
        records = self._uploaded_records(service)
        self.assertEqual(1, len(records))
        self.assertNotEqual("insight_1", records[0]["id"])

    def test_legacy_records_without_cache_key_are_ignored(self):
        service = self._service(existing_records=[])
        legacy = {
            "id": "old",
            "scope": "project",
            "pageId": None,
            "insights": _insights(old=True),
            "cacheVersion": 1,
        }
        service._loadDashboardInsightsFile = lambda _project_id: [legacy]

        result = service.getInsightsFromImage(self._payload())

        self.assertEqual("generated", result["source"])

    def test_records_with_an_unexpected_insight_shape_are_regenerated(self):
        # Records persisted while the model's content blocks leaked through the
        # parser hold {"type": "text", "text": ...} instead of the real schema.
        # Serving them would keep the broken payload alive until the data changed.
        service = self._service(existing_records=[])
        malformed = {
            "id": "insight_bad",
            "pageId": "page_1",
            "cacheKey": self._cache_key(service),
            "insights": {"type": "text", "text": "{\"diagnostic_insights\": []}", "extras": {}},
        }
        service._loadDashboardInsightsFile = lambda _project_id: [malformed]

        result = service.getInsightsFromImage(self._payload())

        self.assertEqual("generated", result["source"])
        self.assertFalse(result["cacheHit"])
        self.assertEqual(1, len(service.imageToInsightsModule.calls))


class DashboardInsightsReadFailureTests(unittest.TestCase):
    """dashboardInsights.json is rewritten whole, so a failed read must never look like an empty file."""

    STORED = [
        {"id": "other-page", "pageId": "page_2", "status": "accepted", "insights": _insights()},
        {"id": "this-page", "pageId": "page_1", "status": "rejected", "insights": _insights()},
    ]

    @classmethod
    def setUpClass(cls):
        cls._savedModules = {name: sys.modules.get(name) for name in _STUB_KEYS}
        cls.module = _reload_utility_service()
        cls.ImageToInsightsModel = importlib.import_module("api.models").ImageToInsightsModel

    @classmethod
    def tearDownClass(cls):
        for name, module in cls._savedModules.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module

    def setUp(self):
        self.logger = mock.Mock()
        self.urlopen = mock.Mock(side_effect=lambda _url: io.BytesIO(json.dumps(self.STORED).encode("utf-8")))
        for patcher in (
            mock.patch.object(self.module, "logger", self.logger),
            mock.patch.object(self.module, "urlopen", self.urlopen),
            mock.patch.dict(os.environ, {"FILE_URL": "https://files.example.test/{projectId}/{fileName}.parquet"}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _service(self, fileNames=("dashboardInsights.json",), listError=None):
        service = object.__new__(self.module.UtilityService)
        service.insightContextBuilder = _FakeContextBuilder()
        service.signalEngine = _FakeSignalEngine()
        service.payloadBuilder = _FakePayloadBuilder()
        service.imageToInsightsModule = _FakeImageToInsights(_insights(fresh=True))
        service.client = _FakeClient(fileNames, listError)
        return service

    def _payload(self, refresh=False):
        return self.ImageToInsightsModel(projectId="project_1", pageId="page_1", refresh=refresh)

    def _assertReadFailureLogged(self):
        self.logger.error.assert_called()
        self.assertTrue(any("project_1" in str(call) for call in self.logger.error.call_args_list))

    def test_missing_file_is_an_empty_list(self):
        service = self._service(fileNames=("metadata.json",))

        self.assertEqual([], service._loadDashboardInsightsFile("project_1"))
        self.urlopen.assert_not_called()
        self.logger.error.assert_not_called()

    def test_stored_records_are_returned(self):
        self.assertEqual(self.STORED, self._service()._loadDashboardInsightsFile("project_1"))

    def test_file_past_the_first_page_of_a_listing_is_still_found(self):
        earlierFiles = [f"a_table_{index:03}.parquet" for index in range(150)]
        service = self._service(fileNames=[*earlierFiles, "dashboardInsights.json"])

        self.assertEqual(self.STORED, service._loadDashboardInsightsFile("project_1"))

    def test_listing_failure_raises_and_is_logged(self):
        service = self._service(listError=ConnectionError("storage unavailable"))

        with self.assertRaises(ConnectionError):
            service._loadDashboardInsightsFile("project_1")

        self._assertReadFailureLogged()

    def test_download_failure_raises_and_is_logged(self):
        self.urlopen.side_effect = URLError("storage unavailable")

        with self.assertRaises(URLError):
            self._service()._loadDashboardInsightsFile("project_1")

        self._assertReadFailureLogged()

    def test_unreadable_contents_raise_and_are_logged(self):
        self.urlopen.side_effect = lambda _url: io.BytesIO(b"{not json")

        with self.assertRaises(ValueError):
            self._service()._loadDashboardInsightsFile("project_1")

        self._assertReadFailureLogged()

    def test_contents_that_are_not_a_list_raise_and_are_logged(self):
        self.urlopen.side_effect = lambda _url: io.BytesIO(b'{"id": "not-a-list"}')

        with self.assertRaises(ValueError):
            self._service()._loadDashboardInsightsFile("project_1")

        self._assertReadFailureLogged()

    def test_get_dashboard_insights_raises_on_a_failed_read(self):
        self.urlopen.side_effect = URLError("storage unavailable")

        with self.assertRaises(self.module.CustomException) as caught:
            self._service().getDashboardInsights("project_1")

        self.assertEqual(500, caught.exception.statusCode)

    def test_status_update_reports_the_read_failure_and_writes_nothing(self):
        self.urlopen.side_effect = URLError("storage unavailable")
        service = self._service()

        with self.assertRaises(self.module.CustomException) as caught:
            service.updateDashboardInsightStatus("project_1", "this-page", "accepted")

        self.assertIn("storage unavailable", str(caught.exception))
        self.assertNotIn("not found", str(caught.exception))
        self.assertEqual([], service.client.storage.uploads)

    def test_failed_read_stops_a_generation_before_the_model_runs(self):
        for refresh in (False, True):
            with self.subTest(refresh=refresh):
                self.urlopen.side_effect = URLError("storage unavailable")
                service = self._service()

                with self.assertRaises(self.module.CustomException):
                    service.getInsightsFromImage(self._payload(refresh=refresh))

                self.assertEqual([], service.imageToInsightsModule.calls)
                self.assertEqual([], service.client.storage.uploads)

    def test_upsert_never_writes_after_a_failed_read(self):
        self.urlopen.side_effect = URLError("storage unavailable")
        service = self._service()

        with self.assertRaises(URLError):
            service._persistDashboardInsight("project_1", "page_1", _insights(), "key")

        self.assertEqual([], service.client.storage.uploads)

    def test_read_failing_after_the_model_ran_still_writes_nothing(self):
        stored = io.BytesIO(json.dumps(self.STORED).encode("utf-8"))
        self.urlopen.side_effect = [stored, URLError("storage unavailable")]
        service = self._service()

        with self.assertRaises(self.module.CustomException):
            service.getInsightsFromImage(self._payload(refresh=True))

        self.assertEqual(1, len(service.imageToInsightsModule.calls))
        self.assertEqual([], service.client.storage.uploads)

    def test_upsert_keeps_other_pages_after_a_good_read(self):
        service = self._service()

        service._persistDashboardInsight("project_1", "page_1", _insights(), "key")

        written = json.loads(service.client.storage.uploads[-1]["file"].decode("utf-8"))
        self.assertEqual(2, len(written))
        self.assertEqual(self.STORED[0], written[0])


if __name__ == "__main__":
    unittest.main()
