"""The local storage server the chart evaluation harness reads its tables through, and the fixtures it serves."""

import threading
from pathlib import Path

import httpx
import pytest

from scripts import localStorageServer

FIXTURES = Path(__file__).parent / "fixtures" / "chart_eval"
DATA = FIXTURES / "data"
PROJECT = "11111111-2222-4333-8444-555555555555"
KEY = "local-eval-key"


# The local storage server


@pytest.fixture
def served():
    url, stop = localStorageServer.serveFolder(str(DATA), key=KEY)
    try:
        yield url
    finally:
        stop()


def _get(url, path, key=KEY, **headers):
    sent = {"Authorization": f"Bearer {key}", "apikey": key, **headers} if key else dict(headers)
    return httpx.get(f"{url}{path}", headers=sent)


PATH = f"/storage/v1/object/AnalyticsHub/{PROJECT}/orders.parquet"


def test_storageServerServesFilesWithHeadAndRange(served):
    body = (DATA / PROJECT / "orders.parquet").read_bytes()
    assert _get(served, PATH).content == body
    head = httpx.head(f"{served}{PATH}", headers={"Authorization": f"Bearer {KEY}"})
    assert head.status_code == 200 and int(head.headers["Content-Length"]) == len(body) and head.content == b""
    part = _get(served, PATH, Range="bytes=4-9")
    assert part.status_code == 206 and part.content == body[4:10]
    assert part.headers["Content-Range"] == f"bytes 4-9/{len(body)}"
    assert _get(served, PATH, Range="bytes=-8").content == body[-8:]
    assert _get(served, PATH, Range=f"bytes={len(body) + 5}-").status_code == 416
    assert _get(served, f"/storage/v1/object/AnalyticsHub/{PROJECT}/metadata.json").json()["orders"]["shape"] == [36, 6]
    assert _get(served, f"/storage/v1/object/AnalyticsHub/{PROJECT}/missing.parquet").status_code == 404


def test_storageServerRequiresTheKey(served):
    assert _get(served, PATH, key=None).status_code == 401
    assert _get(served, PATH, key="wrong").status_code == 401
    assert httpx.get(f"{served}{PATH}", headers={"apikey": KEY}).status_code == 401


def test_storageServerRefusesPathTraversalAndOtherPaths(served):
    for path in (
        f"/storage/v1/object/AnalyticsHub/{PROJECT}/..%2f..%2fcases.jsonl",
        f"/storage/v1/object/AnalyticsHub/{PROJECT}/../../cases.jsonl",
        f"/storage/v1/object/AnalyticsHub/%2e%2e/{PROJECT}/orders.parquet",
        f"/storage/v1/object/AnalyticsHub/{PROJECT}/..%5c..%5ccases.jsonl",
        f"/storage/v1/object/AnalyticsHub/{PROJECT}/C%3A%5cWindows%5cwin.ini",
        "/storage/v1/object/AnalyticsHub//etc/passwd",
        f"/storage/v1/object/AnalyticsHub/{PROJECT}/%2fetc%2fpasswd",
        f"/storage/v1/object/AnalyticsHub/{PROJECT}/orders.parquet/extra",
        "/storage/v1/object/AnalyticsHub/orders.parquet",
        f"/storage/v1/object/public/AnalyticsHub/{PROJECT}/orders.parquet",
        f"/storage/v1/object/OtherBucket/{PROJECT}/orders.parquet",
        f"/{PROJECT}/orders.parquet",
    ):
        response = _get(served, path)
        assert response.status_code in (400, 404), path
        assert not response.content.startswith(b"PAR1"), path


def test_storageServerRefusesASymlinkOutOfTheFolder(tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    root = tmp_path / "root"
    (root / PROJECT).mkdir(parents=True)
    try:
        (root / PROJECT / "leak.txt").symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("this account cannot create symlinks")
    url, stop = localStorageServer.serveFolder(str(root), key=KEY)
    try:
        assert _get(url, f"/storage/v1/object/AnalyticsHub/{PROJECT}/leak.txt").status_code in (400, 404)
    finally:
        stop()


def test_storageServerBindsLoopbackAndStopsCleanly():
    before = threading.active_count()
    url, stop = localStorageServer.serveFolder(str(DATA), key=KEY)
    assert url.startswith("http://127.0.0.1:")
    assert _get(url, PATH).status_code == 200
    stop()
    with pytest.raises(httpx.TransportError):
        _get(url, PATH)
    assert threading.active_count() <= before
    stop()


def test_storageServerNeedsAKey(monkeypatch):
    monkeypatch.delenv("SUPABASE_KEY", raising=False)
    with pytest.raises(ValueError):
        localStorageServer.serveFolder(str(DATA))
    with pytest.raises(ValueError):
        localStorageServer.serveFolder(str(DATA), key="")


# The local storage server and RFC 7233 ranges

FIXTURE_KEY = "chart-eval-fixture-key"


def _size(name="orders"):
    return (DATA / PROJECT / f"{name}.parquet").stat().st_size


@pytest.mark.parametrize(
    "header, status, contentRange",
    [
        ("bytes=0-", 206, "bytes 0-{last}/{size}"),
        ("bytes=10-", 206, "bytes 10-{last}/{size}"),
        ("bytes=0-99999999", 206, "bytes 0-{last}/{size}"),
        ("bytes=-5", 206, "bytes {from5}-{last}/{size}"),
        ("bytes=-99999999", 206, "bytes 0-{last}/{size}"),
        ("bytes=-0", 416, "bytes */{size}"),
        ("bytes={size}-", 416, "bytes */{size}"),
        ("bytes={size}-{more}", 416, "bytes */{size}"),
        # A range that is not syntactically valid, one for several ranges, and another unit are ignored: the whole file.
        ("bytes=5-3", 200, None),
        ("bytes=0-1,5-6", 200, None),
        ("bytes=a-b", 200, None),
        ("bytes=", 200, None),
        ("items=0-1", 200, None),
    ],
)
def test_storageServerRangesFollowRfc7233(served, header, status, contentRange):
    size = _size()
    header = header.format(size=size, more=size + 10)
    response = _get(served, PATH, Range=header)
    assert response.status_code == status, header
    if contentRange is None:
        assert "Content-Range" not in response.headers and len(response.content) == size
        return
    assert response.headers["Content-Range"] == contentRange.format(size=size, last=size - 1, from5=size - 5)
    assert len(response.content) == int(response.headers["Content-Length"])
    if status == 416:
        assert response.content == b""


def test_storageServerAnswersHeadWithRangeLikeGet(served):
    size = _size()
    head = httpx.head(f"{served}{PATH}", headers={"Authorization": f"Bearer {KEY}", "Range": "bytes=4-9"})
    assert head.status_code == 206 and head.content == b""
    assert head.headers["Content-Range"] == f"bytes 4-9/{size}" and head.headers["Content-Length"] == "6"
    unsatisfiable = httpx.head(f"{served}{PATH}", headers={"Authorization": f"Bearer {KEY}", "Range": f"bytes={size}-"})
    assert unsatisfiable.status_code == 416 and unsatisfiable.headers["Content-Range"] == f"bytes */{size}"


def test_storageServerKeepsARequestLogForTheCallersToCount():
    log = []
    url, stop = localStorageServer.serveFolder(str(DATA), key=KEY, requestLog=log)
    try:
        _get(url, PATH)
        httpx.head(f"{url}{PATH}", headers={"Authorization": f"Bearer {KEY}"})
        _get(url, PATH, Range="bytes=0-3")
        _get(url, PATH, key="wrong")
    finally:
        stop()
    assert log == [
        {"method": "GET", "file": "orders.parquet", "range": None, "status": 200},
        {"method": "HEAD", "file": "orders.parquet", "range": None, "status": 200},
        {"method": "GET", "file": "orders.parquet", "range": "bytes=0-3", "status": 206},
        {"method": "GET", "file": "orders.parquet", "range": None, "status": 401},
    ]


def _schemaRequests(monkeypatch, key):
    """The requests `storedTableSchema` makes for the orders table served with `key`; the tables were marked with FIXTURE_KEY."""
    from utils import storedTables

    log = []
    url, stop = localStorageServer.serveFolder(str(DATA), key=key, requestLog=log)
    monkeypatch.setenv("SUPABASE_URL", url)
    monkeypatch.setenv("SUPABASE_KEY", key)
    try:
        schema = storedTables.storedTableSchema(PROJECT, "orders")
    finally:
        stop()
    assert schema.names == ["order_id", "order_date", "region", "product_id", "quantity", "revenue"]
    return [entry for entry in log if entry["file"] == "orders.parquet"]


def test_aMarkedTablesSchemaIsReadByHeadAndRangeOnly(monkeypatch):
    requests = _schemaRequests(monkeypatch, FIXTURE_KEY)
    assert sum(entry["method"] == "HEAD" for entry in requests) == 1
    ranged = [entry for entry in requests if entry["method"] == "GET"]
    assert ranged and all(entry["range"] and entry["status"] == 206 for entry in ranged)


def test_aTableWithoutAValidMarkerIsDownloadedWhole(monkeypatch):
    # The same files under another key: the markers do not match, so the footer read falls back to a full download.
    requests = _schemaRequests(monkeypatch, "some-other-key")
    assert any(entry["method"] == "GET" and entry["range"] is None for entry in requests)


def test_theFixturesAreByteDeterministicAndMarkedByTheApiWriter(tmp_path):
    import importlib.util

    spec = importlib.util.spec_from_file_location("buildFixtures", FIXTURES / "buildFixtures.py")
    build = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build)
    assert build.FIXTURE_KEY == FIXTURE_KEY and build.PROJECT == PROJECT
    build.build(tmp_path)
    for name in ("orders", "products", "stores", "visits"):
        assert (tmp_path / f"{name}.parquet").read_bytes() == (DATA / PROJECT / f"{name}.parquet").read_bytes()
    assert (tmp_path / "metadata.json").read_bytes() == (DATA / PROJECT / "metadata.json").read_bytes()
