"""The SQL chart prompts: they load and format, their worked examples are valid and run, and the chains are wired.

The worked examples are written against an invented retail schema (EXAMPLE_SCHEMAS below). The generator prompt
shows that schema exactly as `groundingText` prints it, and every example must validate, pass `checkSql`, run in
DuckDB over a few invented rows and build a chart the client can read.
"""

import datetime
import hashlib
import json
import os
import re
from decimal import Decimal

import duckdb
import pyarrow as pa
import pytest
import sqlglot
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import PromptTemplate
from langchain_core.runnables import RunnableBinding

from nubrix.components.chartBuilder import buildChart
from nubrix.components.chartContract import contractErrors
from nubrix.components.chartRequest import CATEGORY_TYPES, CHART_TYPES, ChartRequest, requiredColumns
from nubrix.components.sqlCheck import checkSql
from nubrix.components.sqlGrounding import groundingText
from nubrix.utils import readYaml

PROMPTS_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "prompts.yaml")

NEW_PROMPTS = {
    "sqlQueryRephraserAgentPrompt": {"metadata", "query", "format_instructions"},
    "parallelSqlQueryRephraserAgentPrompt": {"metadata", "query", "format_instructions"},
    "sqlChartGeneratorPrompt": {"grounding", "question", "plan"},
    "sqlChartRepairPrompt": {"grounding", "question", "plan", "previous_request", "error"},
}
OLD_PROMPTS = ("queryRephraserAgentPrompt", "parallelQueryRephraserAgentPrompt", "codeGeneratorAgentPrompt", "codeDebuggerAgentPrompt")

# sha256 of every prompt that existed before the SQL chart prompts; the old engine must keep reading them unchanged.
OLD_PROMPT_SHA256 = {
    "metadataGeneratorPrompt": "c7b0abac47efff4e4de4d692c89dee8bac9a2b7ad298ec04b77c70dd5196b5a7",
    "queryRephraserAgentPrompt": "652cedc1e0aac6f2e56cdc004759a2289fdbb58e3287e1410b686c90ee0d2c99",
    "parallelQueryRephraserAgentPrompt": "ddfd9a2632702a3489e05ec6e6c3b00796663bf9c606731a873bd7bbd56603f0",
    "codeGeneratorAgentPrompt": "6a671adf30593e5d0e47da104b13bf4604ff7e415df6c3281c41df6aa57e78f4",
    "codeDebuggerAgentPrompt": "492636fcb512ac31ab138c8346175aa99ea734b6a9fa3b03927d7f38f239abff",
    "insightGeneratorAgentPrompt": "842b034c10df6e0031c7eac4912759ed0379f36fd72005cfcf6081f60daf3539",
    "imageToInsightGeneratorPrompt": "c6e225d12b696c18857f968c5fab2496101ab136cee3c58208e2f33bbaa54dd7",
    "pdfTableExtractionPrompt": "cbcf60f9fd77cb72d4ad3b53c45a7fbbc21da36f4e3686f0655c6cc1712f35ce",
    "pdfTableMergePrompt": "33e351b750bc2b94442278e3f37ffb47219d6549a267b3cc0ab0f7f7cc9d3f4d",
    "domainAwareKpiMappingAgentPrompt": "653f03ff0f3036e55675ca9113c3b0c0c1196c27c407609c0daefa31cf72932e",
    "dashboardNameGeneratorPrompt": "b1a771f36e133c5c323393175134b42b5294d64f30a36664d55fa1c37f578710",
    "transformationAgentPrompt": "2e5c850fecd73c32d387e0c96261a43a2a9196fcfbad2f0dab33170c97485390",
}

# Spec section 6.1, verbatim.
ENCODING_TABLE = """| chartType | encoding (column names from the query result) |
|---|---|
| `bar`, `line`, `area`, `radar` | `x`; `y`: one or more value columns (one dataset each); optional `series`: a column whose values become datasets (long form). `y` with several columns and `series` together is invalid. |
| `histogram` | `x` (bin label), `y` (count). The query computes the bins. |
| `pie`, `doughnut`, `polarArea` | `label`, `value` |
| `scatter` | `x`, `y`, optional `series` |
| `bubble` | `x`, `y`, `r`, optional `series` |
| `card` | `value`; optional `label` text (literal, not a column) |
| `table` | `columns`: ordered list (default: all result columns in query order) |
| `pivot` | `rows`: one or more columns; `columns`: one or more columns; `value`: one column. The query returns long-form, already-aggregated rows. |
| `geoMap` | `lat`, `long`; optional `label`, optional `id` |"""

# ---------------------------------------------------------------------------
# The invented example schema (generic retail tables, no real project).
# ---------------------------------------------------------------------------

EXAMPLE_SCHEMAS = {
    "orders": pa.schema(
        [
            ("order_id", pa.int64()),
            ("customer_id", pa.int64()),
            ("store_id", pa.int64()),
            ("order_date", pa.date32()),
            ("channel", pa.string()),
            ("status", pa.string()),
            ("total_amount", pa.decimal128(12, 2)),
            ("discount_amount", pa.float64()),
        ]
    ),
    "order_items": pa.schema(
        [
            ("order_id", pa.int64()),
            ("product_id", pa.int64()),
            ("quantity", pa.int32()),
            ("unit_price", pa.float64()),
        ]
    ),
    "products": pa.schema(
        [
            ("product_id", pa.int64()),
            ("product_name", pa.string()),
            ("category", pa.string()),
            ("list_price", pa.float64()),
        ]
    ),
    "customers": pa.schema(
        [
            ("customer_id", pa.int64()),
            ("full_name", pa.string()),
            ("segment", pa.string()),
            ("city", pa.string()),
            ("signup_date", pa.date32()),
        ]
    ),
    "stores": pa.schema(
        [
            ("store_id", pa.int64()),
            ("store_name", pa.string()),
            ("region", pa.string()),
            ("latitude", pa.float64()),
            ("longitude", pa.float64()),
            ("floor_area_sqm", pa.float64()),
        ]
    ),
}


def _columns(*pairs):
    return [{"name": name, "type": "object", "description": description} for name, description in pairs]


EXAMPLE_METADATA = {
    "orders": {
        "description": "One row per customer order.",
        "columns": _columns(
            ("order_id", "Unique order number."),
            ("customer_id", "Customer who placed the order."),
            ("store_id", "Store that fulfilled the order."),
            ("order_date", "Date the order was placed."),
            ("channel", "Sales channel."),
            ("status", "Order status."),
            ("total_amount", "Order value after discounts."),
            ("discount_amount", "Discount given on the order."),
        ),
        "sample_row": {
            "order_id": 1001,
            "customer_id": 17,
            "store_id": 3,
            "order_date": "2024-03-15",
            "channel": "Online",
            "status": "Delivered",
            "total_amount": 129.9,
            "discount_amount": 10.0,
        },
    },
    "order_items": {
        "description": "One row per product line on an order.",
        "columns": _columns(
            ("order_id", "Order the line belongs to."),
            ("product_id", "Product sold."),
            ("quantity", "Units sold."),
            ("unit_price", "Price per unit charged."),
        ),
        "sample_row": {"order_id": 1001, "product_id": 42, "quantity": 2, "unit_price": 59.95},
    },
    "products": {
        "description": "Product catalogue.",
        "columns": _columns(
            ("product_id", "Unique product number."),
            ("product_name", "Product name."),
            ("category", "Product category."),
            ("list_price", "Catalogue price."),
        ),
        "sample_row": {"product_id": 42, "product_name": "Desk Lamp", "category": "Home & Garden", "list_price": 59.95},
    },
    "customers": {
        "description": "One row per customer.",
        "columns": _columns(
            ("customer_id", "Unique customer number."),
            ("full_name", "Customer name."),
            ("segment", "Customer segment."),
            ("city", "City of the delivery address."),
            ("signup_date", "Date the customer signed up."),
        ),
        "sample_row": {
            "customer_id": 17,
            "full_name": "Maya Lin",
            "segment": "Consumer",
            "city": "Springfield",
            "signup_date": "2023-11-02",
        },
    },
    "stores": {
        "description": "One row per store.",
        "columns": _columns(
            ("store_id", "Unique store number."),
            ("store_name", "Store name."),
            ("region", "Sales region."),
            ("latitude", "Store latitude in degrees."),
            ("longitude", "Store longitude in degrees."),
            ("floor_area_sqm", "Sales floor area in square metres."),
        ),
        "sample_row": {
            "store_id": 3,
            "store_name": "Riverside",
            "region": "North",
            "latitude": 52.1,
            "longitude": -1.5,
            "floor_area_sqm": 850.0,
        },
    },
}


def _exampleTables() -> dict[str, pa.Table]:
    """Deterministic invented rows: an order every day of 2024 (plus some in 2023), 12 products in 5 categories.

    Dense enough that a LIMIT on a time axis or a long-form query would visibly cut data.
    """
    stores = {
        "store_id": [1, 2, 3],
        "store_name": ["Riverside", "Hilltop", "Harbour"],
        "region": ["North", "South", "North"],
        "latitude": [52.1, 50.8, 53.4],
        "longitude": [-1.5, -0.1, -3.0],
        "floor_area_sqm": [850.0, 1200.0, 640.0],
    }
    customers = {
        "customer_id": [1, 2, 3, 4, 5, 6],
        "full_name": ["Maya Lin", "Tom Reed", "Ana Ruiz", "Li Wei", "Sam Cole", "Ivy Park"],
        "segment": ["Consumer", "Corporate", "Consumer", "Small Business", "Corporate", "Consumer"],
        "city": ["Springfield", "Lakeside", "Springfield", "Hillview", "Lakeside", "Brookfield"],
        "signup_date": [datetime.date(2023, 1, 5) + datetime.timedelta(days=61 * n) for n in range(6)],
    }
    products = {
        "product_id": list(range(1, 13)),
        "product_name": [
            "Desk Lamp", "Headphones", "Garden Hose", "Puzzle", "Smart Speaker", "Football",
            "Novel", "Plant Pot", "Tablet", "Board Game", "Yoga Mat", "Cookbook",
        ],
        "category": [
            "Home & Garden", "Electronics", "Home & Garden", "Toys", "Electronics", "Sports",
            "Books", "Home & Garden", "Electronics", "Toys", "Sports", "Books",
        ],
        "list_price": [59.95, 89.0, 24.5, 15.0, 129.0, 22.0, 12.5, 18.0, 249.0, 35.0, 28.0, 21.0],
    }
    days = [datetime.date(2023, month, day) for month in range(1, 13) for day in (5, 20)]
    days += [datetime.date(2024, 1, 1) + datetime.timedelta(days=n) for n in range(366)]
    orders: dict[str, list] = {name: [] for name in EXAMPLE_SCHEMAS["orders"].names}
    items: dict[str, list] = {name: [] for name in EXAMPLE_SCHEMAS["order_items"].names}
    statuses = ["Delivered", "Shipped", "Cancelled", "Delivered", "Returned"]
    for number, day in enumerate(days):
        lines = [((number * 5) % 12 + 1, number % 3 + 1)]
        if number % 2 == 0:
            lines.append(((number * 7 + 3) % 12 + 1, 1))
        amount = sum(quantity * products["list_price"][product - 1] for product, quantity in lines)
        discount = 5.0 if number % 4 == 0 else 0.0
        orders["order_id"].append(1000 + number)
        orders["customer_id"].append(number % 6 + 1)
        orders["store_id"].append(number % 3 + 1)
        orders["order_date"].append(day)
        orders["channel"].append("In-Store" if number % 3 == 0 else "Online")
        orders["status"].append(statuses[number % 5])
        orders["total_amount"].append(Decimal(f"{amount - discount:.2f}"))
        orders["discount_amount"].append(discount)
        for product, quantity in lines:
            items["order_id"].append(1000 + number)
            items["product_id"].append(product)
            items["quantity"].append(quantity)
            items["unit_price"].append(products["list_price"][product - 1])
    data = {"orders": orders, "order_items": items, "products": products, "customers": customers, "stores": stores}
    return {name: pa.table(data[name], schema=EXAMPLE_SCHEMAS[name]) for name in EXAMPLE_SCHEMAS}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _prompts() -> dict:
    return readYaml(PROMPTS_PATH)


def _formatted(key: str) -> str:
    template = PromptTemplate.from_template(_prompts()[key])
    return template.format(**{name: f"<{name}>" for name in template.input_variables})


def _answers(text: str) -> list[dict]:
    """Every worked-example answer: a line that is a JSON object starting with "chartType"."""
    return [json.loads(line) for line in re.findall(r'^(\{"chartType".*\})\s*$', text, flags=re.MULTILINE)]


def _examples() -> list[ChartRequest]:
    return [ChartRequest.model_validate(answer) for answer in _answers(_formatted("sqlChartGeneratorPrompt"))]


def _example(chartType: str) -> ChartRequest:
    (request,) = [request for request in _examples() if request.chartType == chartType]
    return request


def _query(sql: str, tables: dict[str, pa.Table]) -> list[tuple]:
    connection = duckdb.connect()
    for name, table in tables.items():
        connection.register(name, table)
    return connection.execute(sql).fetchall()


def _hasOuterLimit(sql: str) -> bool:
    """LIMIT on the statement itself (a LIMIT inside a CTE or subquery does not count)."""
    return sqlglot.parse_one(sql, read="duckdb").args.get("limit") is not None


def _runExample(request: ChartRequest, tables: dict[str, pa.Table]) -> dict:
    checked = checkSql(request.sql, EXAMPLE_SCHEMAS)
    connection = duckdb.connect()
    for name, table in tables.items():
        connection.register(name, table)
    result = connection.execute(checked.sql).fetch_arrow_table()
    missing = [name for name in requiredColumns(request) + list(request.encoding.columns or []) if name not in result.column_names]
    assert not missing, f"{request.chartType}: result lacks {missing}; has {result.column_names}"
    assert result.num_rows > 0, f"{request.chartType}: the example returned no rows on the example data"
    chart = buildChart(request, result)
    assert contractErrors(chart) == [], f"{request.chartType}: {contractErrors(chart)}"
    return chart


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("key", sorted(NEW_PROMPTS))
def test_promptsExistAndFormat(key):
    text = _prompts().get(key)
    assert isinstance(text, str) and text.strip()

    template = PromptTemplate.from_template(text)

    assert set(template.input_variables) == NEW_PROMPTS[key]
    formatted = template.format(**{name: f"<<{name}>>" for name in template.input_variables})
    for name in template.input_variables:
        assert f"<<{name}>>" in formatted


def test_oldPromptsStay():
    prompts = _prompts()
    for key in OLD_PROMPTS:
        assert isinstance(prompts.get(key), str) and prompts[key].strip()


def test_oldPromptsAreByteForByteUnchanged():
    prompts = _prompts()

    assert set(prompts) == set(OLD_PROMPT_SHA256) | set(NEW_PROMPTS)
    for key, digest in OLD_PROMPT_SHA256.items():
        assert hashlib.sha256(prompts[key].encode("utf-8")).hexdigest() == digest, key


def test_repairPromptSharesTheGeneratorRulesWordForWord():
    prompts = _prompts()
    generator = prompts["sqlChartGeneratorPrompt"]
    repair = prompts["sqlChartRepairPrompt"]

    generatorRules = generator[generator.index("## Output\n") : generator.index("\n## Worked examples\n")]
    repairRules = repair[repair.index("## Output\n") : repair.index("\n## Example\n")]

    assert len(generatorRules) > 5000
    assert repairRules == generatorRules


def test_examplesAreValidChartRequests():
    text = _formatted("sqlChartGeneratorPrompt")
    tables = _exampleTables()

    # The examples' schema is shown exactly as the real tables will be.
    assert groundingText(EXAMPLE_METADATA, EXAMPLE_SCHEMAS) in text

    answers = _answers(text)
    requests = [ChartRequest.model_validate(answer) for answer in answers]
    assert sorted(request.chartType for request in requests) == sorted(CHART_TYPES)
    for request in requests:
        _runExample(request, tables)

    repairAnswers = _answers(_formatted("sqlChartRepairPrompt"))
    assert repairAnswers
    for answer in repairAnswers:
        _runExample(ChartRequest.model_validate(answer), tables)


MONTHS_2024 = [f"2024-{month:02d}" for month in range(1, 13)]
CATEGORY_REVENUE_2024 = (
    'SELECT p."category" AS category, SUM(oi."quantity" * oi."unit_price") AS revenue FROM "order_items" AS oi '
    'JOIN "products" AS p ON p."product_id" = oi."product_id" JOIN "orders" AS o ON o."order_id" = oi."order_id" '
    "WHERE o.\"order_date\" >= DATE '2024-01-01' AND o.\"order_date\" < DATE '2025-01-01' GROUP BY ALL"
)


def test_limitCutsATimeAxisSoNoTimeAxisOrLongFormExampleUsesIt():
    tables = _exampleTables()
    # A size rule applied as LIMIT on a daily axis keeps the first 50 days and drops the rest without an error.
    daily = ChartRequest(
        chartType="line",
        title="Orders per Day",
        sql="SELECT strftime(\"order_date\", '%Y-%m-%d') AS day, COUNT(*) AS order_count FROM \"orders\" "
        "WHERE \"order_date\" >= DATE '2024-01-01' GROUP BY ALL ORDER BY day LIMIT 50",
        encoding={"x": "day", "y": ["order_count"]},
    )
    assert _runExample(daily, tables)["data"]["labels"][-1] == "2024-02-19"

    for request in _examples():
        if request.chartType in ("line", "area", "pivot") or (request.chartType in CATEGORY_TYPES and request.encoding.series):
            assert not _hasOuterLimit(request.sql), request.chartType


def test_histogramsAreNeverCutOnlyWidened():
    tables = _exampleTables()
    orders = tables["orders"]
    # Spread the order values over 0..1000, a quarter of them bunched at 500..560, so narrow bins outnumber
    # the 50-row cut and have uneven counts.
    values = [(i * 37) % 1000 if i % 4 else 500 + i % 60 for i in range(orders.num_rows)]
    spread = pa.array([Decimal(value) + Decimal("0.25") for value in values], pa.decimal128(12, 2))
    tables = {**tables, "orders": orders.set_column(orders.schema.get_field_index("total_amount"), "total_amount", spread)}
    totalOrders = _query('SELECT COUNT(*) FROM "orders" WHERE "total_amount" IS NOT NULL', tables)[0][0]
    narrow = (
        'WITH binned AS (SELECT CAST(floor("total_amount" / 5) AS INTEGER) AS bin FROM "orders" '
        'WHERE "total_amount" IS NOT NULL) SELECT bin, COUNT(*) AS order_count FROM binned GROUP BY bin'
    )
    allBins = _query(narrow + " ORDER BY bin", tables)
    assert len(allBins) > 50
    # The top-50 rule applied to a histogram: by count the bins lose their range order, by bin the upper tail goes.
    byCount = _query(narrow + " ORDER BY order_count DESC, bin LIMIT 50", tables)
    assert [row[0] for row in byCount] != sorted(row[0] for row in byCount)
    byBin = _query(narrow + " ORDER BY bin LIMIT 50", tables)
    assert sum(row[1] for row in byBin) < totalOrders

    # The rule's form: wider bins, every bin kept, in range order.
    histogram = _example("histogram")
    assert not _hasOuterLimit(histogram.sql)
    rows = _query(histogram.sql, tables)
    assert 1 < len(rows) <= 50
    assert sum(row[1] for row in rows) == totalOrders
    lows = [int(row[0].split("-")[0]) for row in rows]
    assert lows == sorted(lows)

    for key in ("sqlChartGeneratorPrompt", "sqlChartRepairPrompt"):
        text = _formatted(key)
        assert "A histogram is never cut: widen the bins so there are at most 50" in text, key
        # The top-50 cut names category axes only, never a time axis or a histogram.
        sizeRule = next(line for line in text.splitlines() if "at most 50 categories" in line)
        assert "histogram" not in sizeRule and "only when x is not a time" in sizeRule, key


def test_topCategoriesOfALongFormChartKeepEveryMonth():
    tables = _exampleTables()
    line = _example("line")
    assert line.encoding.series

    chart = _runExample(line, tables)

    top = [row[0] for row in _query(f"SELECT category FROM ({CATEGORY_REVENUE_2024}) ORDER BY revenue DESC, category LIMIT 3", tables)]
    assert chart["data"]["labels"] == MONTHS_2024
    assert sorted(dataset["label"] for dataset in chart["data"]["datasets"]) == sorted(top)
    assert all(value is not None for dataset in chart["data"]["datasets"] for value in dataset["data"])

    # The old shape: every category, cut to 50 rows. Five categories over twelve months is 60 rows, so the
    # last months lose some or all of their series.
    allCategories = (
        "SELECT strftime(date_trunc('month', o.\"order_date\"), '%Y-%m') AS month, p.\"category\" AS category, "
        'SUM(oi."quantity" * oi."unit_price") AS revenue FROM "order_items" AS oi '
        'JOIN "products" AS p ON p."product_id" = oi."product_id" JOIN "orders" AS o ON o."order_id" = oi."order_id" '
        "WHERE o.\"order_date\" >= DATE '2024-01-01' AND o.\"order_date\" < DATE '2025-01-01' "
        "GROUP BY ALL ORDER BY month, category"
    )
    assert len(_query(allCategories, tables)) == 60
    cut = _runExample(line.model_copy(update={"sql": allCategories + " LIMIT 50"}), tables)
    assert cut["data"]["labels"][-1] != "2024-12" or any(None in dataset["data"] for dataset in cut["data"]["datasets"])


def test_bubbleRadiusIsTheRawMeasure():
    tables = _exampleTables()

    chart = _runExample(_example("bubble"), tables)

    # The client scales r to a bubble size and shows the raw value in the tooltip.
    counts = _query('SELECT s."floor_area_sqm", COUNT(*) FROM "stores" AS s JOIN "orders" AS o ON o."store_id" = s."store_id" GROUP BY ALL', tables)
    points = [point for dataset in chart["data"]["datasets"] for point in dataset["data"]]
    assert sorted((point["x"], point["r"]) for point in points) == sorted(counts)


def test_shareChartsKeepTheWhole():
    tables = _exampleTables()
    total = _query('SELECT SUM("quantity" * "unit_price") FROM "order_items"', tables)[0][0]
    byProduct = (
        'SELECT p."product_name" AS product, SUM(oi."quantity" * oi."unit_price") AS revenue FROM "order_items" AS oi '
        'JOIN "products" AS p ON p."product_id" = oi."product_id" GROUP BY ALL'
    )
    doughnut = _example("doughnut")

    chart = _runExample(doughnut, tables)

    values = chart["data"]["datasets"][0]["data"]
    assert sum(values) == pytest.approx(total, abs=0.05)
    assert len(values) == 10 and chart["data"]["labels"][-1] == "Other"
    assert values[:9] == sorted(values[:9], reverse=True)
    # The old shape: the top 10 slices only. The client's percentages would then be shares of those 10.
    cut = _runExample(doughnut.model_copy(update={"sql": byProduct + " ORDER BY revenue DESC, product LIMIT 10"}), tables)
    assert sum(cut["data"]["datasets"][0]["data"]) < total - 1
    for request in _examples():
        if request.chartType in ("pie", "doughnut", "polarArea"):
            assert not _hasOuterLimit(request.sql), request.chartType


def test_pivotColumnsComeOutInOrderWhenDataIsSparse():
    tables = _exampleTables()
    sparse = _query(
        "SELECT * FROM \"orders\" WHERE NOT (\"store_id\" IN (1, 3) AND \"order_date\" BETWEEN DATE '2024-01-01' AND DATE '2024-03-31')",
        tables,
    )
    names = EXAMPLE_SCHEMAS["orders"].names
    tables["orders"] = pa.table({name: [row[index] for row in sparse] for index, name in enumerate(names)}, schema=EXAMPLE_SCHEMAS["orders"])
    pivot = _example("pivot")

    chart = _runExample(pivot, tables)

    # The client keeps the column keys in the order it gets them.
    assert list(chart["data"]) == ["Q1", "Q2", "Q3", "Q4"]
    rowsFirst = pivot.sql.replace("ORDER BY order_quarter, region", "ORDER BY region, order_quarter")
    assert rowsFirst != pivot.sql
    assert list(_runExample(pivot.model_copy(update={"sql": rowsFirst}), tables)["data"]) == ["Q2", "Q3", "Q4", "Q1"]


def test_tablesReturnEveryMatchingRow():
    tables = _exampleTables()
    table = _example("table")

    chart = _runExample(table, tables)

    assert not _hasOuterLimit(table.sql)
    assert len(chart["data"]) == _query('SELECT COUNT(DISTINCT "customer_id") FROM "orders"', tables)[0][0]
    for key in NEW_PROMPTS:
        assert "100 unless" not in _prompts()[key], key


def _withoutRows(tables: dict[str, pa.Table], name: str, where: str) -> dict[str, pa.Table]:
    connection = duckdb.connect()
    for tableName, table in tables.items():
        connection.register(tableName, table)
    kept = connection.execute(f'SELECT * FROM "{name}" WHERE NOT ({where})').fetch_arrow_table().cast(EXAMPLE_SCHEMAS[name])
    return {**tables, name: kept}


def test_examplesGiveTheSameChartWhateverTheRowOrder():
    tables = _exampleTables()
    reversedTables = {name: table.take(list(range(table.num_rows - 1, -1, -1))) for name, table in tables.items()}
    requests = _examples() + [ChartRequest.model_validate(answer) for answer in _answers(_formatted("sqlChartRepairPrompt"))]

    for request in requests:
        assert _runExample(request, tables) == _runExample(request, reversedTables), request.chartType


def test_missingPeriodsAreFilledWithZero():
    tables = _withoutRows(_exampleTables(), "orders", "month(\"order_date\") = 6 AND year(\"order_date\") = 2024")

    for chartType in ("line", "area"):
        chart = _runExample(_example(chartType), tables)
        assert chart["data"]["labels"] == MONTHS_2024, chartType
        june = MONTHS_2024.index("2024-06")
        for dataset in chart["data"]["datasets"]:
            assert dataset["data"][june] is not None, chartType

    # Without a grid of periods, a month with no orders disappears from the axis.
    plain = ChartRequest(
        chartType="line",
        title="Monthly Revenue",
        sql="SELECT strftime(\"order_date\", '%Y-%m') AS month, SUM(\"total_amount\") AS revenue FROM \"orders\" "
        "WHERE year(\"order_date\") = 2024 GROUP BY ALL ORDER BY month",
        encoding={"x": "month", "y": ["revenue"]},
    )
    assert "2024-06" not in _runExample(plain, tables)["data"]["labels"]


def test_geoMapSkipsUnreadableCoordinates():
    tables = _exampleTables()
    stores = tables["stores"]
    textual = stores.set_column(
        stores.schema.get_field_index("latitude"), "latitude", pa.array(["52.1", "", "53.4"], pa.string())
    )
    geoMap = _example("geoMap")

    chart = _runExample(geoMap, {**tables, "stores": textual})

    assert sorted(point["id"] for point in chart["map"]["data"]["points"]) == ["1", "3"]


@pytest.mark.parametrize("key", ["sqlChartGeneratorPrompt", "sqlChartRepairPrompt"])
def test_chartPromptsNameTheTimestampTypesAndAllowOtherFunctions(key):
    text = _formatted(key)

    assert "TIMESTAMP_S, TIMESTAMP_MS, TIMESTAMP_NS and TIMESTAMP WITH TIME ZONE are ordinary timestamps" in text
    assert "use only the DuckDB functions" not in text
    assert re.search(r"(?<!TRY_)CAST them AS DOUBLE", text) is None


def test_seriesOrderByTotalRunsAndPutsTheBiggestCategoryFirst():
    rule = "aggregate in a CTE, then ORDER BY SUM(m) OVER (PARTITION BY x) DESC, x, series"
    for key in ("sqlChartGeneratorPrompt", "sqlChartRepairPrompt"):
        assert rule in _formatted(key), key

    sql = (
        'WITH t AS (SELECT p."category" AS category, o."channel" AS channel, '
        'SUM(oi."quantity" * oi."unit_price") AS revenue FROM "order_items" AS oi '
        'JOIN "orders" AS o ON o."order_id" = oi."order_id" '
        'JOIN "products" AS p ON p."product_id" = oi."product_id" GROUP BY ALL) '
        "SELECT category, channel, revenue FROM t "
        "ORDER BY SUM(revenue) OVER (PARTITION BY category) DESC, category, channel"
    )
    checkSql(sql, EXAMPLE_SCHEMAS)
    rows = _query(sql, _exampleTables())

    totals: dict[str, float] = {}
    for category, _channel, revenue in rows:
        totals[category] = totals.get(category, 0.0) + float(revenue)
    order = list(dict.fromkeys(category for category, _channel, _revenue in rows))
    assert order == sorted(totals, key=lambda category: (-totals[category], category))
    assert len(order) == 5
    assert order != sorted(order)  # the total order is not the alphabetical one on this data


PINNED_TODAY = datetime.date(2024, 5, 15)  # a Wednesday
D = datetime.date

# (phrase whose rule line gives the expression, date_trunc unit, count, first day, last day) with today pinned.
RELATIVE_PERIODS = [
    ("last 30 days", None, None, D(2024, 4, 16), D(2024, 5, 15)),
    ("this month", "month", 1, D(2024, 5, 1), D(2024, 5, 15)),
    ("this month", "week", 1, D(2024, 5, 13), D(2024, 5, 15)),
    ("this month", "quarter", 1, D(2024, 4, 1), D(2024, 5, 15)),
    ("this month", "year", 1, D(2024, 1, 1), D(2024, 5, 15)),
    ("last month", "month", 1, D(2024, 4, 1), D(2024, 4, 30)),
    ("last month", "week", 1, D(2024, 5, 6), D(2024, 5, 12)),
    ("last month", "quarter", 1, D(2024, 1, 1), D(2024, 3, 31)),
    ("last month", "year", 1, D(2023, 1, 1), D(2023, 12, 31)),
    ("last 6 months", "month", 6, D(2023, 11, 1), D(2024, 4, 30)),
    ("last 6 months", "week", 4, D(2024, 4, 15), D(2024, 5, 12)),
    ("last 6 months", "quarter", 2, D(2023, 10, 1), D(2024, 3, 31)),
    ("last 6 months", "year", 3, D(2021, 1, 1), D(2023, 12, 31)),
]


def _periodRule(phrase: str) -> str:
    """The SQL condition the prompt's rule line gives for a relative-period phrase."""
    text = _formatted("sqlChartGeneratorPrompt")
    match = re.search(r'^\s*- "' + re.escape(phrase) + r'"[^\n]*?\): (.+)$', text, flags=re.MULTILINE)
    assert match, phrase
    return match.group(1).strip()


def _periodCondition(phrase: str, unit: str | None, count: int | None) -> str:
    """The rule's condition for one unit, built the way the rule says (week, quarter = 3 months, year)."""
    condition = _periodRule(phrase)
    if unit is None or unit == "month":
        return condition.replace("INTERVAL 6 MONTH", f"INTERVAL {count} MONTH") if count else condition
    sizes = {"week": (1, "WEEK"), "quarter": (3, "MONTH"), "year": (1, "YEAR")}
    multiple, name = sizes[unit]
    condition = condition.replace("'month'", f"'{unit}'")
    return re.sub(r"INTERVAL \d+ MONTH", f"INTERVAL {count * multiple} {name}", condition)


@pytest.mark.parametrize(("phrase", "unit", "count", "first", "last"), RELATIVE_PERIODS)
def test_relativePeriodsCountBackFromToday(phrase, unit, count, first, last):
    condition = _periodCondition(phrase, unit, count)
    # Today comes from the database at run time, so a refreshed chart moves forward.
    assert "CURRENT_DATE" in condition
    assert re.search(r"DATE '\d", condition) is None

    onOrders = condition.replace('"col"', '"order_date"')
    checkSql(f'SELECT COUNT(*) AS order_count FROM "orders" WHERE {onOrders}', EXAMPLE_SCHEMAS)

    pinned = condition.replace("CURRENT_DATE", f"DATE '{PINNED_TODAY.isoformat()}'")
    days = "unnest(generate_series(DATE '2020-01-01', DATE '2025-12-31', INTERVAL 1 DAY))"
    for column in (f"CAST({days} AS DATE)", f"{days} + INTERVAL 23 HOUR + INTERVAL 30 MINUTE", f"CAST({days} AS TIMESTAMP)"):
        sql = f'WITH t AS (SELECT {column} AS "col") SELECT MIN(CAST("col" AS DATE)), MAX(CAST("col" AS DATE)) FROM t WHERE {pinned}'
        assert _query(sql, {}) == [(first, last)], (phrase, unit, column)


@pytest.mark.parametrize("key", sorted(NEW_PROMPTS))
def test_noPromptAnchorsPeriodsOnTheData(key):
    text = _prompts()[key]

    assert "latest date" not in text and "latest 'order_date'" not in text
    assert "count back from today" in text


@pytest.mark.parametrize("key", ["sqlChartGeneratorPrompt", "sqlChartRepairPrompt"])
def test_relativePeriodTitlesNameThePeriodNotDates(key):
    text = _formatted(key)

    assert "Weeks start on Monday" in text
    assert "never the computed dates" in text


@pytest.mark.parametrize("key", ["sqlChartGeneratorPrompt", "sqlChartRepairPrompt"])
def test_chartPromptsCarryTheEncodingTable(key):
    assert ENCODING_TABLE in _formatted(key)


def _exampleSql() -> list[str]:
    repair = [ChartRequest.model_validate(answer) for answer in _answers(_formatted("sqlChartRepairPrompt"))]
    return [request.sql for request in _examples() + repair[-1:]]


def test_examplesQuoteEverySchemaTableAndColumn():
    columns = {name: set(schema.names) for name, schema in EXAMPLE_SCHEMAS.items()}

    for sql in _exampleSql():
        tree = sqlglot.parse_one(sql, read="duckdb")
        aliases: dict[str, str] = {}
        for table in tree.find_all(sqlglot.exp.Table):
            if table.name in columns:
                assert table.this.quoted, (table.name, sql)
                aliases[table.alias_or_name] = table.name
        for column in tree.find_all(sqlglot.exp.Column):
            if column.table in aliases:
                assert column.name in columns[aliases[column.table]], (column.sql(), sql)
                assert column.this.quoted, (column.sql(), sql)


def test_examplesNeverUsePivotAndReadOnlyTheirTables():
    for sql in _exampleSql():
        tree = sqlglot.parse_one(sql, read="duckdb")
        assert not list(tree.find_all(sqlglot.exp.Pivot)), sql
        checkSql(sql, EXAMPLE_SCHEMAS)


def test_examplesMatchTypedTextWhateverItsCase():
    tables = _exampleTables()
    products = tables["products"]
    index = products.schema.get_field_index("category")
    shouting = products.set_column(
        index, "category", pa.array([value.upper() for value in products.column("category").to_pylist()], pa.string())
    )

    bar = _example("bar")
    chart = _runExample(bar, tables)

    assert chart["data"]["labels"]
    assert _runExample(bar, {**tables, "products": shouting}) == chart


@pytest.mark.parametrize("key", sorted(NEW_PROMPTS))
def test_sqlPromptsMentionNoPython(key):
    text = _prompts()[key].lower()

    for word in ("fetch_data", "polars", "pandas", "print(", "final_df", "python"):
        assert word not in text, word


@pytest.mark.parametrize("key", ["sqlQueryRephraserAgentPrompt", "parallelSqlQueryRephraserAgentPrompt"])
def test_sqlRephrasersNameEveryChartType(key):
    text = _prompts()[key]

    for chartType in CHART_TYPES:
        assert f"`{chartType}`" in text, chartType


def test_rephraserTakesAPromptKey(monkeypatch):
    from nubrix.components import queryRephraser

    prompts = _prompts()
    reply = '{"rephrasedOutput": "Bar chart of revenue by category.", "doubt": null}'
    monkeypatch.setattr(queryRephraser, "getGenaiLlm", lambda *args, **kwargs: FakeListChatModel(responses=[reply]))
    cases = (
        (queryRephraser.QueryRephaser, "queryRephraserAgentPrompt", "sqlQueryRephraserAgentPrompt"),
        (queryRephraser.ParallelQueryRephaser, "parallelQueryRephraserAgentPrompt", "parallelSqlQueryRephraserAgentPrompt"),
    )
    for rephraserClass, defaultKey, sqlKey in cases:
        assert rephraserClass().getQueryRephraserChain().first.template == prompts[defaultKey]
        assert rephraserClass(promptKey=sqlKey).getQueryRephraserChain().first.template == prompts[sqlKey]

    sqlChain = queryRephraser.QueryRephaser(promptKey="sqlQueryRephraserAgentPrompt").getQueryRephraserChain()
    assert sqlChain.invoke({"metadata": "{}", "query": "revenue by category"}) == {
        "rephrasedOutput": "Bar chart of revenue by category.",
        "doubt": None,
    }
    parallelChain = queryRephraser.ParallelQueryRephaser(promptKey="parallelSqlQueryRephraserAgentPrompt").getQueryRephraserChain()
    assert parallelChain.invoke({"metadata": "{}", "query": "revenue by category"}) == {
        "rephrasedOutput": "Bar chart of revenue by category."
    }


def test_jsonOutputSetsTheResponseMimeType(monkeypatch):
    from langchain_google_genai import ChatGoogleGenerativeAI

    from nubrix.components import llmChainFactory

    llm = ChatGoogleGenerativeAI(model="gemini-3.5-flash-lite", api_key="test-key")
    monkeypatch.setattr(llmChainFactory, "getGenaiLlm", lambda *args, **kwargs: llm)
    sent = {}

    def generateContent(**request):
        sent.update(request)
        raise RuntimeError("stop before the network")

    monkeypatch.setattr(llm.client.models, "generate_content", generateContent)

    chain = llmChainFactory.buildLlmChain("SQLCHARTGENERATOR", "sqlChartGeneratorPrompt", jsonOutput=True)
    bindings = [step for step in chain.steps if isinstance(step, RunnableBinding)]
    assert [binding.kwargs for binding in bindings] == [{"response_mime_type": "application/json"}]
    assert isinstance(chain.steps[-1], StrOutputParser)
    with pytest.raises(Exception):
        chain.invoke({"grounding": "g", "question": "q", "plan": "p"})
    assert sent["config"].response_mime_type == "application/json"
    # The cached model instance is not changed for other chains.
    assert llm.response_mime_type is None

    plain = llmChainFactory.buildLlmChain("SQLCHARTREPAIR", "sqlChartRepairPrompt")
    assert not [step for step in plain.steps if isinstance(step, RunnableBinding)]
    sent.clear()
    with pytest.raises(Exception):
        plain.invoke({"grounding": "g", "question": "q", "plan": "p", "previous_request": "r", "error": "e"})
    assert sent["config"].response_mime_type is None


def test_jsonOutputChainReturnsText(monkeypatch):
    from nubrix.components import llmChainFactory

    reply = '{"chartType": "card", "title": "t", "sql": "SELECT 1 AS n", "encoding": {"value": "n"}}'
    monkeypatch.setattr(llmChainFactory, "getGenaiLlm", lambda *args, **kwargs: FakeListChatModel(responses=[reply]))

    chain = llmChainFactory.buildLlmChain("SQLCHARTGENERATOR", "sqlChartGeneratorPrompt", jsonOutput=True)

    assert chain.invoke({"grounding": "g", "question": "q", "plan": "p"}) == reply


def test_sqlChartSectionsUseFlashLite():
    from nubrix.utils import getConfig

    config = getConfig(os.path.join(os.path.dirname(PROMPTS_PATH), "config.ini"))
    for section in ("SQLCHARTGENERATOR", "SQLCHARTREPAIR"):
        assert config.get(section, "model") == "gemini-3.5-flash-lite"
