"""Tests for the transformation service metadata URL builder."""

import types

import pytest


def _metadataUrl(projectId):
    from api.services.transformationService import TransformationService

    return TransformationService._metadata_url(types.SimpleNamespace(), projectId)


def test_metadataUrlRequiresSupabaseUrl(monkeypatch):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    with pytest.raises(KeyError):
        _metadataUrl("p1")

    monkeypatch.setenv("SUPABASE_URL", "https://storage.example.test")
    assert _metadataUrl("p1") == "https://storage.example.test/storage/v1/object/AnalyticsHub/p1/metadata.json"
