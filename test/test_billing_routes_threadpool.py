"""Billing routes run blocking DB/provider work in FastAPI's threadpool."""
import ast
import asyncio
import time
from pathlib import Path

import httpx
from fastapi import FastAPI


ROUTER_FILES = ('api/routers/subscriptions.py', 'api/routers/credits.py')
ROUTE_METHODS = {'get', 'post', 'put', 'patch', 'delete', 'head', 'options', 'api_route'}


def _isRouterRoute(decorator):
    target = decorator.func if isinstance(decorator, ast.Call) else decorator
    return (isinstance(target, ast.Attribute) and target.attr in ROUTE_METHODS
            and isinstance(target.value, ast.Name) and target.value.id == 'router')


def test_billing_route_handlers_never_block_the_event_loop():
    offenders = []
    root = Path(__file__).resolve().parent.parent
    for relative in ROUTER_FILES:
        tree = ast.parse((root / relative).read_text(encoding='utf-8'), filename=relative)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not any(_isRouterRoute(decorator) for decorator in node.decorator_list):
                continue
            awaits = any(isinstance(child, ast.Await) for child in ast.walk(node))
            if isinstance(node, ast.AsyncFunctionDef) and not awaits:
                offenders.append(f'{relative}:{node.lineno} {node.name}')
    assert not offenders, (
        f'{len(offenders)} async route handler(s) never await and block the event loop; '
        'declare them with def so FastAPI runs them in its threadpool:\n' + '\n'.join(offenders))


def test_slow_renewal_preparation_does_not_serialize_other_requests(monkeypatch):
    from api.commons import verifyToken
    from api.routers import subscriptions
    from api.services.subscriptions.subscriptionService import subscriptionService

    app = FastAPI()
    app.include_router(subscriptions.router)
    app.dependency_overrides[verifyToken] = lambda: 'local-token'

    def slowPrepare(**kwargs):
        time.sleep(0.4)
        return {}

    monkeypatch.setattr(subscriptionService, 'prepareRenewalInvoice', slowPrepare)

    async def fireTwo():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url='http://testserver') as client:
            started = time.perf_counter()
            responses = await asyncio.gather(
                client.post('/prepareRenewalInvoice', json={}),
                client.post('/prepareRenewalInvoice', json={}),
            )
            return responses, time.perf_counter() - started

    responses, elapsed = asyncio.run(fireTwo())

    assert [response.status_code for response in responses] == [200, 200]
    assert elapsed < 0.7, f'two concurrent renewal preparations took {elapsed:.3f}s; blocking gives >= 0.8s'
