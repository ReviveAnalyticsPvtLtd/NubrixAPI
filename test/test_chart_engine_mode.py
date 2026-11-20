import pytest

from nubrix.components import chartEngineMode as module


@pytest.fixture(autouse=True)
def resetWarning():
    module._warnedInvalid = False
    yield
    module._warnedInvalid = False


def test_modeDefaultsToOff(monkeypatch):
    monkeypatch.delenv("CHART_ENGINE", raising=False)
    assert module.chartEngineMode() == "off"


@pytest.mark.parametrize("value", ["off", "shadow", "on"])
def test_modeValues(monkeypatch, value):
    monkeypatch.setenv("CHART_ENGINE", value)
    assert module.chartEngineMode() == value


def test_modeIsReadOnEachCall(monkeypatch):
    monkeypatch.setenv("CHART_ENGINE", "on")
    assert module.chartEngineMode() == "on"
    monkeypatch.setenv("CHART_ENGINE", "shadow")
    assert module.chartEngineMode() == "shadow"


def test_invalidModeIsOff(monkeypatch):
    monkeypatch.setenv("CHART_ENGINE", "yes please")
    assert module.chartEngineMode() == "off"


def test_invalidModeWarnsOncePerProcess(monkeypatch):
    messages = []
    monkeypatch.setattr(module.logger, "warning", lambda message, *a, **k: messages.append(message))
    monkeypatch.setenv("CHART_ENGINE", "bogus")
    module.chartEngineMode()
    module.chartEngineMode()
    assert len(messages) == 1
    monkeypatch.setenv("CHART_ENGINE", "on")
    assert module.chartEngineMode() == "on"
    assert len(messages) == 1
