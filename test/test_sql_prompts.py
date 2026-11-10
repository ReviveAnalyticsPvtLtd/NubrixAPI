"""The SQL chart prompts: they load and format, their worked examples are valid and run, and the chains are wired.

The worked examples are written against an invented retail schema (EXAMPLE_SCHEMAS below). The generator prompt
shows that schema exactly as `groundingText` prints it, and every example must validate, pass `checkSql`, run in
DuckDB over a few invented rows and build a chart the client can read.
"""

import datetime
import json
import os
import re
from decimal import Decimal

import duckdb
import pyarrow as pa
import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import PromptTemplate
from langchain_core.runnables import RunnableBinding

from nubrix.components.chartBuilder import buildChart
from nubrix.components.chartContract import contractErrors
from nubrix.components.chartRequest import CHART_TYPES, ChartRequest, requiredColumns
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
    """A few deterministic rows per table, enough for every example to return a non-empty result."""
    stores = {
        "store_id": [1, 2, 3],
        "store_name": ["Riverside", "Hilltop", "Harbour"],
        "region": ["North", "South", "North"],
        "latitude": [52.1, 50.8, 53.4],
        "longitude": [-1.5, -0.1, -3.0],
        "floor_area_sqm": [850.0, 1200.0, 640.0],
    }
    customers = {
        "customer_id": [1, 2, 3, 4],
        "full_name": ["Maya Lin", "Tom Reed", "Ana Ruiz", "Li Wei"],
        "segment": ["Consumer", "Corporate", "Consumer", "Small Business"],
        "city": ["Springfield", "Lakeside", "Springfield", "Hillview"],
        "signup_date": [datetime.date(2023, 1, 5), datetime.date(2023, 6, 1), datetime.date(2024, 2, 9), datetime.date(2024, 4, 20)],
    }
    products = {
        "product_id": [1, 2, 3, 4, 5],
        "product_name": ["Desk Lamp", "Headphones", "Garden Hose", "Puzzle", "Smart Speaker"],
        "category": ["Home & Garden", "Electronics", "Home & Garden", "Toys", "Electronics"],
        "list_price": [59.95, 89.0, 24.5, 15.0, 129.0],
    }
    orders: dict[str, list] = {name: [] for name in EXAMPLE_SCHEMAS["orders"].names}
    items: dict[str, list] = {name: [] for name in EXAMPLE_SCHEMAS["order_items"].names}
    channels = ["Online", "In-Store"]
    statuses = ["Delivered", "Shipped", "Cancelled", "Delivered"]
    for number in range(1, 31):
        year = 2023 if number <= 6 else 2024
        month = (number % 12) + 1
        productA, productB = (number % 5) + 1, ((number + 2) % 5) + 1
        quantityA, quantityB = (number % 3) + 1, 1
        amount = quantityA * products["list_price"][productA - 1] + quantityB * products["list_price"][productB - 1]
        discount = 5.0 if number % 4 == 0 else 0.0
        orders["order_id"].append(1000 + number)
        orders["customer_id"].append((number % 4) + 1)
        orders["store_id"].append((number % 3) + 1)
        orders["order_date"].append(datetime.date(year, month, (number % 27) + 1))
        orders["channel"].append(channels[number % 2])
        orders["status"].append(statuses[number % 4])
        orders["total_amount"].append(Decimal(f"{amount - discount:.2f}"))
        orders["discount_amount"].append(discount)
        for product, quantity in ((productA, quantityA), (productB, quantityB)):
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


@pytest.mark.parametrize("key", ["sqlChartGeneratorPrompt", "sqlChartRepairPrompt"])
def test_chartPromptsCarryTheEncodingTableAndRules(key):
    text = _formatted(key)

    assert ENCODING_TABLE in text
    for phrase in (
        "double quotes",
        "ILIKE",
        "lower(",
        "GROUP BY ALL",
        "date_trunc",
        "strftime",
        "date_diff",
        "QUALIFY",
        "TRY_CAST",
        "NULLIF",
        "COALESCE",
        "at most 50",
        "exactly one row",
        "PIVOT",
        "only a JSON object",
    ):
        assert phrase in text, phrase


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
