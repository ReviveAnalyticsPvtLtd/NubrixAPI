"""Tests for the transformation service metadata URL builder."""

import types

import pytest


def _metadataUrl(projectId):
    from api.services.transformationService import TransformationService

    return TransformationService._metadata_url(types.SimpleNamespace(), projectId)


def test_metadataUrlRequiresFileUrl(monkeypatch):
    monkeypatch.delenv("FILE_URL", raising=False)
    with pytest.raises(KeyError):
        _metadataUrl("p1")

    monkeypatch.setenv("FILE_URL", "https://files.example.test/{projectId}/{fileName}.parquet")
    assert _metadataUrl("p1") == "https://files.example.test/p1/metadata.json"
