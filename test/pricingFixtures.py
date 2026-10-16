"""Explicit catalog boundary for unit tests; PostgreSQL tests do not use it."""
from datetime import datetime, timezone
from types import SimpleNamespace
import pytest
from api.services.billing.pricingCatalogContracts import CatalogSnapshot
from test.test_pricing_catalog_contracts import BASELINE


@pytest.fixture(autouse=True)
def catalog_source(monkeypatch):
    catalog = CatalogSnapshot(1, BASELINE, datetime.now(timezone.utc), None, 'fixture', 'Unit test catalog')
    repository = SimpleNamespace(loadCurrent=lambda **kwargs: catalog)
    monkeypatch.setattr('api.services.billing.pricingCatalogRepository.getPricingCatalogRepository',lambda:repository)
    return catalog
