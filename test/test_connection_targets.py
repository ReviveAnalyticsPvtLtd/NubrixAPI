import socket
import types

import pytest

from utils import connectionTargets
from utils.connectionTargets import (
    BlockedAddressError,
    checkMongoUri,
    publicAddress,
    publicServers,
)


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


def _failToResolve(*a, **k):
    raise socket.gaierror(11001, "getaddrinfo failed")


def test_unresolvableHostIsNotReportedAsBlocked(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _failToResolve)
    with pytest.raises(socket.gaierror):
        publicAddress("typo.example.com", 3306)


def _server(host, port=27017):
    return types.SimpleNamespace(address=(host, port))


def test_serverSelectorKeepsOnlyPublicServers(monkeypatch):
    ips = {"a.example.com": "8.8.8.8", "b.example.com": "10.0.0.5"}
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port, *a, **k: [(socket.AF_INET, 1, 6, "", (ips[host], port))])
    public, private = _server("a.example.com"), _server("b.example.com")
    internal, local = _server("x.railway.internal"), _server("localhost")
    assert publicServers([public, private, internal, local]) == [public]


def test_serverSelectorDropsServersThatDoNotResolve(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _failToResolve)
    assert publicServers([_server("typo.example.com")]) == []
