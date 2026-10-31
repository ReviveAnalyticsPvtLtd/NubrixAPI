"""
sandboxRunner.py

Runs a launcher as a subprocess that holds no credentials.

Generated code can reach anything its process can: its environment, its memory
and, through pandas and NumPy, `os`, file reads and HTTP reads. So the child
gets no Supabase or Redis settings. The parent reads tables for it over a
private pipe pair. The child sends a table name only: the project isn't in the
protocol, because the parent already knows it.

Each run has its own uid, so a child can't read the parent's secrets from
`/proc`, write to the application, or reach another run's tables. A shared
sandbox user would let concurrent runs for different projects read each other.

    runFolder/        root's, children may only pass through
        tables/       the parent writes here, this run may read
        work/         this run's own: cwd, HOME, TMPDIR and the result file
"""

__all__ = ["runSandbox", "serveTables", "readResult", "connectTableChannel", "SandboxResult"]


import itertools
import json
import os
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
from typing import NamedTuple

from utils.initMethods import spool_table, use_table_source
from utils.logger import logger

# Below 65536, the range a user namespace normally maps.
_FIRST_RUN_UID = 20000
_RUN_UID_COUNT = 40000
_MAX_REQUEST_BYTES = 4096
# The first newline ends any partial request the child left, the second is a blank line.
_STOP_SERVING = b"\n\n"
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Generated code can read its whole environment, so never add a secret here.
_INHERITED_ENV = (
    "PATH", "PYTHONPATH", "LANG", "LC_ALL", "TZ",
    "POLARS_MAX_THREADS", "OPENBLAS_NUM_THREADS",
    "DF_CACHE_MAX_BYTES", "DF_CACHE_MAX_ENTRIES",
)


class SandboxResult(NamedTuple):
    stdout: str
    stderr: str
    returncode: int
    resultBytes: bytes | None


def _claimRun() -> tuple[int | None, str]:
    """Return this run's uid and folder. The folder is the claim: only one run can create it.

    An image sets SANDBOX_RUN_ROOT to require a separate uid, and a parent that
    can't change uid then fails on the chown instead of running without one.
    """
    runRoot = os.environ.get("SANDBOX_RUN_ROOT")
    if not runRoot:
        if os.name == "posix" and os.geteuid() == 0:
            raise RuntimeError("SANDBOX_RUN_ROOT must be set: a root parent can't run sandboxes without a separate uid.")
        return None, tempfile.mkdtemp(prefix="sandbox-")
    while True:
        runUid = _FIRST_RUN_UID + secrets.randbelow(_RUN_UID_COUNT)
        runFolder = os.path.join(runRoot, f"sandbox-{runUid}")
        try:
            os.mkdir(runFolder, 0o700)
            return runUid, runFolder
        except FileExistsError:
            continue


def _prepareFolders(runUid: int | None, runFolder: str) -> tuple[str, str]:
    """Create the tables and work folders. The run folder opens last, once nothing in it can be swapped."""
    tablesFolder = os.path.join(runFolder, "tables")
    workFolder = os.path.join(runFolder, "work")
    os.mkdir(tablesFolder, 0o700)
    os.mkdir(workFolder, 0o700)
    os.chmod(tablesFolder, 0o750)
    if runUid is not None:
        os.chown(tablesFolder, os.geteuid(), runUid)
        os.chown(workFolder, runUid, runUid)
    os.chmod(runFolder, 0o711)
    return tablesFolder, workFolder


def serveTables(projectId: str, requests, replies, tablesFolder: str) -> None:
    """Answer table requests until the run ends. Nothing the child sends can name a project."""
    spooled: dict[str, str] = {}
    fileNumbers = itertools.count()
    childIsGone = False
    while True:
        line = requests.readline(_MAX_REQUEST_BYTES)
        if not line.strip():
            return
        if childIsGone:
            continue
        tableName = None
        try:
            tableName = json.loads(line)
            if not isinstance(tableName, str):
                raise ValueError("A table request must be a table name.")
            if tableName not in spooled:
                path = spool_table(projectId, tableName, os.path.join(tablesFolder, str(next(fileNumbers))))
                os.chmod(path, 0o644)
                spooled[tableName] = os.path.basename(path)
            reply = {"file": spooled[tableName]}
        except ValueError as e:
            reply = {"error": str(e)}
        except Exception as e:
            logger.warning(f"Sandbox table read failed for project {projectId}: {e}")
            reply = {"error": f"Could not read table {tableName!r}."}
        try:
            replies.write(json.dumps(reply).encode("utf-8") + b"\n")
        except OSError:
            # Keep draining, so the parent's stop line can't block on a full pipe.
            childIsGone = True


def _startChild(launcherName: str, runUid: int | None, workFolder: str, childFds: tuple[int, int]) -> tuple[subprocess.Popen, dict]:
    """Start the launcher. Returns the process and how the child finds its end of the channel."""
    env = {k: os.environ[k] for k in _INHERITED_ENV if k in os.environ}
    env["PYTHONPATH"] = _ROOT + os.pathsep + env.get("PYTHONPATH", "")
    env.update(HOME=workFolder, TMPDIR=workFolder, USER="sandbox", LOGNAME="sandbox")
    options: dict = {}
    if runUid is not None:
        options.update(user=runUid, group=runUid, extra_groups=[])
    if os.name == "nt":
        import msvcrt
        channelEnds = [msvcrt.get_osfhandle(fd) for fd in childFds]
        for handle in channelEnds:
            os.set_handle_inheritable(handle, True)
        options["startupinfo"] = subprocess.STARTUPINFO(lpAttributeList={"handle_list": channelEnds})
    else:
        channelEnds = list(childFds)
        options["pass_fds"] = childFds
    proc = subprocess.Popen(
        [sys.executable, "-s", os.path.join(_ROOT, "utils", launcherName)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        cwd=workFolder,
        close_fds=True,
        **options,
    )
    return proc, {"requests": channelEnds[0], "replies": channelEnds[1]}


def readResult(path: str, runUid: int | None) -> bytes | None:
    """Read the child's result file. The child owns that folder, so a link there could point anywhere the parent can read."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError as e:
        raise ValueError("Sandbox result is not a regular file.") from e
    with os.fdopen(fd, "rb") as result:
        info = os.fstat(result.fileno())
        if not stat.S_ISREG(info.st_mode) or (runUid is not None and info.st_uid != runUid):
            raise ValueError("Sandbox result is not a regular file.")
        return result.read()


def _communicate(proc: subprocess.Popen, payload: str, timeoutSeconds: int | None) -> tuple[str, str]:
    """Send the payload and wait for the child, killing it at the timeout."""
    try:
        if timeoutSeconds and timeoutSeconds > 0:
            return proc.communicate(input=payload, timeout=timeoutSeconds)
        return proc.communicate(input=payload)
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate()
        return stdout, stderr + f"\nExecution timed out after {timeoutSeconds} seconds.\n"


def runSandbox(launcherName: str, projectId: str, code: str, timeoutSeconds: int | None) -> SandboxResult:
    """Run generated code in a launcher subprocess, serving its table reads from this process."""
    runUid, runFolder = _claimRun()
    try:
        tablesFolder, workFolder = _prepareFolders(runUid, runFolder)
        resultPath = os.path.join(workFolder, "result.parquet")
        requestRead, requestWrite = os.pipe()
        replyRead, replyWrite = os.pipe()
        with os.fdopen(requestRead, "rb") as requests, os.fdopen(replyWrite, "wb", buffering=0) as replies:
            try:
                try:
                    proc, channel = _startChild(launcherName, runUid, workFolder, (requestWrite, replyRead))
                finally:
                    # Only the child holds this end now, so a reply to a dead child fails instead of blocking.
                    os.close(replyRead)
                server = threading.Thread(target=serveTables, args=(projectId, requests, replies, tablesFolder), daemon=True)
                server.start()
                try:
                    payload = json.dumps({
                        "projectId": projectId,
                        "code": code,
                        "result_path": resultPath,
                        "channel": {**channel, "tablesFolder": tablesFolder},
                    })
                    stdout, stderr = _communicate(proc, payload, timeoutSeconds)
                finally:
                    os.write(requestWrite, _STOP_SERVING)
                    server.join()
            finally:
                os.close(requestWrite)
        resultBytes = readResult(resultPath, runUid) if proc.returncode == 0 else None
        return SandboxResult(stdout, stderr, proc.returncode, resultBytes)
    finally:
        shutil.rmtree(runFolder, ignore_errors=True)


def connectTableChannel(channel: dict) -> None:
    """Child side: send every table read to the parent, which holds the credentials and the project."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    if os.name == "nt":
        import msvcrt
        requestFd = msvcrt.open_osfhandle(channel["requests"], os.O_WRONLY)
        replyFd = msvcrt.open_osfhandle(channel["replies"], os.O_RDONLY)
    else:
        requestFd, replyFd = channel["requests"], channel["replies"]
    requests = os.fdopen(requestFd, "wb")
    replies = os.fdopen(replyFd, "rb")
    lock = threading.Lock()

    def readTable(tableName: str) -> "pa.Table":
        with lock:
            requests.write(json.dumps(tableName).encode("utf-8") + b"\n")
            requests.flush()
            reply = json.loads(replies.readline())
        if "error" in reply:
            raise RuntimeError(reply["error"])
        path = os.path.join(channel["tablesFolder"], reply["file"])
        if path.endswith(".arrow"):
            return pa.ipc.open_file(pa.memory_map(path)).read_all()
        return pq.read_table(path)

    use_table_source(readTable)
