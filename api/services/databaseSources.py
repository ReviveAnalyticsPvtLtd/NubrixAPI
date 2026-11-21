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
    required = ["connectionString", "db"] if kind == "mongodb" else ["host", "port", "user", "password", "db"]
    for field in required:
        if not body.get(field):
            raise ValueError(f"The {field} field is required.")
    if kind == "mongodb":
        checkMongoUri(body["connectionString"])
        return Source(kind=kind, db=body["db"], connectionString=body["connectionString"])
    return Source(
        kind=kind, host=body["host"], port=int(body["port"]), user=body["user"],
        password=body["password"], db=body["db"], address=publicAddress(body["host"], int(body["port"])),
    )


def _engine(source: Source):
    if source.kind == "mysql":
        url = URL.create("mysql+pymysql", username=source.user, password=source.password,
                         host=source.address, port=source.port, database=source.db)
        return create_engine(url, connect_args={"connect_timeout": 10})
    url = URL.create("postgresql+psycopg2", username=source.user, password=source.password,
                     host=source.host, port=source.port, database=source.db)
    # libpq connects to hostaddr and still uses host for SNI and certificate checks
    return create_engine(url, connect_args={"hostaddr": source.address, "connect_timeout": 10})


def _mongoClient(source: Source) -> MongoClient:
    return MongoClient(source.connectionString, server_api=ServerApi("1"),
                       server_selector=publicServers, serverSelectionTimeoutMS=10000)


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
                found = [(n, s) for s in inspector.get_schema_names()
                         if s not in _SKIPPED_SCHEMAS and not s.startswith("pg_toast")
                         for n in inspector.get_table_names(schema=s)]
        finally:
            engine.dispose()
    return sorted(found, key=lambda t: (t[1] or "", t[0]))


def storedName(table: SourceTable) -> str:
    name, schema = table
    return DataLoadService._sanitizeFileName(name if schema in (None, "public") else f"{schema}_{name}")


def readChunks(source: Source, table: SourceTable, chunkRows: int) -> Iterator[pd.DataFrame]:
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
        query = select(text("*")).select_from(sqlalchemy.table(name, schema=schema))
        with engine.connect() as conn:
            yield from pd.read_sql(query, conn, chunksize=chunkRows)
    finally:
        engine.dispose()
