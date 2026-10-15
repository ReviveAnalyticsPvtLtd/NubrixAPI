"""Slow credit reads must not stall other price requests on the API event loop."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.commons import verifyToken
from api.routers.credits import router


def test_slow_credit_catalog_read_does_not_block_other_requests(monkeypatch):
    from api.services.credits.topupService import topupService
    entered, release = Event(), Event()
    def listPacks(token):
        entered.set()
        assert release.wait(5)
        return {'packs':[], 'eligible':False, 'currency':'USD', 'catalogRevision':1}
    monkeypatch.setattr(topupService, 'listPacks', listPacks)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[verifyToken] = lambda:'verified-test-token'
    @app.get('/independent-price-read')
    async def independent(): return {'ready':True}
    with TestClient(app) as client, ThreadPoolExecutor(max_workers=2) as workers:
        blocked = workers.submit(client.get, '/topup/packs')
        assert entered.wait(3)
        ping = workers.submit(client.get, '/independent-price-read')
        try:
            assert ping.result(timeout=.5).json() == {'ready':True}
        finally:
            release.set()
            assert blocked.result(timeout=3).status_code == 200
