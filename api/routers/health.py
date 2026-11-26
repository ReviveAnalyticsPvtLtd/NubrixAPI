import os
from contextlib import closing

import psycopg2
import redis
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from redis.backoff import NoBackoff
from redis.retry import Retry


router = APIRouter()


@router.get("/readyz", include_in_schema=False)
def readiness():
    database_url = os.environ.get("DATABASE_URL")
    redis_host = os.environ.get("REDIS_HOST")
    if not database_url or not redis_host:
        return JSONResponse({"status": "unavailable"}, status_code=503)

    try:
        with closing(psycopg2.connect(database_url, connect_timeout=2)) as connection:
            with connection, connection.cursor() as cursor:
                cursor.execute("SET LOCAL statement_timeout = '2s'")
                cursor.execute("SELECT 1")

        with redis.Redis(
            host=redis_host,
            port=int(os.environ.get("REDIS_PORT", "6379")),
            password=os.environ.get("REDIS_PASSWORD"),
            socket_connect_timeout=2,
            socket_timeout=2,
            retry=Retry(NoBackoff(), 0),
        ) as client:
            if client.ping():
                return {"status": "ready"}
    except (psycopg2.Error, redis.exceptions.RedisError, ValueError):
        pass

    return JSONResponse({"status": "unavailable"}, status_code=503)
