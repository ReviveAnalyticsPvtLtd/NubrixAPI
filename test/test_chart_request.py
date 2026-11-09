import pytest
from pydantic import ValidationError

from nubrix.components.chartRequest import (
    CATEGORY_TYPES,
    CHART_TYPES,
    LABEL_CAP,
    ChartRequest,
    decodeGeneratedCode,
    encodeGeneratedCode,
    requiredColumns,
    rowLimit,
)
from nubrix.components.widgetProvenance import extractProvenance

MINIMAL_ENCODINGS = {
    "bar": {"x": "a", "y": ["b"]},
    "line": {"x": "a", "y": ["b", "c"]},
    "area": {"x": "a", "y": "b", "series": "s"},
    "histogram": {"x": "bin", "y": ["n"]},
    "radar": {"x": "a", "y": ["b"]},
    "pie": {"label": "a", "value": "b"},
    "doughnut": {"label": "a", "value": "b"},
    "polarArea": {"label": "a", "value": "b"},
    "scatter": {"x": "a", "y": ["b"]},
    "bubble": {"x": "a", "y": ["b"], "r": "c"},
    "card": {"value": "total"},
    "table": {},
    "pivot": {"rows": ["a"], "columns": ["b"], "value": "v"},
    "geoMap": {"lat": "la", "long": "lo"},
}


def makeRequest(chartType="bar", encoding=None, **extra):
    body = {
        "chartType": chartType,
        "title": "A title",
        "sql": "SELECT 1",
        "encoding": MINIMAL_ENCODINGS[chartType] if encoding is None else encoding,
    }
    body.update(extra)
    return ChartRequest.model_validate(body)


def test_chartTypeConstants():
    assert len(CHART_TYPES) == 14
    assert set(CATEGORY_TYPES) < set(CHART_TYPES)
    assert LABEL_CAP == 500


@pytest.mark.parametrize("chartType", CHART_TYPES)
def test_everyChartTypeAcceptsItsMinimalEncoding(chartType):
    request = makeRequest(chartType)
    assert request.chartType == chartType


def test_singleYStringBecomesList():
    assert makeRequest("bar", {"x": "a", "y": "b"}).encoding.y == ["b"]


def test_missingRoleIsRejectedWithItsName():
    with pytest.raises(ValidationError) as error:
        makeRequest("bar", {"y": ["b"]})
    assert "'x'" in str(error.value)
    assert "bar" in str(error.value)


def test_severalYWithSeriesIsRejected():
    with pytest.raises(ValidationError):
        makeRequest("bar", {"x": "m", "y": ["a", "b"], "series": "s"})


def test_roleOfAnotherChartTypeIsRejected():
    with pytest.raises(ValidationError) as error:
        makeRequest("pie", {"label": "a", "value": "b", "series": "s"})
    assert "'series'" in str(error.value)


def test_unknownChartTypeAndExtraKeysAreRejected():
    with pytest.raises(ValidationError):
        ChartRequest.model_validate(
            {"chartType": "box-plot", "title": "t", "sql": "SELECT 1", "encoding": {"x": "a", "y": ["b"]}}
        )
    with pytest.raises(ValidationError):
        ChartRequest.model_validate(
            {"chartType": "bar", "title": "t", "sql": "SELECT 1", "encoding": {"x": "a", "y": ["b"]}, "code": "x"}
        )


def test_titleAndSqlLengthsAreEnforced():
    with pytest.raises(ValidationError):
        makeRequest("bar", title="")
    with pytest.raises(ValidationError):
        makeRequest("bar", title="t" * 201)
    with pytest.raises(ValidationError):
        makeRequest("bar", sql="")
    with pytest.raises(ValidationError):
        makeRequest("bar", sql="s" * 20001)


def test_requiredColumnsSkipsTheCardLabel():
    request = makeRequest("card", {"value": "total", "label": "Total orders"})
    assert requiredColumns(request) == ["total"]


def test_requiredColumnsListsRolesInOrderWithoutRepeats():
    request = makeRequest("bar", {"x": "a", "y": ["b", "a"], "series": None})
    assert requiredColumns(request) == ["a", "b"]
    request = makeRequest("bubble", {"x": "a", "y": ["b"], "r": "c", "series": "s"})
    assert requiredColumns(request) == ["a", "b", "s", "c"]


def test_requiredColumnsOfTableWithoutColumnsIsEmpty():
    assert requiredColumns(makeRequest("table")) == []
    assert requiredColumns(makeRequest("table", {"columns": ["b", "a"]})) == ["b", "a"]


def test_geoMapLabelIsAColumn():
    request = makeRequest("geoMap", {"lat": "la", "long": "lo", "label": "name", "id": "key"})
    assert requiredColumns(request) == ["name", "la", "lo", "key"]


@pytest.mark.parametrize(
    "chartType, limit",
    [
        ("table", 1000),
        ("geoMap", 2000),
        ("scatter", 5000),
        ("bubble", 5000),
        ("pivot", 10000),
        ("card", 2),
        ("bar", 5000),
        ("pie", 5000),
    ],
)
def test_rowLimit(chartType, limit):
    assert rowLimit(chartType) == limit


def test_rowLimitOfUnknownTypeRaises():
    with pytest.raises(ValueError):
        rowLimit("box-plot")


def test_generatedCodeRoundTrips():
    request = makeRequest("bar", {"x": "a", "y": ["b"]}, xLabel="Ünï", datasetLabels={"b": "Bee"})
    text = encodeGeneratedCode(request)
    assert text.startswith('{"nubrixChart":1,')
    assert "Ünï" in text
    assert "null" not in text
    assert decodeGeneratedCode(text) == request


@pytest.mark.parametrize(
    "text",
    [
        "result = 1\nprint(result)",
        "# Failed to generate code for: x",
        "```python\nresult = 1\n```",
        "",
        None,
        "[1, 2]",
        '{"other": 1}',
    ],
)
def test_pythonCodeIsNotAChartRequest(text):
    assert decodeGeneratedCode(text) is None


def test_wrongVersionRaises():
    text = encodeGeneratedCode(makeRequest("bar")).replace('"nubrixChart":1', '"nubrixChart":2')
    with pytest.raises(ValueError):
        decodeGeneratedCode(text)


@pytest.mark.parametrize("version", ["1.0", "true"])
def test_onlyTheIntegerOneIsAVersion(version):
    text = encodeGeneratedCode(makeRequest("bar")).replace('"nubrixChart":1', '"nubrixChart":' + version)
    with pytest.raises(ValueError):
        decodeGeneratedCode(text)


def test_markedButInvalidRequestRaises():
    with pytest.raises(ValueError):
        decodeGeneratedCode('{"nubrixChart":1,"chartType":"bar"}')


def test_chartRequestTextGivesEmptyProvenance():
    text = encodeGeneratedCode(makeRequest("bar"))
    assert extractProvenance(text) == extractProvenance("")
