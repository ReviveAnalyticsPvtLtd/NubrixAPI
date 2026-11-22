"""Metadata sample rows come from the stored table, never from the model, so an invented value can't pass as evidence."""

import datetime
import io
from decimal import Decimal
from unittest import mock

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from api.services import managementService as managementModule
from nubrix.components import sqlGrounding
from utils import storedTables
from utils.chartLabelUnits import cleanLabels, labelEvidence

INVENTED = {"sales": {"description": "d", "columns": [{"name": "sales"}], "sample_row": {"sales": "$120", "currency_note": "USD"}}}


def parquet(columns):
    sink = io.BytesIO()
    pq.write_table(pa.table(columns), sink)
    return sink.getvalue()


def generated(monkeypatch, outcome):
    service = object.__new__(managementModule.ManagementService)
    service.client = mock.Mock()
    service.metadataGenerator = mock.Mock()
    service._parseModelJsonOutput = mock.Mock(return_value={k: dict(v) for k, v in INVENTED.items()})
    monkeypatch.setattr(managementModule, "readTableBytes", lambda *_args: b"bytes")
    monkeypatch.setattr(managementModule, "storedTableInfo", outcome)
    return service._generateMetadata("p1", tableNames=["sales"])["sales"]


def info(**changes):
    return {"columns": [["sales", "double"]], "rows": 1, "cols": 1, "sample": "x", "sampleFailed": False,
            "sampleValues": {"sales": 120.5}, **changes}


def test_the_models_sample_row_is_replaced_by_the_real_one(monkeypatch):
    table = generated(monkeypatch, lambda *_args: info())
    assert table["sample_row"] == {"sales": 120.5}
    assert cleanLabels({"title": "Sales (USD)"}, labelEvidence("sales", {"sales": table})) == {"title": "Sales"}


@pytest.mark.parametrize("changes", [{"sample": None, "sampleValues": None},
                                     {"sample": None, "sampleFailed": True, "sampleValues": None}])
def test_a_table_without_a_readable_row_gets_no_invented_sample(monkeypatch, changes):
    assert generated(monkeypatch, lambda *_args: info(**changes))["sample_row"] == {}


def test_a_table_that_cannot_be_read_gets_no_invented_sample(monkeypatch):
    def fail(*_args):
        raise RuntimeError("unreadable")

    assert generated(monkeypatch, fail)["sample_row"] == {}


def test_table_info_gives_the_first_row_as_plain_json_values():
    data = parquet({"n": [7, 8], "price": [-1.5, 2.0], "ok": [True, False], "gap": [None, "x"], "bad": [float("nan"), 1.0],
                    "day": [datetime.date(2024, 1, 2), datetime.date(2024, 1, 3)], "exact": [Decimal("-5.10"), Decimal("1.00")]})
    assert storedTables.tableInfo(data, 0)["sampleValues"] == {
        "n": 7, "price": -1.5, "ok": True, "gap": None, "bad": None, "day": "2024-01-02", "exact": "-5.10"}


def test_a_cut_sample_is_still_long_enough_for_the_grounding_to_mark_it_as_cut():
    assert storedTables.MAX_SAMPLE_VALUE_CHARS > sqlGrounding.SAMPLE_VALUE_CAP


def test_table_info_bounds_the_sample_values():
    long = storedTables.tableInfo(parquet({"note": ["x" * 5000]}), 0)["sampleValues"]["note"]
    assert long == "x" * storedTables.MAX_SAMPLE_VALUE_CHARS
    wide = storedTables.tableInfo(parquet({f"c{i}": ["y" * 100] for i in range(2000)}), 0)["sampleValues"]
    assert 0 < sum(len(name) + len(value) for name, value in wide.items()) <= storedTables.MAX_SAMPLE_CHARS


@pytest.mark.parametrize("values", [[], "text", {"a": {"b": 1}}, {"a": [1]}, {"a": "x" * 5000}, {"a": float("nan")}])
def test_the_parent_refuses_a_child_answer_with_other_sample_values(values):
    assert storedTables._isInfo(info())
    assert not storedTables._isInfo(info(sampleValues=values))
