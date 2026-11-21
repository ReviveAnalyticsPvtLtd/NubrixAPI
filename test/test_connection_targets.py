import socket

import pytest

from api.services.dataLoadService import dataLoadService
from api.models import LoadMongoDB, LoadMySQLorPostgreSQL
from utils import connectionTargets
from utils.connectionTargets import (
    BLOCKED_ADDRESS_MESSAGE,
    BlockedAddressError,
    checkMongoUri,
    publicAddress,
)
from utils.exceptionHandler import CustomException


def _resolveTo(monkeypatch, *ips):
    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda host, port, *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in ips],
    )


def test_publicHostResolvesToItsAddress(monkeypatch):
    _resolveTo(monkeypatch, "8.8.8.8")
    assert publicAddress("db.example.com", 3306) == "8.8.8.8"


@pytest.mark.parametrize("ip", [
    "127.0.0.1", "10.0.0.5", "172.16.0.1", "192.168.1.1", "169.254.169.254",
    "100.64.0.1", "::1", "fd00::1", "0.0.0.0",
])
def test_refuses(monkeypatch, ip):
    _resolveTo(monkeypatch, ip)
    with pytest.raises(BlockedAddressError):
        publicAddress("db.example.com", 5432)


def test_refusesWhenAnyResolvedAddressIsPrivate(monkeypatch):
    _resolveTo(monkeypatch, "8.8.8.8", "10.0.0.1")
    with pytest.raises(BlockedAddressError):
        publicAddress("db.example.com", 5432)


@pytest.mark.parametrize("host", ["postgres.railway.internal", "localhost"])
def test_refusesRailwayInternalNames(monkeypatch, host):
    def boom(*a, **k):
        raise AssertionError("must not resolve")
    monkeypatch.setattr(socket, "getaddrinfo", boom)
    with pytest.raises(BlockedAddressError):
        publicAddress(host, 5432)


def test_mongoUriWithPrivateHostIsRefused(monkeypatch):
    monkeypatch.setattr(connectionTargets, "parse_uri", lambda uri: {"nodelist": [("db.example.com", 27017), ("db2.example.com", 27017)]})
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port, *a, **k: [(socket.AF_INET, 1, 6, "", ("10.0.0.1" if host == "db2.example.com" else "8.8.8.8", port))])
    with pytest.raises(BlockedAddressError):
        checkMongoUri("mongodb://ignored")


def test_mongoUriWithPublicHostsPasses(monkeypatch):
    monkeypatch.setattr(connectionTargets, "parse_uri", lambda uri: {"nodelist": [("a.example.com", 27017), ("b.example.com", 27017)]})
    _resolveTo(monkeypatch, "8.8.8.8")
    checkMongoUri("mongodb://ignored")


def test_oldLoaderRefusesPrivateHost(monkeypatch):
    import api.services.dataLoadService as module

    def boom(*a, **k):
        raise AssertionError("create_engine must not run")
    monkeypatch.setattr(module, "create_engine", boom)
    _resolveTo(monkeypatch, "10.0.0.1")
    connection = LoadMySQLorPostgreSQL(host="10.0.0.1", port=3306, user="u", password="p", db="d", table="t", projectId="p1")
    with pytest.raises(CustomException) as caught:
        dataLoadService.loadMySql(connection)
    assert caught.value.statusCode == 400
    assert caught.value.uiMessage == BLOCKED_ADDRESS_MESSAGE


def _failToResolve(*a, **k):
    raise socket.gaierror(11001, "getaddrinfo failed")


def test_unresolvableHostIsNotReportedAsBlocked(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _failToResolve)
    with pytest.raises(socket.gaierror):
        publicAddress("typo.example.com", 3306)


@pytest.mark.parametrize("loader,message", [
    ("loadMySql", "Unable to connect to MySQL : check host/port/credentials."),
    ("loadPostgreSQL", "Unable to connect to PostgreSQL: check host/port/credentials."),
])
def test_loaderKeepsConnectMessageForUnresolvableHost(monkeypatch, loader, message):
    monkeypatch.setattr(socket, "getaddrinfo", _failToResolve)
    connection = LoadMySQLorPostgreSQL(host="typo.example.com", port=3306, user="u", password="p", db="d", table="t", projectId="p1")
    with pytest.raises(CustomException) as caught:
        getattr(dataLoadService, loader)(connection)
    assert caught.value.statusCode == 400
    assert caught.value.uiMessage == message


def test_mongoLoaderKeepsConnectMessageForUnresolvableHost(monkeypatch):
    monkeypatch.setattr(connectionTargets, "parse_uri", lambda uri: {"nodelist": [("typo.example.com", 27017)]})
    monkeypatch.setattr(socket, "getaddrinfo", _failToResolve)
    with pytest.raises(CustomException) as caught:
        dataLoadService.loadMongoDB(LoadMongoDB(projectId="p1", connectionString="mongodb://ignored", db="d", collection="c"))
    assert caught.value.statusCode == 400
    assert caught.value.uiMessage == "Unable to connect to MongoDB: check connection string/credentials."
