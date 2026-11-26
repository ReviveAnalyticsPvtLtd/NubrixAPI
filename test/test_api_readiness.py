from contextlib import nullcontext
from unittest.mock import MagicMock, Mock, call

import psycopg2
import pytest
import redis
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routers import health


@pytest.fixture
def readiness(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://example/fixture")
    monkeypatch.setenv("REDIS_HOST", "redis.fixture")
    monkeypatch.setenv("REDIS_PORT", "6380")
    monkeypatch.setenv("REDIS_PASSWORD", "fixture")

    cursor = Mock()
    connection = MagicMock()
    connection.cursor.return_value = nullcontext(cursor)
    postgres = Mock(return_value=connection)
    cache = Mock()
    cache.ping.return_value = True
    redis_client = Mock(return_value=nullcontext(cache))
    monkeypatch.setattr(health.psycopg2, "connect", postgres)
    monkeypatch.setattr(health.redis, "Redis", redis_client)

    app = FastAPI()
    app.include_router(health.router)
    return TestClient(app), postgres, cursor, connection, redis_client, cache


def test_ready_requires_postgres_and_redis(readiness):
    client, postgres, cursor, connection, redis_client, cache = readiness

    response = client.get("/readyz")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}
    postgres.assert_called_once_with("postgresql://example/fixture", connect_timeout=2)
    assert cursor.execute.call_args_list == [
        call("SET LOCAL statement_timeout = '2s'"),
        call("SELECT 1"),
    ]
    connection.close.assert_called_once()
    assert redis_client.call_args.kwargs["socket_timeout"] == 2
    cache.ping.assert_called_once()


@pytest.mark.parametrize("dependency", ["postgres", "redis"])
def test_unavailable_dependency_returns_generic_503(readiness, dependency):
    client, postgres, _, _, _, cache = readiness
    if dependency == "postgres":
        postgres.side_effect = psycopg2.OperationalError("database connection details")
    else:
        cache.ping.side_effect = redis.exceptions.ConnectionError("redis connection details")

    response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}


def test_missing_database_configuration_is_unavailable(readiness, monkeypatch):
    client, postgres, *_ = readiness
    monkeypatch.delenv("DATABASE_URL")

    assert client.get("/readyz").status_code == 503
    postgres.assert_not_called()
