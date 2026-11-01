"""Read-only rollout check for the private AnalyticsHub bucket."""

import io
import json

import polars as pl
import pytest

import scripts.privateBucketPreflight as preflight
from test.privateStorageFixtures import SERVICE_KEY, FakePrivateStorage

PROJECT = "11111111-2222-4333-8444-555555555555"
ANON_KEY = "test-anon-key"


@pytest.fixture
def storage(monkeypatch):
    fake = FakePrivateStorage()
    buffer = io.BytesIO()
    pl.DataFrame({"amount": [1, 2, 3]}).write_parquet(buffer)
    fake.put(PROJECT, "sales.parquet", buffer.getvalue())
    fake.putJson(PROJECT, "metadata.json", {"sales": {"description": "customer secret"}})
    monkeypatch.setenv("SUPABASE_URL", fake.url)
    monkeypatch.setenv("SUPABASE_KEY", SERVICE_KEY)
    monkeypatch.setenv("SUPABASE_ANON_KEY", ANON_KEY)
    yield fake
    fake.close()


def _run(expectPrivate=False):
    return preflight.runPreflight(PROJECT, "sales", expectPrivate=expectPrivate)


def test_passesBeforeTheFlipWhileTheBucketIsStillPublic(storage):
    storage.publicBucket = True

    result = _run()

    assert result["ok"] is True
    assert result["authenticatedRead"] == "ok"
    assert result["pagedReadRows"] == 3
    assert result["publicRoute"] == "reachable"


def test_passesAfterTheFlipWhenThePublicRouteIsRefused(storage):
    result = _run(expectPrivate=True)

    assert result["ok"] is True
    assert result["publicRoute"] == "refused"


def test_failsAfterTheFlipIfThePublicRouteStillServesTheObject(storage):
    storage.publicBucket = True

    assert _run(expectPrivate=True)["ok"] is False


def test_failsAfterTheFlipIfOnlyTheTableIsStillPublic(storage):
    storage.publicKeys = {f"AnalyticsHub/{PROJECT}/sales.parquet"}

    result = _run(expectPrivate=True)

    assert result["ok"] is False
    assert result["publicRoute"] == "reachable"


@pytest.mark.parametrize("status", [401, 403, 404])
def test_clientErrorsOnThePublicRouteCountAsRefused(storage, status):
    storage.publicRouteStatus = status

    result = _run(expectPrivate=True)

    assert result["ok"] is True
    assert result["publicRoute"] == "refused"


def test_aServerErrorOnThePublicRouteIsNotARefusal(storage):
    storage.publicRouteStatus = 503

    result = _run(expectPrivate=True)

    assert result["ok"] is False
    assert result["publicRoute"] == "unknown"


def test_failsAfterTheFlipIfTheAnonKeyCanStillRead(storage):
    storage.anonKey = ANON_KEY

    result = _run(expectPrivate=True)

    assert result["ok"] is False
    assert result["anonKeyRead"] == "reachable"


def test_afterTheFlipTheAnonKeyMustBeSupplied(storage, monkeypatch):
    monkeypatch.delenv("SUPABASE_ANON_KEY")

    result = _run(expectPrivate=True)

    assert result["ok"] is False
    assert result["anonKeyRead"] == "notChecked"


def test_beforeTheFlipTheAnonKeyIsOptional(storage, monkeypatch):
    storage.publicBucket = True
    monkeypatch.delenv("SUPABASE_ANON_KEY")

    assert _run()["ok"] is True


def test_probesDoNotDownloadTheTableThroughThePublicRoute(storage):
    storage.publicBucket = True

    _run()

    publicReads = [r for r in storage.requests if "/object/public/" in r["path"]]
    assert {r["path"].rsplit("/", 1)[1] for r in publicReads} == {"metadata.json", "sales.parquet"}
    assert all(r["range"] == "bytes=0-0" for r in publicReads)


def test_reportsTheCacheHeadersOfTheAuthenticatedRead(storage):
    storage.responseHeaders = {"Cache-Control": "max-age=3600", "cf-cache-status": "DYNAMIC"}

    result = _run()

    assert result["cacheControl"] == "max-age=3600"
    assert result["cfCacheStatus"] == "DYNAMIC"


def test_failsWhenAuthenticatedReadsFail(storage, monkeypatch):
    storage.publicBucket = True
    monkeypatch.setenv("SUPABASE_KEY", "wrong-key")

    result = _run()

    assert result["ok"] is False
    assert result["authenticatedRead"] == "failed"


def test_failsWhenATableCannotBePaged(storage):
    storage.publicBucket = True
    storage.ignoreRange = True

    result = _run()

    assert result["ok"] is False
    assert result["pagedReadRows"] is None


def test_aRefusedReadReportsTheStatusAndTheFile(storage, monkeypatch):
    storage.publicBucket = True
    monkeypatch.setenv("SUPABASE_KEY", "wrong-key")

    result = _run()

    assert "400" in result["authenticatedReadError"] and "metadata.json" in result["authenticatedReadError"]
    assert "400" in result["pagedReadError"] and "sales.parquet" in result["pagedReadError"]
    assert "wrong-key" not in json.dumps(result)


def test_aMissingTableIsToldApartFromAnIgnoredRange(storage):
    missing = preflight.runPreflight(PROJECT, "absent")
    storage.ignoreRange = True
    ignored = _run()

    assert "400" in missing["pagedReadError"] and "absent.parquet" in missing["pagedReadError"]
    assert "bytes" in ignored["pagedReadError"]
    assert missing["pagedReadError"] != ignored["pagedReadError"]


def test_aShortRangeBodyNamesTheBytesItAskedFor(storage):
    storage.truncateRanges = True

    assert "bytes" in _run()["pagedReadError"]


def test_otherErrorsStillReportOnlyTheirType(storage, monkeypatch):
    def fail(*_args):
        raise RuntimeError(SERVICE_KEY)

    monkeypatch.setattr(preflight, "readJson", fail)

    assert _run()["authenticatedReadError"] == "RuntimeError"


def test_resultCarriesNoStoredContentOrCredential(storage, monkeypatch):
    text = json.dumps(_run())

    assert "customer secret" not in text
    assert SERVICE_KEY not in text
    assert ANON_KEY not in text
