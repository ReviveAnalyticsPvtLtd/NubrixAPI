"""Generated code reads tables from real sandbox subprocesses.

Each launcher runs with the filtered environment its parent builds, so these
tests fail if SUPABASE_URL or SUPABASE_KEY ever leaves an allowlist. Another
project's table sits in the same bucket, so a missing project check reads it
instead of failing on a 404.
"""

import io
import os
import subprocess
from unittest.mock import MagicMock

import polars as pl
import pytest

from nubrix.components.transformationExecutor import TransformationExecutor
from test.privateStorageFixtures import SERVICE_KEY, FakePrivateStorage
from utils import codeExecutor

PROJECT = "11111111-2222-4333-8444-555555555555"
OTHER_PROJECT = "99999999-8888-4777-8666-555555555555"
READERS = ("fetch_data", "fetch_data_pl", "scan_data")
READ_EVERY_WAY = (
    f'a = fetch_data("{PROJECT}", "sales")\n'
    f'b = fetch_data_pl("{PROJECT}", "sales")\n'
    f'c = scan_data("{PROJECT}", "sales").collect()\n'
)


@pytest.fixture(autouse=True)
def _windowsSocketsNeedSystemRoot(monkeypatch):
    """Winsock can't load without SYSTEMROOT, which the Linux-only allowlists leave out."""
    if os.name != "nt":
        return
    realPopen = subprocess.Popen

    def popen(*args, env=None, **kwargs):
        return realPopen(*args, env={**env, "SYSTEMROOT": os.environ["SYSTEMROOT"]}, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", popen)


@pytest.fixture
def storage(monkeypatch):
    fake = FakePrivateStorage()
    buffer = io.BytesIO()
    pl.DataFrame({"amount": [1, 2, 3]}).write_parquet(buffer)
    fake.put(PROJECT, "sales.parquet", buffer.getvalue())
    fake.put(OTHER_PROJECT, "sales.parquet", buffer.getvalue())
    monkeypatch.setenv("SUPABASE_URL", fake.url)
    monkeypatch.setenv("SUPABASE_KEY", SERVICE_KEY)
    monkeypatch.setenv("REDIS_HOST", "127.0.0.1")
    monkeypatch.setenv("REDIS_PORT", "1")
    yield fake
    fake.close()


def _assertReadWithTheServiceKey(storage):
    reads = [r for r in storage.requests if r["method"] == "GET"]
    assert reads
    assert all(r["path"] == f"/storage/v1/object/AnalyticsHub/{PROJECT}/sales.parquet" for r in reads)
    assert all(r["authorization"] == f"Bearer {SERVICE_KEY}" for r in reads)


def test_codeExecutionSandboxReadsTheBucket(storage, monkeypatch):
    monkeypatch.setattr(codeExecutor.creditService, "_redis", lambda: MagicMock(zcard=lambda _key: 0))
    manager = codeExecutor.REPLManager(timeoutSeconds=120)

    output = manager.run(READ_EVERY_WAY + "print(len(a), b.height, c.height)", projectId=PROJECT)

    assert output.strip() == "3 3 3"
    _assertReadWithTheServiceKey(storage)


def test_transformationSandboxReadsTheBucket(storage):
    executor = TransformationExecutor(timeoutSeconds=120)

    frame = executor._execute_code(PROJECT, READ_EVERY_WAY + "final_df = b")

    assert list(frame["amount"]) == [1, 2, 3]
    _assertReadWithTheServiceKey(storage)


def test_inspectionSandboxReadsTheBucket(storage):
    executor = TransformationExecutor(timeoutSeconds=120)

    output = executor.executeInspection(PROJECT, READ_EVERY_WAY + "print(len(a), b.height, c.height)")

    assert output.strip() == "3 3 3"
    _assertReadWithTheServiceKey(storage)


def _assertOtherProjectUntouched(storage):
    assert not [path for path in storage.pathsRead() if OTHER_PROJECT in path]


@pytest.mark.parametrize("reader", READERS)
def test_codeExecutionSandboxRefusesAnotherProject(storage, monkeypatch, reader):
    monkeypatch.setattr(codeExecutor.creditService, "_redis", lambda: MagicMock(zcard=lambda _key: 0))
    manager = codeExecutor.REPLManager(timeoutSeconds=120)

    output = manager.run(f'print(type({reader}("{OTHER_PROJECT}", "sales")).__name__)', projectId=PROJECT)

    assert "PermissionError: Access denied" in output
    _assertOtherProjectUntouched(storage)


@pytest.mark.parametrize("reader", READERS)
def test_transformationSandboxRefusesAnotherProject(storage, reader):
    executor = TransformationExecutor(timeoutSeconds=120)

    with pytest.raises(ValueError, match="PermissionError: Access denied"):
        executor._execute_code(PROJECT, f'final_df = {reader}("{OTHER_PROJECT}", "sales")')

    _assertOtherProjectUntouched(storage)


@pytest.mark.parametrize("reader", READERS)
def test_inspectionSandboxRefusesAnotherProject(storage, reader):
    executor = TransformationExecutor(timeoutSeconds=120)

    output = executor.executeInspection(PROJECT, f'print(type({reader}("{OTHER_PROJECT}", "sales")).__name__)')

    assert "PermissionError: Access denied" in output
    _assertOtherProjectUntouched(storage)


def test_transformationSandboxRefusesAnotherProjectByKeyword(storage):
    executor = TransformationExecutor(timeoutSeconds=120)

    with pytest.raises(ValueError, match="PermissionError: Access denied"):
        executor._execute_code(PROJECT, f'final_df = fetch_data_pl(projectId="{OTHER_PROJECT}", tableName="sales")')

    _assertOtherProjectUntouched(storage)


def test_inspectionSandboxRefusesAnotherProjectByKeyword(storage):
    executor = TransformationExecutor(timeoutSeconds=120)

    output = executor.executeInspection(PROJECT, f'print(type(fetch_data_pl(projectId="{OTHER_PROJECT}", tableName="sales")).__name__)')

    assert "PermissionError: Access denied" in output
    _assertOtherProjectUntouched(storage)


def test_transformationSandboxIgnoresARebuiltProjectId(storage):
    executor = TransformationExecutor(timeoutSeconds=120)
    code = f'projectId = "{OTHER_PROJECT}"\nfinal_df = fetch_data_pl(projectId, "sales")'

    with pytest.raises(ValueError, match="PermissionError: Access denied"):
        executor._execute_code(PROJECT, code)

    _assertOtherProjectUntouched(storage)


def test_inspectionSandboxIgnoresARebuiltProjectId(storage):
    executor = TransformationExecutor(timeoutSeconds=120)
    code = f'projectId = "{OTHER_PROJECT}"\nprint(type(fetch_data_pl(projectId, "sales")).__name__)'

    output = executor.executeInspection(PROJECT, code)

    assert "PermissionError: Access denied" in output
    _assertOtherProjectUntouched(storage)


def test_transformationSandboxKeepsEveryOwnProjectCallShape(storage):
    executor = TransformationExecutor(timeoutSeconds=120)
    code = (
        'a = fetch_data_pl(projectId=projectId, tableName="sales")\n'
        'b = scan_data(projectId, tableName="sales").collect()\n'
        'c = fetch_data(projectId, "sales", [{"amount": 2}])\n'
        'd = fetch_data(projectId=projectId, tableName="sales", baseFilters=[{"amount": 3}])\n'
        'final_df = pd.DataFrame({"sizes": [a.height, b.height, len(c), len(d)]})'
    )

    frame = executor._execute_code(PROJECT, code)

    assert list(frame["sizes"]) == [3, 3, 1, 1]
    _assertReadWithTheServiceKey(storage)


def test_inspectionSandboxKeepsEveryOwnProjectCallShape(storage):
    executor = TransformationExecutor(timeoutSeconds=120)
    code = (
        'a = fetch_data_pl(projectId=projectId, tableName="sales")\n'
        'b = scan_data(projectId, tableName="sales").collect()\n'
        'c = fetch_data(projectId, "sales", [{"amount": 2}])\n'
        'd = fetch_data(projectId=projectId, tableName="sales", baseFilters=[{"amount": 3}])\n'
        'print(a.height, b.height, len(c), len(d))'
    )

    output = executor.executeInspection(PROJECT, code)

    assert output.strip() == "3 3 1 1"
    _assertReadWithTheServiceKey(storage)
