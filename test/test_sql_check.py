import pytest

from nubrix.components.sqlCheck import CheckedSql, SqlCheckError, checkSql

ACTIVE = ["orders", "customers", "order items"]


@pytest.mark.parametrize(
    "sql",
    [
        # table functions in FROM
        "SELECT * FROM read_parquet('x')",
        "SELECT * FROM read_csv('x')",
        "SELECT * FROM read_json_auto('x')",
        "SELECT * FROM sniff_csv('x')",
        "SELECT * FROM glob('*')",
        "SELECT * FROM query('select 1')",
        "SELECT * FROM query_table('orders')",
        "SELECT * FROM range(10)",
        "SELECT * FROM generate_series(1, 3)",
        "SELECT * FROM unnest([1, 2])",
        "SELECT * FROM main.read_csv('x')",
        'SELECT * FROM "read_csv"(\'x\')',
        "SELECT * FROM some_unknown_function(1)",
        "SELECT * FROM orders, LATERAL unnest([1, 2])",
        "SELECT * FROM orders JOIN range(3) r(i) ON true",
        "SELECT * FROM (SELECT * FROM read_csv('x'))",
        # statements other than SELECT
        "ATTACH 'x' AS y",
        "COPY orders TO 'x'",
        "PRAGMA version",
        "SET threads=1",
        "INSTALL httpfs",
        "LOAD httpfs",
        "CALL pragma_version()",
        "EXPORT DATABASE 'x'",
        "CREATE TABLE t (a INT)",
        "CREATE VIEW v AS SELECT 1",
        "DROP TABLE orders",
        "INSERT INTO orders VALUES (1)",
        "UPDATE orders SET a = 1",
        "DELETE FROM orders",
        "WITH a AS (SELECT 1) INSERT INTO orders SELECT * FROM a",
        "DESCRIBE orders",
        "SHOW TABLES",
        "EXPLAIN SELECT 1",
        "SUMMARIZE orders",
        "PIVOT orders ON a USING sum(b)",
        "SELECT * INTO copy_of_orders FROM orders",
        "USE other",
        "VACUUM",
        "CHECKPOINT",
        # system and environment functions
        "SELECT current_setting('threads')",
        "SELECT getvariable('x')",
        "SELECT getenv('HOME')",
        "SELECT which_secret('a', 'b')",
        "SELECT load_extension('x')",
        "SELECT json_execute_serialized_sql('x')",
        "SELECT * FROM duckdb_settings()",
        "SELECT duckdb_version()",
        "SELECT pragma_version()",
        "SELECT parquet_metadata('x')",
        "SELECT iceberg_scan('x')",
        "SELECT delta_scan('x')",
        "SELECT main.getenv('HOME')",
        'SELECT "GetEnv"(\'HOME\')',
        # dangerous functions nested anywhere
        "SELECT (SELECT count(*) FROM read_csv('x'))",
        "WITH a AS (SELECT * FROM glob('*')) SELECT * FROM a",
        "SELECT * FROM orders WHERE id IN (SELECT id FROM read_parquet('x'))",
        "SELECT a FROM orders UNION ALL SELECT b FROM read_csv('x')",
        "SELECT * FROM (SELECT getenv('HOME') AS h) s",
        "SELECT * FROM orders WHERE EXISTS (SELECT 1 FROM customers c WHERE c.a = getenv('A'))",
        "SELECT * FROM orders o, LATERAL (SELECT * FROM read_csv(o.path)) l",
        "SELECT CASE WHEN a > 1 THEN current_setting('threads') END FROM orders",
        "SELECT sum(a) OVER (PARTITION BY getenv('A')) FROM orders",
        # names
        "SELECT * FROM main.orders",
        "SELECT * FROM db.main.orders",
        "SELECT * FROM information_schema.tables",
        "SELECT * FROM unknown_table",
        "SELECT * FROM 'file.csv'",
        "SELECT * FROM orders, 'other.parquet'",
        "SELECT * FROM duckdb_tables",
        "WITH a AS (SELECT 1) SELECT * FROM b",
        # CTE names are not visible outside their scope
        "SELECT * FROM (WITH a AS (SELECT 1 AS x) SELECT x FROM a) s, a",
        # shape
        "SELECT 1; SELECT 2",
        "SELECT 1; DROP TABLE orders",
        "",
        "   ",
        ";",
        "not sql at all (",
        "hello world",
        "SELECT $1",
        "SELECT ?",
    ],
)
def test_rejected(sql):
    with pytest.raises(SqlCheckError):
        checkSql(sql, ACTIVE)


@pytest.mark.parametrize(
    "sql, tables",
    [
        ("SELECT * FROM orders", ("orders",)),
        ("WITH t AS (SELECT region, sum(amount) AS s FROM orders GROUP BY region) SELECT * FROM t", ("orders",)),
        ("WITH t AS (SELECT 1 AS a), u AS (SELECT a FROM t) SELECT * FROM u", ()),
        (
            "WITH RECURSIVE r AS (SELECT 1 AS n UNION ALL SELECT n + 1 FROM r WHERE n < 5) SELECT * FROM r",
            (),
        ),
        (
            "SELECT region, amount, row_number() OVER (PARTITION BY region ORDER BY amount DESC) AS rn "
            "FROM orders QUALIFY rn <= 3",
            ("orders",),
        ),
        (
            "SELECT region, amount FROM orders QUALIFY row_number() OVER (PARTITION BY region ORDER BY amount DESC) = 1",
            ("orders",),
        ),
        ("SELECT a FROM orders UNION ALL SELECT a FROM customers", ("orders", "customers")),
        ("SELECT a FROM orders INTERSECT SELECT a FROM customers", ("orders", "customers")),
        ("(SELECT a FROM orders) UNION (SELECT a FROM customers)", ("orders", "customers")),
        ("SELECT region, count(*) FROM orders GROUP BY ALL", ("orders",)),
        (
            "SELECT date_trunc('month', d) AS m, strftime(d, '%Y') AS y, date_diff('day', d, e) AS n "
            "FROM orders GROUP BY ALL",
            ("orders",),
        ),
        ("SELECT * FROM orders WHERE a > (SELECT avg(a) FROM orders)", ("orders",)),
        ("SELECT * FROM orders WHERE a IN (SELECT a FROM customers)", ("orders", "customers")),
        ("SELECT * FROM orders WHERE EXISTS (SELECT 1 FROM customers c WHERE c.id = orders.id)", ("orders", "customers")),
        ("SELECT (SELECT max(a) FROM customers) AS m, a FROM orders", ("customers", "orders")),
        ("SELECT * FROM (VALUES (1, 'a'), (2, 'b')) AS v(id, name)", ()),
        ("SELECT o.a FROM orders o JOIN (VALUES (1), (2)) v(id) ON o.id = v.id", ("orders",)),
        ("SELECT a.x, b.x FROM orders a JOIN orders b ON a.id = b.parent_id", ("orders",)),
        ('SELECT * FROM "order items"', ("order items",)),
        ('SELECT * FROM "order items" i JOIN orders o ON o.id = i.order_id', ("order items", "orders")),
        ("SELECT * FROM orders o LEFT JOIN customers c USING (id)", ("orders", "customers")),
        ("SELECT * FROM orders o, LATERAL (SELECT max(x) AS m FROM customers c WHERE c.id = o.id) l", ("orders", "customers")),
        ("SELECT unnest([1, 2, 3]) AS n", ()),
        ("SELECT range(3) AS r", ()),
        ("SELECT list_transform(l, x -> x + 1) FROM orders", ("orders",)),
        ("SELECT CAST(a AS VARCHAR), b::INT FROM orders", ("orders",)),
        ("SELECT * FROM orders USING SAMPLE 10 PERCENT", ("orders",)),
        ("FROM orders SELECT a", ("orders",)),
        ("SELECT * FROM orders;", ("orders",)),
        ("SELECT 1", ()),
        ("select 'read_csv(x)' AS note, 'getenv' AS g", ()),
        ("SELECT 1 AS range", ()),
        ("SELECT * FROM (SELECT 1 AS query) q", ()),
    ],
)
def test_accepted(sql, tables):
    checked = checkSql(sql, ACTIVE)
    assert isinstance(checked, CheckedSql)
    assert checked.tables == tables
    assert checked.sql == sql.strip().rstrip(";").strip()


def test_tableNamesMatchCaseInsensitivelyAndReturnCanonicalNames():
    checked = checkSql('select * from orders join "ORDER-ITEMS" using (id)', ["Orders", "order-items"])
    assert checked.tables == ("Orders", "order-items")


def test_cteShadowingAProjectTableIsTheCte():
    checked = checkSql("WITH orders AS (SELECT 1 AS a) SELECT * FROM orders", ACTIVE)
    assert checked.tables == ()


def test_nonRecursiveCteBodyStillReadsTheProjectTable():
    checked = checkSql("WITH orders AS (SELECT * FROM orders) SELECT * FROM orders", ACTIVE)
    assert checked.tables == ("orders",)


def test_cteScopeEndsWithItsQuery():
    checked = checkSql(
        "SELECT * FROM (WITH orders AS (SELECT 1 AS a) SELECT a FROM orders) s JOIN orders ON true", ACTIVE
    )
    assert checked.tables == ("orders",)


def test_tablesAreDistinctAndInFirstSeenOrder():
    checked = checkSql(
        "SELECT * FROM customers c JOIN orders o ON true JOIN customers d ON true JOIN orders p ON true", ACTIVE
    )
    assert checked.tables == ("customers", "orders")


def test_stringLiteralSourceNeverPassesAsAProjectTable():
    with pytest.raises(SqlCheckError, match="string"):
        checkSql("SELECT * FROM 'orders'", ACTIVE)


def test_activeTablesMayBeAnyIterable():
    checked = checkSql("SELECT * FROM orders", iter(["orders"]))
    assert checked.tables == ("orders",)


def test_noActiveTablesStillAllowsPlainSelect():
    assert checkSql("SELECT 1", []).tables == ()
    with pytest.raises(SqlCheckError):
        checkSql("SELECT * FROM orders", [])


def test_messagesNameTheConstructAndSayWhatIsAllowed():
    with pytest.raises(SqlCheckError) as info:
        checkSql("SELECT * FROM read_csv('x')", ACTIVE)
    message = str(info.value).lower()
    assert "read_csv" in message and "table function" in message and "cte" in message

    with pytest.raises(SqlCheckError) as info:
        checkSql("DROP TABLE orders", ACTIVE)
    message = str(info.value)
    assert "DROP" in message and "SELECT" in message

    with pytest.raises(SqlCheckError) as info:
        checkSql("SELECT * FROM main.orders", ACTIVE)
    assert "main" in str(info.value) and "schema" in str(info.value).lower()

    with pytest.raises(SqlCheckError) as info:
        checkSql("SELECT * FROM mystery", ACTIVE)
    assert "mystery" in str(info.value) and "orders" in str(info.value)

    with pytest.raises(SqlCheckError) as info:
        checkSql("SELECT getenv('HOME')", ACTIVE)
    assert "getenv" in str(info.value)

    with pytest.raises(SqlCheckError) as info:
        checkSql("SELECT 1; SELECT 2", ACTIVE)
    assert "one" in str(info.value).lower()

    with pytest.raises(SqlCheckError) as info:
        checkSql("", ACTIVE)
    assert "empty" in str(info.value).lower()


def test_parseErrorKeepsOnlyItsFirstLine():
    with pytest.raises(SqlCheckError) as info:
        checkSql("SELECT * FROM (", ACTIVE)
    assert "\n" not in str(info.value)


@pytest.mark.parametrize(
    "cte, source",
    [
        ("duckdb_tableſ", "duckdb_tables"),
        ("ſqlite_master", "sqlite_master"),
        ("pg_claſſ", "pg_class"),
        ("straße", "strasse"),
        ("Kelvin_k", "kelvin_k"),
        ("dir/probe_ſecret.csv", "dir/probe_secret.csv"),
    ],
)
def test_unicodeLookalikeCteDoesNotHideARealSource(cte, source):
    with pytest.raises(SqlCheckError):
        checkSql(f'WITH "{cte}" AS (SELECT 1 AS a) SELECT * FROM "{source}"', ["orders"])


@pytest.mark.parametrize(
    "active, used",
    [("ſales.csv", "sales.csv"), ("sales.csv", "ſales.csv"), ("strasse", "straße"), ("kelvin", "Kelvin")],
)
def test_unicodeFoldingDoesNotMatchActiveTables(active, used):
    with pytest.raises(SqlCheckError):
        checkSql(f'SELECT * FROM "{used}"', [active])


def test_nonAsciiActiveTableFoldsAsciiLettersOnly():
    assert checkSql('SELECT * FROM "ÄRGER"', ["Ärger"]).tables == ("Ärger",)
    assert checkSql('SELECT * FROM "Ärger"', ["Ärger"]).tables == ("Ärger",)
    with pytest.raises(SqlCheckError):
        checkSql('SELECT * FROM "ärger"', ["Ärger"])
