"""Billing routes run blocking DB/provider work in FastAPI's threadpool."""
import ast
import asyncio
import time
from pathlib import Path

import httpx
from fastapi import FastAPI


ROUTER_FILES = ('api/routers/subscriptions.py', 'api/routers/credits.py')
# Billing handlers in routers that are otherwise out of this guard's scope: only these names are checked.
# getUserProfile is the billing refresh behind every settings focus and every grant.
NAMED_HANDLERS = {'api/routers/manager.py': {'getUserProfile'}}
ROUTE_METHODS = {'get', 'post', 'put', 'patch', 'delete', 'head', 'options', 'api_route'}


def _isRouterRoute(decorator):
    target = decorator.func if isinstance(decorator, ast.Call) else decorator
    return (isinstance(target, ast.Attribute) and target.attr in ROUTE_METHODS
            and isinstance(target.value, ast.Name) and target.value.id == 'router')


def _routeHandlers(relative):
    root = Path(__file__).resolve().parent.parent
    tree = ast.parse((root / relative).read_text(encoding='utf-8'), filename=relative)
    return [node for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and any(_isRouterRoute(decorator) for decorator in node.decorator_list)]


def _blocksTheEventLoop(node):
    return isinstance(node, ast.AsyncFunctionDef) and not any(isinstance(child, ast.Await) for child in ast.walk(node))


def test_billing_route_handlers_never_block_the_event_loop():
    offenders = []
    for relative in ROUTER_FILES:
        offenders += [f'{relative}:{node.lineno} {node.name}' for node in _routeHandlers(relative) if _blocksTheEventLoop(node)]
    for relative, names in NAMED_HANDLERS.items():
        handlers = [node for node in _routeHandlers(relative) if node.name in names]
        missing = sorted(names - {node.name for node in handlers})
        assert not missing, f'{relative} no longer defines the named billing route handler(s) {missing}'
        offenders += [f'{relative}:{node.lineno} {node.name}' for node in handlers if _blocksTheEventLoop(node)]
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
