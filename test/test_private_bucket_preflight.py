"""Read-only rollout check for the private AnalyticsHub bucket."""

import io
import json

import polars as pl
import pytest

import scripts.privateBucketPreflight as preflight
from test.privateStorageFixtures import SERVICE_KEY, FakePrivateStorage

PROJECT = "11111111-2222-4333-8444-555555555555"


@pytest.fixture
def storage(monkeypatch):
    fake = FakePrivateStorage()
    buffer = io.BytesIO()
    pl.DataFrame({"amount": [1, 2, 3]}).write_parquet(buffer)
    fake.put(PROJECT, "sales.parquet", buffer.getvalue())
    fake.putJson(PROJECT, "metadata.json", {"sales": {"description": "customer secret"}})
    monkeypatch.setenv("SUPABASE_URL", fake.url)
    monkeypatch.setenv("SUPABASE_KEY", SERVICE_KEY)
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


def test_resultCarriesNoStoredContentOrCredential(storage, monkeypatch):
    text = json.dumps(_run())

    assert "customer secret" not in text
    assert SERVICE_KEY not in text
