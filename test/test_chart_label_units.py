"""The label check removes a currency the question and the data never named from text the model wrote, and nothing else.

It edits the model's chart request or code before it runs, never a chart, so values and names from the data can't change
and a dashboard refresh shows the same labels.
"""

import contextlib
import io
import json
import logging
from unittest.mock import Mock

import pyarrow as pa
import pytest

from api.models import GetData
from api.services.dashboardService import DashboardService
from nubrix.components.chartBuilder import buildChart
from nubrix.components.chartRequest import decodeGeneratedCode
from nubrix.workflows.parallelReportingToolWorkflow import ParallelReportingToolWorkflow
from nubrix.workflows.reportingToolWorkflow import ReportingToolWorkflow
from test.storageListingFixtures import PROJECT_ID
from test.test_sql_chart_workflow import Harness
from utils.chartLabelUnits import cleanCode, cleanLabels, labelEvidence, labelTexts

NO_CURRENCY = {"sales": {"description": "Sales in USD", "columns": [{"name": "order_date"}, {"name": "sales"}],
                         "sample_row": {"order_date": "2024-01-02", "sales": 120.5}}}
USD_SAMPLE = {"sales": {"columns": [{"name": "sales"}, {"name": "unit"}], "sample_row": {"sales": 1, "unit": "USD"}}}
CURRENCY_COLUMN = {"sales": {"columns": [{"name": "sales"}, {"name": "currency"}], "sample_row": {"sales": 1, "currency": "INR"}}}


def cleaned(label, question="monthly sales trend", metadata=NO_CURRENCY):
    return cleanLabels({"title": label}, labelEvidence(question, metadata), ref="t").get("title", label)


# What counts as evidence.


def test_a_table_description_is_not_evidence():
    # NO_CURRENCY's description says USD; the AI writes descriptions, so a guess there would look like proof.
    assert cleaned("Total Sales (in USD)") == "Total Sales"


def test_a_currency_sample_value_is_evidence():
    assert cleaned("Total Sales (in USD)", metadata=USD_SAMPLE) == "Total Sales (in USD)"


def test_a_currency_in_the_question_is_kept():
    assert cleaned("Revenue (INR)", question="monthly revenue in INR") == "Revenue (INR)"


def test_a_column_name_is_evidence():
    assert cleaned("Price (₹)", metadata={"orders": {"columns": [{"name": "price_inr"}]}}) == "Price (₹)"


@pytest.mark.parametrize("column", ["currency", "currency_code", "txn_ccy", "Currency"])
def test_a_currency_column_allows_every_currency_because_one_sample_row_cannot_list_them(column):
    metadata = {"sales": {"columns": [{"name": column}], "sample_row": {column: "INR"}}}
    assert cleaned("Revenue EUR", metadata=metadata) == "Revenue EUR"


def test_a_word_names_the_same_currency_as_its_symbol():
    assert cleaned("Revenue ($)", question="revenue in dollars") == "Revenue ($)"


def test_evidence_accepts_metadata_as_json_text():
    assert "usd" in labelEvidence("q", json.dumps(USD_SAMPLE))
    assert labelEvidence("q", "not json") == labelEvidence("q", None)


# What a removal does to a label.


@pytest.mark.parametrize("label, expected", [
    ("Revenue ($)", "Revenue"),
    ("Revenue [$]", "Revenue"),
    ("Sales in USD", "Sales"),
    ("USD Revenue by Month", "Revenue by Month"),
    ("USD - Revenue", "Revenue"),
    ("Total Revenue: $1M", "Total Revenue: 1M"),
    ("Sales in Dollars", "Sales"),
    ("Sales in dollars by month", "Sales by month"),
    ("Revenue in millions of dollars", "Revenue in millions"),
    ("Revenue in euros (millions)", "Revenue (millions)"),
    ("Price per unit, USD", "Price per unit"),
    ("Revenue (USD, millions)", "Revenue (millions)"),
    ("Spend ($ millions)", "Spend (millions)"),
    ("Total Spend Usd", "Total Spend"),
    ("Amount (Rs.)", "Amount"),
    ("Amount (INR)", "Amount"),
    ("Ticket price (R$)", "Ticket price"),
    ("Cost in €", "Cost"),
    ("TOTAL SALES$", "TOTAL SALES"),
])
def test_wrappers_leave_no_empty_brackets_or_dangling_words(label, expected):
    assert cleaned(label, question="totals", metadata={}) == expected


@pytest.mark.parametrize("label", [
    "Orders in RS", "Revenue in rs region", "Production (cadence)", "Used units", "Rsvp count", "Share of sales (%)",
    "Weight (kg)", "Price", "Cost over plan",
    # Words that are also currencies, where they are not used as a unit.
    "CAD software licences", "Patients with CAD", "Average INR by patient", "AED locations by site", "CNY holiday orders",
    "Dollar General", "Euro Foods", "Sales at Dollar General", "Trade in Euro Area", "Yuan Li's orders",
])
def test_ordinary_words_names_and_other_units_stay(label):
    assert cleaned(label, question="anything", metadata={}) == label


@pytest.mark.parametrize("label, expected", [
    ("-$500", "-500"), ("$-500", "-500"), ("-₹1,200", "-1,200"), ("$500-", "500-"), ("-500 USD", "-500"),
    ("($500)", "(500)"), ("Change: -$500.00", "Change: -500.00"), ("$.5", ".5"),
    ("500₹1,200", "500₹1,200"), ("500$.5", "500$.5"), ("1,200€₹500", "1,200€₹500"), ("500[$]500", "500[$]500"),
])
def test_a_sign_is_never_lost(label, expected):
    assert cleaned(label, question="net profit", metadata={}) == expected


def test_a_title_made_only_of_a_currency_is_kept():
    assert cleaned("USD") == "USD"


def test_series_are_left_alone_when_cleaning_would_give_two_the_same_name():
    labels = {"a": "Revenue (EUR)", "b": "Revenue (USD)", "title": "Revenue in USD"}
    changed = cleanLabels(labels, labelEvidence("revenue", NO_CURRENCY), series=("a", "b"))
    assert changed == {"title": "Revenue"}


def test_each_removal_is_logged_with_the_token_and_never_the_data(caplog):
    with caplog.at_level(logging.INFO):
        cleanLabels({"title": "Acme sales 120.5 (USD)"}, labelEvidence("sales", NO_CURRENCY), ref="trace-1")
    lines = [r.getMessage() for r in caplog.records if "chart_label_currency" in r.getMessage()]
    assert len(lines) == 1 and "trace-1" in lines[0] and "USD" in lines[0]
    assert "120.5" not in lines[0] and "Acme" not in lines[0]


def test_label_texts_reports_everything_shown_including_what_is_never_edited():
    chart = {"chartType": "card", "title": "Net", "label": "Net ($)", "data": "-$500"}
    assert labelTexts(chart) == ["Net", "Net ($)", "-$500"]


# The SQL engine: the request is cleaned before it is rendered and saved.


def sql_run(monkeypatch, request, table, metadata=NO_CURRENCY, question="totals"):
    harness = Harness(monkeypatch)
    harness.generator.invoke.return_value = json.dumps({"sql": "SELECT 1", **request})
    harness.render.side_effect = lambda manager, projectId, chartRequest, *args, **kwargs: buildChart(chartRequest, pa.table(table))
    return harness.graph.invoke({"metadata": metadata, "inputQuery": question, "projectId": "p1"})


BAR = {"chartType": "bar", "title": "Revenue in USD", "xLabel": "Store", "yLabel": "Revenue (USD)",
       "encoding": {"x": "store", "y": ["revenue"]}, "datasetLabels": {"revenue": "Revenue ($)"}}


def test_sql_labels_the_model_wrote_lose_the_currency_in_the_chart_and_in_what_is_saved(monkeypatch):
    table = {"store": ["A"], "revenue": [5]}
    state = sql_run(monkeypatch, BAR, table)
    chart = state["finalOutput"]
    assert (chart["title"], chart["yLabels"], chart["data"]["datasets"][0]["label"]) == ("Revenue", "Revenue", "Revenue")
    assert json.loads(state["codeOutput"]) == chart
    assert buildChart(decodeGeneratedCode(state["generatedCode"]), pa.table(table)) == chart  # a refresh


def test_sql_labels_keep_a_currency_the_data_names(monkeypatch):
    state = sql_run(monkeypatch, BAR, {"store": ["A"], "revenue": [5]}, metadata=USD_SAMPLE)
    assert state["finalOutput"]["yLabels"] == "Revenue (USD)"


def test_sql_default_labels_made_from_the_models_alias_are_cleaned(monkeypatch):
    request = {"chartType": "bar", "title": "Revenue", "encoding": {"x": "store", "y": ["revenue_usd"]}}
    chart = sql_run(monkeypatch, request, {"store": ["A"], "revenue_usd": [5]})["finalOutput"]
    assert (chart["yLabels"], chart["data"]["datasets"][0]["label"]) == ("Revenue", "Revenue")


def test_sql_series_names_from_the_data_are_never_changed(monkeypatch):
    names = ["Euro Foods", "Foods", "Revenue EUR", "Sales (USD)"]
    request = {"chartType": "bar", "title": "Sales (USD)", "encoding": {"x": "store", "y": ["sales"], "series": "company"}}
    table = {"store": ["A"] * 4, "company": names, "sales": [1, 2, 3, 4]}
    chart = sql_run(monkeypatch, request, table)["finalOutput"]
    assert chart["title"] == "Sales"
    assert [dataset["label"] for dataset in chart["data"]["datasets"]] == names


@pytest.mark.parametrize("value", ["-$500", "$-500", "-₹1,200", "-500 USD"])
def test_sql_card_values_are_never_changed(monkeypatch, value):
    request = {"chartType": "card", "title": "Net profit", "encoding": {"value": "net", "label": "Net profit ($)"}}
    chart = sql_run(monkeypatch, request, {"net": [value]})["finalOutput"]
    assert (chart["label"], chart["data"]) == ("Net profit", value)


def test_a_repaired_sql_request_is_cleaned_too(monkeypatch):
    harness = Harness(monkeypatch)
    harness.generator.invoke.return_value = "not json"
    harness.repair.invoke.return_value = json.dumps({"sql": "SELECT 1", **BAR})
    harness.render.side_effect = lambda manager, projectId, request, *args, **kwargs: buildChart(request, pa.table({"store": ["A"], "revenue": [5]}))
    assert harness.run(metadata=NO_CURRENCY)["finalOutput"]["yLabels"] == "Revenue"


# The Python engine: label literals in the code are cleaned before it runs and is saved.


PROGRAM = '''import json
name = "Euro Foods"
amount = "-$500"
final_output = {
    "chartType": "bar",
    "title": "Net profit in USD",
    "xLabels": "Company — ₹",
    "yLabels": "Net profit (USD)",
    "data": {
        "labels": ["Euro Foods"],
        "datasets": [{"label": "Net profit ($)", "data": [amount]}, {"label": name, "data": [1]}],
    },
    "points": [{"id": 1, "label": "Pune: $42"}],
}
print(json.dumps(final_output))'''


def printed(code):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        exec(code, {})  # the test's own program
    return json.loads(out.getvalue())


def test_python_code_cleans_only_the_label_literals():
    chart = printed(cleanCode(PROGRAM, labelEvidence("net profit", NO_CURRENCY)))
    assert (chart["title"], chart["xLabels"], chart["yLabels"]) == ("Net profit", "Company —", "Net profit")
    assert chart["data"]["labels"] == ["Euro Foods"]
    assert chart["data"]["datasets"] == [{"label": "Net profit", "data": ["-$500"]}, {"label": "Euro Foods", "data": [1]}]
    assert chart["points"] == [{"id": 1, "label": "Pune: $42"}]


def test_python_card_values_are_never_changed():
    code = 'import json\nprint(json.dumps({"chartType": "card", "label": "Net ($)", "data": "-$500"}))'
    assert printed(cleanCode(code, "")) == {"chartType": "card", "label": "Net", "data": "-$500"}


def test_python_code_inside_fences_is_cleaned():
    code = cleanCode(f"```python\n{PROGRAM}\n```", "")
    assert "Net profit in USD" not in code and code.startswith("```python") and code.endswith("```")


@pytest.mark.parametrize("code", ["not python (", "", None, 'x = {"title": "Sales (USD)"}', 'print("Sales in USD")',
                                  "x = " + "-" * 100000 + "1"], ids=["syntax", "empty", "none", "noChart", "print", "tooDeep"])
def test_code_without_chart_label_literals_is_returned_unchanged(code):
    assert cleanCode(code, "") == code


@pytest.mark.parametrize("node", ["_generateCode", "_codeDebugger"])
@pytest.mark.parametrize("workflow", [ReportingToolWorkflow, ParallelReportingToolWorkflow])
def test_python_workflows_clean_the_code_they_run_and_save(workflow, node):
    instance = object.__new__(workflow)
    instance.codeGeneratorChain = instance.codeDebuggerChain = Mock(invoke=Mock(return_value=PROGRAM))
    state = {"projectId": "p1", "inputQuery": "net profit", "metadata": NO_CURRENCY, "rephrasedQuery": "r",
             "generatedCode": "x", "codeOutput": "err"}
    assert "Net profit (USD)" not in getattr(instance, node)(dict(state))["generatedCode"]
    kept = getattr(instance, node)({**state, "metadata": USD_SAMPLE})["generatedCode"]
    assert "Net profit (USD)" in kept


@pytest.mark.parametrize("workflow", [ReportingToolWorkflow, ParallelReportingToolWorkflow])
def test_python_output_is_not_edited_after_it_ran(workflow):
    output = {"chartType": "card", "title": "Net", "label": "Dollar General ($)", "data": "-$500"}
    formatter = object.__new__(workflow)
    state = {"codeOutput": json.dumps(output), "inputQuery": "net", "metadata": NO_CURRENCY}
    assert formatter._formatJsonResponse(state)["finalOutput"] == output


# Dashboards show what was saved: a widget often has no question to check a currency against.


def test_a_dashboard_never_edits_a_saved_widget(monkeypatch):
    widgets = [{"id": "w1", "chartType": "card", "title": "Sales in dollars", "label": "Sales (USD)", "data": "-$500"}]
    reads = []

    def readJson(projectId, name):
        reads.append(name)
        return json.loads(json.dumps({"main": {"name": "Main", "widgets": widgets}})) if name == "dashboardConfig.json" else {}

    monkeypatch.setattr("api.services.dashboardService.readJson", readJson)
    service = object.__new__(DashboardService)
    service.client = Mock()
    assert service.getData(GetData(projectId=PROJECT_ID, page="main"))["widgets"] == widgets
    assert reads == ["dashboardConfig.json"]
