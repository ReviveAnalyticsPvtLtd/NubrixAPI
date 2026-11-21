import dataclasses
import socket
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
import sqlalchemy

from api.services import databaseSources as ds
from api.services.databaseSources import Source, listTables, readChunks, storedName, toSource
from utils.connectionTargets import BlockedAddressError


@pytest.fixture
def sqliteEngine(tmp_path):
    engine = sqlalchemy.create_engine(f"sqlite:///{tmp_path / 'x.db'}")
    with engine.begin() as conn:
        conn.execute(sqlalchemy.text("CREATE TABLE b (n INTEGER)"))
        conn.execute(sqlalchemy.text("CREATE TABLE a (n INTEGER)"))
        conn.execute(sqlalchemy.text("CREATE VIEW v AS SELECT n FROM a"))
        conn.execute(sqlalchemy.text('CREATE TABLE "order" (n INTEGER)'))
        conn.execute(sqlalchemy.text('CREATE TABLE "a b" (n INTEGER)'))
        for i in range(5):
            conn.execute(sqlalchemy.text(f"INSERT INTO a VALUES ({i})"))
        conn.execute(sqlalchemy.text('INSERT INTO "order" VALUES (7)'))
        conn.execute(sqlalchemy.text('INSERT INTO "a b" VALUES (8)'))
    with patch.object(ds, "_engine", return_value=engine):
        yield engine


MYSQL = Source(kind="mysql", host="h", address="1.2.3.4", port=3306, user="u", password="p", db="d")


def test_listsBaseTablesNotViews(sqliteEngine):
    names = [t[0] for t in listTables(MYSQL)]
    assert "v" not in names
    assert names == sorted(names) and {"a", "b"} <= set(names)


def test_readChunksSplitsRows(sqliteEngine):
    frames = list(readChunks(MYSQL, ("a", None), 2, 2000000))
    assert [len(f) for f in frames] == [2, 2, 1]


def test_tableNamesAreQuoted(sqliteEngine):
    assert list(readChunks(MYSQL, ("order", None), 10, 2000000))[0]["n"].tolist() == [7]
    assert list(readChunks(MYSQL, ("a b", None), 10, 2000000))[0]["n"].tolist() == [8]


def test_engineDisposedWhenGeneratorAbandoned(sqliteEngine):
    with patch.object(sqliteEngine, "dispose", wraps=sqliteEngine.dispose) as dispose:
        gen = readChunks(MYSQL, ("a", None), 2, 2000000)
        next(gen)
        gen.close()
    dispose.assert_called_once()


def test_postgresListingSkipsSystemSchemas():
    fake = MagicMock()
    fake.get_schema_names.return_value = ["public", "sales", "pg_catalog", "information_schema", "pg_toast"]
    fake.get_table_names.side_effect = lambda schema=None: {"public": ["orders"], "sales": ["orders", "a"]}[schema]
    pg = Source(kind="postgresql", host="h", address="1.2.3.4", port=5432, user="u", password="p", db="d")
    engine = MagicMock()
    with patch.object(ds, "_engine", return_value=engine), patch("sqlalchemy.inspect", return_value=fake):
        assert listTables(pg) == [("orders", "public"), ("a", "sales"), ("orders", "sales")]
    engine.dispose.assert_called_once()


def test_storedName():
    assert storedName(("orders", "sales")) == "sales_orders"
    assert storedName(("orders", "public")) == "orders"
    assert storedName(("a b", None)) == "a_b"


class FakeCollection:
    def __init__(self, docs):
        self.docs = docs

    def find(self, batch_size=0):
        return iter(self.docs)


class FakeMongo:
    def __init__(self, *a, **k):
        self.kwargs = k
        self.closed = False
        self.db = MagicMock()
        self.db.list_collection_names.return_value = ["system.views", "zeta", "alpha"]
        self.db.__getitem__.side_effect = lambda n: FakeCollection([{"_id": i, "x": i} for i in range(5)])

    def __getitem__(self, name):
        return self.db

    def close(self):
        self.closed = True


MONGO = Source(kind="mongodb", db="d", connectionString="mongodb://h/")


def test_listSkipsSystemCollections():
    with patch.object(ds, "MongoClient", FakeMongo):
        assert listTables(MONGO) == [("alpha", None), ("zeta", None)]


def test_readChunksDropsId():
    made = []

    def make(*a, **k):
        made.append(FakeMongo(*a, **k))
        return made[-1]

    with patch.object(ds, "MongoClient", make):
        frames = list(readChunks(MONGO, ("alpha", None), 2, 2000000))
    assert [len(f) for f in frames] == [2, 2, 1]
    assert all("_id" not in f.columns for f in frames)
    assert made[0].closed
    assert made[0].kwargs["server_selector"] is ds.publicServers
    assert made[0].kwargs["serverSelectionTimeoutMS"] == 10000
    assert made[0].kwargs["socketTimeoutMS"] == 300000


def test_sourceReprHasNoSecrets():
    s = Source(kind="mysql", host="h", user="u", password="pw-secret", db="d", connectionString="mongodb://u:cs-secret@h")
    assert "pw-secret" not in repr(s) and "cs-secret" not in str(s)
    assert "mysql" in repr(s) and "d" in repr(s)


def test_asdictRoundTrips():
    assert Source(**dataclasses.asdict(MYSQL)) == MYSQL


def test_engineUrlsPinTheAddress():
    pg = Source(kind="postgresql", host="db.example.com", address="1.2.3.4", port=5432, user="u", password="p", db="d")
    with patch.object(ds, "create_engine") as create:
        ds._engine(pg)
    url = create.call_args.args[0]
    assert url.host == "db.example.com"
    assert create.call_args.kwargs["connect_args"] == {"hostaddr": "1.2.3.4", "connect_timeout": 10,
        "keepalives": 1, "keepalives_idle": 60, "keepalives_interval": 10, "keepalives_count": 5}
    with patch.object(ds, "create_engine") as create:
        ds._engine(Source(kind="mysql", host="db.example.com", address="1.2.3.4", port=3306, user="u", password="p", db="d"))
    assert create.call_args.args[0].host == "1.2.3.4"
    assert create.call_args.kwargs["connect_args"] == {"connect_timeout": 10, "read_timeout": 300}


def test_toSourceResolvesAddress():
    body = {"host": "db.example.com", "port": 3306, "user": "u", "password": "p", "db": "d"}
    with patch.object(ds, "publicAddress", return_value="8.8.8.8") as pa:
        s = toSource("mysql", body)
    pa.assert_called_once_with("db.example.com", 3306)
    assert (s.host, s.address) == ("db.example.com", "8.8.8.8")


def test_toSourceMissingFieldNamesItWithoutSecrets():
    with pytest.raises(ValueError) as e:
        toSource("mysql", {"host": "h", "port": 1, "user": "u", "password": "pw-secret"})
    assert "db" in str(e.value) and "pw-secret" not in str(e.value)
    with pytest.raises(ValueError):
        toSource("mongodb", {"db": "d"})


def test_toSourcePropagatesBlockedAndUnresolvable():
    body = {"host": "h", "port": 1, "user": "u", "password": "p", "db": "d"}
    with patch.object(ds, "publicAddress", side_effect=BlockedAddressError("x")):
        with pytest.raises(BlockedAddressError):
            toSource("postgresql", body)
    with patch.object(ds, "publicAddress", side_effect=socket.gaierror):
        with pytest.raises(socket.gaierror):
            toSource("postgresql", body)
    with patch.object(ds, "checkMongoUri", side_effect=BlockedAddressError("x")):
        with pytest.raises(BlockedAddressError):
            toSource("mongodb", {"connectionString": "mongodb://h", "db": "d"})


def _spyConnect(engine):
    real = engine.connect
    spy = {}

    def connect():
        conn = real()
        spy["conn"] = conn
        spy["options"] = []
        orig = conn.execution_options
        conn.execution_options = lambda **kw: (spy["options"].append(kw), orig(**kw))[1]
        spy["invalidate"] = MagicMock(wraps=conn.invalidate)
        conn.invalidate = spy["invalidate"]
        return conn

    return patch.object(engine, "connect", connect), spy


def test_sqlReadStreamsAndExhaustedReadDoesNotInvalidate(sqliteEngine):
    cm, spy = _spyConnect(sqliteEngine)
    with cm:
        list(readChunks(MYSQL, ("a", None), 2, 2000000))
    assert {"stream_results": True} in spy["options"]
    spy["invalidate"].assert_not_called()


def test_abandonedSqlReadInvalidatesConnection(sqliteEngine):
    cm, spy = _spyConnect(sqliteEngine)
    with cm:
        gen = readChunks(MYSQL, ("a", None), 2, 2000000)
        next(gen)
        gen.close()
    spy["invalidate"].assert_called_once()


def test_dottedNamesStayApart():
    assert storedName(("fs.files", None)) != storedName(("fs.chunks", None))
    assert storedName(("logs.2023", None)) == "logs_2023"


def test_toSourceRejectsBadKindAndPort():
    with pytest.raises(ValueError):
        toSource("oracle", {})
    with pytest.raises(ValueError, match="port must be a number"):
        toSource("mysql", {"host": "h", "port": "abc", "user": "u", "password": "p", "db": "d"})


@pytest.mark.parametrize("columns, rows", [(100, 20000), (5, 50000), (5000, 1000)])
def test_sqlChunkRowsFollowTheTableWidth(sqliteEngine, columns, rows):
    inspector = MagicMock()
    inspector.get_columns.return_value = [{"name": f"c{i}"} for i in range(columns)]
    read = MagicMock(return_value=iter(()))
    with patch("sqlalchemy.inspect", return_value=inspector), patch.object(ds.pd, "read_sql", read):
        list(readChunks(MYSQL, ("a", "sales"), 50000, 2000000))
    inspector.get_columns.assert_called_once_with("a", schema="sales")
    assert read.call_args.kwargs["chunksize"] == rows
