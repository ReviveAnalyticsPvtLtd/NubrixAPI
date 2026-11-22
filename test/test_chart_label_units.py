"""The label check removes a currency the question and the data never named, and leaves everything else alone."""

import json
import logging

import pytest

from utils.chartLabelUnits import hasCurrency, labelEvidence, removeUnsupportedCurrency

NO_CURRENCY = {"sales": {"description": "Sales in USD", "columns": [{"name": "order_date"}, {"name": "sales"}],
                         "sample_row": {"order_date": "2024-01-02", "sales": 120.5}}}
USD_COLUMN = {"sales": {"columns": [{"name": "sales"}, {"name": "currency"}], "sample_row": {"sales": 1, "currency": "USD"}}}


def line_chart(title="Monthly Sales Trend", y="Total Sales (in USD)", dataset="Total Sales (in USD)"):
    return {"chartType": "line", "title": title, "xLabels": "Month", "yLabels": y,
            "data": {"labels": ["2024-01"], "datasets": [{"label": dataset, "data": [120.5]}]}}


def cleaned(chart, question="monthly sales trend", metadata=NO_CURRENCY):
    removeUnsupportedCurrency(chart, labelEvidence(question, metadata), ref="t")
    return chart


def test_a_currency_with_no_evidence_is_removed_from_every_label():
    chart = cleaned(line_chart())
    assert (chart["yLabels"], chart["data"]["datasets"][0]["label"]) == ("Total Sales", "Total Sales")
    assert chart["data"]["datasets"][0]["data"] == [120.5]


def test_a_table_description_is_not_evidence():
    # NO_CURRENCY's description says USD; the AI writes descriptions, so a guess there would look like proof.
    assert cleaned(line_chart())["yLabels"] == "Total Sales"


def test_a_currency_sample_value_is_evidence():
    assert cleaned(line_chart(), metadata=USD_COLUMN) == line_chart()


def test_a_currency_in_the_question_is_kept():
    chart = cleaned(line_chart(y="Revenue (INR)"), question="monthly revenue in INR")
    assert chart["yLabels"] == "Revenue (INR)"


def test_a_column_name_is_evidence():
    metadata = {"orders": {"columns": [{"name": "price_inr"}], "sample_row": {}}}
    assert cleaned(line_chart(y="Price (₹)"), metadata=metadata)["yLabels"] == "Price (₹)"


def test_a_word_names_the_same_currency_as_its_symbol():
    assert cleaned(line_chart(y="Revenue ($)"), question="revenue in dollars")["yLabels"] == "Revenue ($)"


@pytest.mark.parametrize("label, expected", [
    ("Revenue ($)", "Revenue"),
    ("Sales in USD", "Sales"),
    ("USD Revenue by Month", "Revenue by Month"),
    ("Total Revenue: $1M", "Total Revenue: 1M"),
    ("Sales in Dollars", "Sales"),
    ("Price per unit, USD", "Price per unit"),
    ("Revenue (USD, millions)", "Revenue (millions)"),
    ("Spend ($ millions)", "Spend (millions)"),
    ("Total Spend Usd", "Total Spend"),
    ("Amount (Rs.)", "Amount"),
    ("Ticket price (R$)", "Ticket price"),
    ("Cost in €", "Cost"),
])
def test_wrappers_leave_no_empty_brackets_or_dangling_words(label, expected):
    assert cleaned(line_chart(y=label))["yLabels"] == expected


@pytest.mark.parametrize("label", ["Orders in RS", "Revenue in rs region", "Production (cadence)", "Used units", "Rsvp count", "Share of sales (%)", "Weight (kg)",
                                   "Price", "Cost over plan"])
def test_words_that_only_contain_a_code_and_other_units_stay(label):
    assert cleaned(line_chart(y=label), question="anything")["yLabels"] == label


def test_a_card_label_and_a_text_value_lose_the_currency():
    card = {"chartType": "card", "title": "Total Revenue", "label": "Total Revenue ($)", "data": "$1.2M"}
    assert cleaned(card) == {"chartType": "card", "title": "Total Revenue", "label": "Total Revenue", "data": "1.2M"}


def test_a_title_made_only_of_a_currency_is_kept():
    assert cleaned(line_chart(title="USD"))["title"] == "USD"


def test_each_removal_is_logged_with_the_token_and_never_the_data(caplog):
    with caplog.at_level(logging.INFO):
        removeUnsupportedCurrency(line_chart(), labelEvidence("sales", NO_CURRENCY), ref="trace-1")
    lines = [r.getMessage() for r in caplog.records if "chart_label_currency" in r.getMessage()]
    assert len(lines) == 1 and "trace-1" in lines[0] and "USD" in lines[0] and "120.5" not in lines[0]


@pytest.mark.parametrize("value", [None, "text", [line_chart()], {"error": "Endpoint says: boom"}, {"response": "Which year?"},
                                   {"chartType": "bar", "data": None, "title": None, "yLabels": ["a", 1]}])
def test_other_shapes_never_raise_and_never_change(value):
    before = json.dumps(value)
    removeUnsupportedCurrency(value, labelEvidence(None, None), ref=None)
    assert json.dumps(value) == before


def test_evidence_accepts_metadata_as_json_text():
    assert "currency" in labelEvidence("q", json.dumps(USD_COLUMN)) and "usd" in labelEvidence("q", json.dumps(USD_COLUMN))
    assert labelEvidence("q", "not json") == labelEvidence("q", None)


def test_has_currency_looks_only_at_label_text():
    assert hasCurrency(line_chart())
    assert not hasCurrency(line_chart(y="Total Sales", dataset="Total Sales"))
    assert not hasCurrency({"chartType": "table", "title": "Orders", "columns": ["price_usd"], "data": [{"price_usd": 1}]})


# Where the check runs: every place a chart reaches the user.

from unittest.mock import Mock  # noqa: E402

from api.models import GetData  # noqa: E402
from api.services.dashboardService import DashboardService  # noqa: E402
from nubrix.workflows.parallelReportingToolWorkflow import ParallelReportingToolWorkflow  # noqa: E402
from nubrix.workflows.reportingToolWorkflow import ReportingToolWorkflow  # noqa: E402
from test.storageListingFixtures import PROJECT_ID  # noqa: E402
from test.test_sql_chart_workflow import Harness  # noqa: E402


@pytest.mark.parametrize("workflow", [ReportingToolWorkflow, ParallelReportingToolWorkflow])
def test_python_charts_lose_an_unsupported_currency(workflow):
    formatter = object.__new__(workflow)
    state = {"codeOutput": json.dumps(line_chart()), "inputQuery": "monthly sales trend", "metadata": json.dumps(NO_CURRENCY)}
    assert formatter._formatJsonResponse(state)["finalOutput"]["yLabels"] == "Total Sales"
    state["metadata"] = json.dumps(USD_COLUMN)
    assert formatter._formatJsonResponse(state)["finalOutput"]["yLabels"] == "Total Sales (in USD)"


def test_sql_charts_lose_an_unsupported_currency_before_the_response_is_written(monkeypatch):
    harness = Harness(monkeypatch)
    harness.render.return_value = line_chart()
    state = harness.run()
    assert state["finalOutput"]["yLabels"] == "Total Sales"
    assert json.loads(state["codeOutput"]) == state["finalOutput"]


def test_sql_charts_keep_a_currency_the_data_names(monkeypatch):
    harness = Harness(monkeypatch)
    harness.render.return_value = line_chart()
    assert harness.run(metadata=USD_COLUMN)["finalOutput"]["yLabels"] == "Total Sales (in USD)"


def _dashboard(monkeypatch, widgets, metadata):
    files = {"dashboardConfig.json": {"main": {"name": "Main", "widgets": widgets}}, "metadata.json": metadata}
    reads = []

    def readJson(projectId, name):
        reads.append(name)
        if isinstance(files[name], Exception):
            raise files[name]
        return json.loads(json.dumps(files[name]))

    monkeypatch.setattr("api.services.dashboardService.readJson", readJson)
    service = object.__new__(DashboardService)
    service.client = Mock()
    return service.getData(GetData(projectId=PROJECT_ID, page="main")), reads


def test_dashboard_shows_saved_charts_without_an_unsupported_currency(monkeypatch):
    page, reads = _dashboard(monkeypatch, [{"id": "w1", "query": "monthly sales", **line_chart()}], NO_CURRENCY)
    assert page["widgets"][0]["yLabels"] == "Total Sales"
    assert reads == ["dashboardConfig.json", "metadata.json"]


def test_dashboard_keeps_a_currency_the_data_names(monkeypatch):
    page, _ = _dashboard(monkeypatch, [{"id": "w1", "query": "monthly sales", **line_chart()}], USD_COLUMN)
    assert page["widgets"][0]["yLabels"] == "Total Sales (in USD)"


def test_dashboard_reads_no_metadata_when_no_label_names_a_currency(monkeypatch):
    plain = {"id": "w1", "query": "monthly sales", **line_chart(y="Total Sales", dataset="Total Sales")}
    page, reads = _dashboard(monkeypatch, [plain], NO_CURRENCY)
    assert reads == ["dashboardConfig.json"] and page["widgets"][0] == plain


def test_dashboard_falls_back_to_the_question_when_metadata_cannot_be_read(monkeypatch):
    widgets = [{"id": "w1", "query": "sales in USD", **line_chart()}, {"id": "w2", "query": "sales", **line_chart()}]
    page, _ = _dashboard(monkeypatch, widgets, OSError("storage down"))
    assert [w["yLabels"] for w in page["widgets"]] == ["Total Sales (in USD)", "Total Sales"]
