import itertools
from dataclasses import dataclass
from typing import Iterator, Literal

import pandas as pd
import sqlalchemy
from pymongo import MongoClient
from pymongo.server_api import ServerApi
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import URL

from api.services.dataLoadService import DataLoadService
from utils.connectionTargets import checkMongoUri, publicAddress, publicServers

SourceTable = tuple[str, str | None]
_SKIPPED_SCHEMAS = ("information_schema", "pg_catalog", "pg_toast")
# Supabase and hosted PostgreSQL internals: credentials, tokens and password hashes, never the user's own data.
_PLATFORM_SCHEMAS = ("auth", "storage", "realtime", "_realtime", "vault", "pgsodium", "pgsodium_masks", "extensions",
                     "graphql", "graphql_public", "supabase_functions", "supabase_migrations", "cron", "net", "pgbouncer",
                     "_analytics")
# get_table_names lists partitioned tables and their partitions; the parent holds every row.
_PARTITIONS = text("SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                   "WHERE n.nspname = :schema AND c.relispartition")
_MIN_CHUNK_ROWS = 1000


@dataclass(frozen=True, repr=False)
class Source:
    kind: Literal["mysql", "postgresql", "mongodb"]
    host: str = ""  # the user's host name
    port: int = 0
    user: str = ""
    password: str = ""
    db: str = ""
    connectionString: str = ""
    address: str = ""  # the resolved public IP of host

    def __repr__(self) -> str:
        return f"Source(kind={self.kind!r}, db={self.db!r})"


def toSource(kind: str, body: dict) -> Source:
    """Build a Source from a request body, checking its address. Errors never echo secrets."""
    if kind not in ("mysql", "postgresql", "mongodb"):
        raise ValueError("The database type must be mysql, postgresql or mongodb.")
    required = ["connectionString", "db"] if kind == "mongodb" else ["host", "port", "user", "password", "db"]
    for field in required:
        if not body.get(field):
            raise ValueError(f"The {field} field is required.")
    if kind == "mongodb":
        checkMongoUri(body["connectionString"])
        return Source(kind=kind, db=body["db"], connectionString=body["connectionString"])
    try:
        port = int(body["port"])
    except (TypeError, ValueError):
        raise ValueError("The port must be a number.") from None
    return Source(
        kind=kind, host=body["host"], port=port, user=body["user"],
        password=body["password"], db=body["db"], address=publicAddress(body["host"], port),
    )


def _engine(source: Source):
    if source.kind == "mysql":
        url = URL.create("mysql+pymysql", username=source.user, password=source.password,
                         host=source.address, port=source.port, database=source.db)
        return create_engine(url, connect_args={"connect_timeout": 10, "read_timeout": 300})
    assert source.kind == "postgresql", source.kind
    url = URL.create("postgresql+psycopg2", username=source.user, password=source.password,
                     host=source.host, port=source.port, database=source.db)
    # libpq connects to hostaddr and still uses host for SNI and certificate checks
    return create_engine(url, connect_args={
        "hostaddr": source.address, "connect_timeout": 10,
        "keepalives": 1, "keepalives_idle": 60, "keepalives_interval": 10, "keepalives_count": 5,
    })


def _mongoClient(source: Source) -> MongoClient:
    return MongoClient(source.connectionString, server_api=ServerApi("1"),
                       server_selector=publicServers, serverSelectionTimeoutMS=10000, socketTimeoutMS=300000)


def listTables(source: Source) -> list[SourceTable]:
    if source.kind == "mongodb":
        client = _mongoClient(source)
        try:
            names = [n for n in client[source.db].list_collection_names() if not n.startswith("system.")]
        finally:
            client.close()
        found = [(n, None) for n in names]
    else:
        engine = _engine(source)
        try:
            inspector = sqlalchemy.inspect(engine)
            if source.kind == "mysql":
                found = [(n, None) for n in inspector.get_table_names()]
            else:
                found = []
                with engine.connect() as conn:
                    for s in inspector.get_schema_names():
                        if s in _SKIPPED_SCHEMAS or s in _PLATFORM_SCHEMAS or s.startswith("pg_toast"):
                            continue
                        partitions = set(conn.execute(_PARTITIONS, {"schema": s}).scalars())
                        found += [(n, s) for n in inspector.get_table_names(schema=s) if n not in partitions]
        finally:
            engine.dispose()
    return sorted(found, key=lambda t: (t[1] or "", t[0]))


def storedName(table: SourceTable) -> str:
    name, schema = table
    return DataLoadService._sanitizeName(name if schema in (None, "public") else f"{schema}_{name}") or "table"


def readChunks(source: Source, table: SourceTable, chunkRows: int, chunkCells: int) -> Iterator[pd.DataFrame]:
    """The table in DataFrames of at most chunkRows rows; a SQL chunk also holds about chunkCells cells, never under 1,000 rows."""
    name, schema = table
    if source.kind == "mongodb":
        client = _mongoClient(source)
        try:
            for batch in itertools.batched(client[source.db][name].find(batch_size=chunkRows), chunkRows):
                yield pd.DataFrame([{k: v for k, v in doc.items() if k != "_id"} for doc in batch])
        finally:
            client.close()
        return
    engine = _engine(source)
    try:
        columns = len(sqlalchemy.inspect(engine).get_columns(name, schema=schema))
        chunkRows = min(chunkRows, max(_MIN_CHUNK_ROWS, chunkCells // max(1, columns)))
        query = select(text("*")).select_from(sqlalchemy.table(name, schema=schema))
        # stream_results: without it pymysql and psycopg2 buffer the whole result before the first chunk
        conn = engine.connect().execution_options(stream_results=True)
        finished = False
        try:
            yield from pd.read_sql(query, conn, chunksize=chunkRows)
            finished = True
        finally:
            if not finished:
                conn.invalidate()  # an abandoned pymysql SSCursor would drain the rest on close
            conn.close()
    finally:
        engine.dispose()
