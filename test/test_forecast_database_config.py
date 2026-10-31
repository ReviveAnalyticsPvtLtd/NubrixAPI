import contextlib
import dataclasses
import importlib

import pytest

from nubrix.triggers.tasks import generateForecasts as forecastModule


_ENVIRONMENT = {
    "FORECAST_DB_USER": "forecast-user",
    "FORECAST_DB_PASSWORD": "test-only-p@ss/word:1",
    "FORECAST_DB_HOST": "forecast.invalid",
    "FORECAST_DB_PORT": "6543",
    "FORECAST_DB_NAME": "forecast-db",
}


def _useEnvironment(monkeypatch, **overrides):
    for name, value in {**_ENVIRONMENT, **overrides}.items():
        if value is None:
            monkeypatch.delenv(name, raising = False)
        else:
            monkeypatch.setenv(name, value)


def _refuseConnections(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("the forecast task must not connect without its settings")
    monkeypatch.setattr(forecastModule, "create_engine", refuse)


def test_configHoldsNoConnectionDetailsOfItsOwn():
    fields = dataclasses.fields(forecastModule.GenerateForecastsConfig)
    assert fields
    assert all(field.default is dataclasses.MISSING for field in fields)


def test_taskNamesEveryMissingVariableBeforeConnecting(monkeypatch):
    _useEnvironment(monkeypatch, **dict.fromkeys(_ENVIRONMENT))
    _refuseConnections(monkeypatch)

    with pytest.raises(RuntimeError) as error:
        forecastModule.GenerateForecasts().generateAndSendForecasts()

    for name in _ENVIRONMENT:
        assert name in str(error.value)


@pytest.mark.parametrize("name", sorted(_ENVIRONMENT))
@pytest.mark.parametrize("value", [None, "", "   "])
def test_taskNamesTheOneVariableThatIsMissing(monkeypatch, name, value):
    _useEnvironment(monkeypatch, **{name: value})
    _refuseConnections(monkeypatch)

    with pytest.raises(RuntimeError) as error:
        forecastModule.GenerateForecasts().generateAndSendForecasts()

    assert name in str(error.value)
    assert not any(other in str(error.value) for other in _ENVIRONMENT if other != name)


def test_nonNumericPortIsRefusedWithoutEchoingTheValue(monkeypatch):
    _useEnvironment(monkeypatch, FORECAST_DB_PORT = "not-a-port")
    _refuseConnections(monkeypatch)

    with pytest.raises(RuntimeError) as error:
        forecastModule.GenerateForecasts().generateAndSendForecasts()

    assert "FORECAST_DB_PORT" in str(error.value)
    assert "not-a-port" not in str(error.value)


def test_taskConnectsWithTheEnvironmentSettings(monkeypatch):
    _useEnvironment(monkeypatch)
    urls = []

    def recordUrl(url):
        urls.append(url)
        raise ConnectionError("stop before any network use")

    monkeypatch.setattr(forecastModule, "create_engine", recordUrl)
    monkeypatch.setattr(forecastModule.requests, "post", lambda *args, **kwargs: None)

    # The stop above is what ends the run; only the recorded URL matters here.
    with contextlib.suppress(Exception):
        forecastModule.GenerateForecasts().generateAndSendForecasts()

    assert len(urls) == 1
    assert urls[0].drivername == "postgresql+psycopg2"
    assert urls[0].username == "forecast-user"
    assert urls[0].password == "test-only-p@ss/word:1"
    assert urls[0].host == "forecast.invalid"
    assert urls[0].port == 6543
    assert urls[0].database == "forecast-db"


def test_passwordStaysOutOfTheConfigRepr(monkeypatch):
    _useEnvironment(monkeypatch)

    config = forecastModule.GenerateForecasts().generateForecastsConfig

    assert "test-only-p@ss/word:1" not in repr(config)


def test_moduleImportsWithoutTheVariablesSoTheWorkerStillStarts(monkeypatch):
    _useEnvironment(monkeypatch, **dict.fromkeys(_ENVIRONMENT))

    assert importlib.reload(forecastModule).GenerateForecasts
