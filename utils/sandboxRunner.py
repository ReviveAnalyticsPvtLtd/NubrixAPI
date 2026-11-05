"""
sandboxRunner.py

Runs a launcher as a subprocess that holds no credentials.

Generated code can reach anything its process can: its environment, its memory
and, through pandas and NumPy, `os`, file reads and HTTP reads. So the child
gets no Supabase or Redis settings. The parent reads tables for it over a
private socket. The child sends a table name only: the project isn't in the
protocol, because the parent already knows it.

Each run has its own uid, so a child can't read the parent's secrets from
`/proc`, write to the application, or reach another run's tables. A shared
sandbox user would let concurrent runs for different projects read each other.
A uid is free again only once sandbox_reaper.py has ended everything running as
it and removed its files from its own folders, because generated code can fork
and outlive the launcher. A file it left in another uid's folder stays until
that uid is cleared. The shared temp folders are closed to runs, because every
clear would otherwise have to look through whatever was left in them. A run
folder is the claim on its uid. Its worker holds a lock on
it, which the kernel drops if the worker dies, so a sweep can tell an orphaned
run from a live one.

    runFolder/        root's, children may only pass through
        tables/       the parent writes here, this run may read
        work/         this run's own: cwd, HOME, TMPDIR and the result file
"""

__all__ = ["runSandbox", "runTrustedSandbox", "sweepOrphanedRuns", "serveTables", "readResult", "ResultTooLargeError", "enterSandbox", "SandboxResult"]


import base64
import contextlib
import ctypes
import glob
import itertools
import json
import os
import secrets
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass
from typing import IO, Callable, NamedTuple

from utils.initMethods import spool_table, use_table_source
from utils.logger import logger
from utils.sandbox_reaper import _SHARED_TEMP_FOLDERS
from utils.sandboxLimits import RunLimits, currentRunLimits
from utils.sandboxSeccomp import installFilter
from utils.sandboxSlots import SLOTS_FOLDER, acquireRunSlot, releaseRunSlot
from utils.sandboxWatchdog import RunWatchdog

# Below 65536, the range a user namespace normally maps. The same number of uids above
# the range are spare: a reaper's real uid is its run's uid plus _RUN_UID_COUNT. No run
# has one, and no two reapers share one, because a shared real uid could signal each other.
_FIRST_RUN_UID = 20000
_RUN_UID_COUNT = 20000
_MAX_CLAIM_ATTEMPTS = 64
# Everything that uses these folders here runs as root. A run can pass through, but can't write or list.
_CLOSED_FOLDER_MODE = 0o711
_CLOSED_FILE_MODE = 0o600
_MAX_REQUEST_BYTES = 4096
_MAX_OUTPUT_BYTES = 64 * 1024 * 1024
# Redis refuses a bigger value, and a transformation preview is cached there before it can be applied.
_MAX_RESULT_BYTES = 512 * 1024 * 1024
_MAX_PREVIEW_BYTES = 4 * 1024 * 1024
_MAX_SPOOL_BYTES = 8 * 1024 * 1024 * 1024
_MAX_RUN_OPEN_FILES = 4096
_CPU_HARD_LIMIT_GRACE_SECONDS = 10
_CLEAR_UID_TIMEOUT_SECONDS = 30
_MAX_REAPER_ERROR_BYTES = 1024
_PR_SET_PDEATHSIG = 1
# Linux numbers, because Windows has neither name.
_SIGXCPU = 24
_SIGKILL = 9
_PR_SET_NO_NEW_PRIVS = 38
_THREAD_VARIABLES = ("POLARS_MAX_THREADS", "OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_MAX_THREADS")
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Generated code can read its whole environment, so never add a secret here.
_INHERITED_ENV = (
    "PATH", "PYTHONPATH", "LANG", "LC_ALL", "TZ", "SYSTEMROOT",
    "DF_CACHE_MAX_BYTES", "DF_CACHE_MAX_ENTRIES",
)
# Passed through only when the thread cap is off; with a cap they are set to it.
_THREAD_TUNING_ENV = ("POLARS_MAX_THREADS", "OPENBLAS_NUM_THREADS")


# The launchers runTrustedSandbox will start, by name, each with its script relative to the repo root. A name
# not in this table is refused, so no caller can point a run at another script.
_TRUSTED_LAUNCHERS: dict[str, str] = {
    "normalise_launcher.py": "utils/normalise_launcher.py",
    "table_reader_launcher.py": "utils/table_reader_launcher.py",
}
_TRUSTED_RESULT_NAME = "result"
_TRUSTED_INPUT_NAME = "input"
_INPUT_FILE_MODE = 0o640


@dataclass(frozen=True)
class _RunKind:
    """What differs between a run of generated code and a trusted run: where the child writes and what it is given."""

    resultName: str = "result.parquet"
    resultCap: int | None = None
    previewName: str | None = "preview.json"
    servesTables: bool = True
    script: str | None = None
    payload: dict | None = None


class ResultTooLargeError(ValueError):
    """A file the child wrote is over the cap given to `readResult`. Any other ValueError from it means the file is unsafe."""


class SandboxResult(NamedTuple):
    stdout: str
    stderr: str
    returncode: int
    resultBytes: bytes | None
    previewBytes: bytes | None = None
    stopped: str | None = None


def _lockRun(runFolder: str) -> int | None:
    """Lock a run folder and return the descriptor holding it, or None if a live process holds it or it has gone."""
    import fcntl

    try:
        lock = os.open(runFolder, os.O_RDONLY)
    except FileNotFoundError:
        return None
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if os.path.samestat(os.fstat(lock), os.stat(runFolder)):
            return lock
    except OSError:
        pass
    os.close(lock)
    return None


def _closeToRuns(path: str, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except FileNotFoundError:
        pass
    except OSError as e:
        logger.warning(f"Could not close {path} to sandbox runs: {e}")


def _closeSharedFolders() -> None:
    """Close the shared temp folders and uv's lock files to runs.

    A clear has to list every shared folder to find its uid's files, so a run that
    fills one would make every other run's clear time out and lose its uid. A clear
    can't list a closed folder, so it costs nothing. If one can't be closed the
    clear goes on looking through it, and only that protection is lost. uv makes
    its lock files writable by anyone, and root owns them, so what a run wrote to
    one would outlive every clear. uv leaves them closed, and if it makes a new
    one the next claim closes that.
    """
    for folder in _SHARED_TEMP_FOLDERS:
        _closeToRuns(folder, _CLOSED_FOLDER_MODE)
    for lockFile in (os.path.join(_ROOT, ".venv", ".lock"), *glob.glob("/tmp/uv-*.lock")):
        _closeToRuns(lockFile, _CLOSED_FILE_MODE)


def _claimRun() -> tuple[int | None, str, int | None]:
    """Return this run's uid, its folder and the lock on that folder. The folder is the claim: only one run can create it.

    An image sets SANDBOX_RUN_ROOT to require a separate uid, and a parent that
    can't change uid then refuses instead of running without one. Running
    children as this process's own uid is for local development only, so it has
    to be asked for.
    """
    runRoot = os.environ.get("SANDBOX_RUN_ROOT")
    if not runRoot:
        if os.name == "posix" and os.geteuid() == 0:
            raise RuntimeError("SANDBOX_RUN_ROOT must be set: a root parent can't run sandboxes without a separate uid.")
        if os.environ.get("SANDBOX_ALLOW_SAME_UID") != "1":
            raise RuntimeError("Set SANDBOX_RUN_ROOT, or SANDBOX_ALLOW_SAME_UID=1 for local development.")
        return None, tempfile.mkdtemp(prefix="sandbox-"), None
    if os.geteuid() != 0:
        raise PermissionError("Sandboxes need a root parent when SANDBOX_RUN_ROOT is set.")
    _closeSharedFolders()
    for _ in range(_MAX_CLAIM_ATTEMPTS):
        runUid = _FIRST_RUN_UID + secrets.randbelow(_RUN_UID_COUNT)
        runFolder = os.path.join(runRoot, f"sandbox-{runUid}")
        try:
            os.mkdir(runFolder, 0o700)
        except FileExistsError:
            continue
        lock = _lockRun(runFolder)
        if lock is not None:  # Otherwise a sweep took it for an orphan, and it is removing it.
            return runUid, runFolder, lock
    raise RuntimeError("No sandbox uid is free.")


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


def _clearUid(runUid: int | None, runFolder: str) -> bool:
    """End every process of this uid and remove its files. Returns False unless the reaper cleared all it found.

    The reaper starts as root and drops itself, so that what a run left can't reach it.
    """
    if runUid is None:
        return True
    try:
        subprocess.run(
            [
                sys.executable, "-I", "-S", os.path.join(_ROOT, "utils", "sandbox_reaper.py"),
                str(runUid), str(runUid + _RUN_UID_COUNT), os.path.join(runFolder, "work"),
            ],
            env={},
            cwd="/",
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=_CLEAR_UID_TIMEOUT_SECONDS,
            check=True,
        )
        return True
    except (OSError, subprocess.SubprocessError) as e:
        reason = (getattr(e, "stderr", None) or b"")[-_MAX_REAPER_ERROR_BYTES:].decode("utf-8", "replace")
        logger.error(f"Could not clear sandbox uid {runUid}: {e} {reason}")
        return False


def _removeRun(runFolder: str, uidIsClear: bool) -> None:
    """Delete the run's files. The run folder frees the uid, so it stays while a process may be left."""
    if uidIsClear:
        shutil.rmtree(runFolder, ignore_errors=True)
        return
    for name in os.listdir(runFolder):
        shutil.rmtree(os.path.join(runFolder, name), ignore_errors=True)


def _sweepRun(runRoot: str, name: str) -> None:
    """Clear one folder of the run root, unless a live worker holds it."""
    if name == SLOTS_FOLDER:  # The run slots, not a run.
        return
    runUid = name.removeprefix("sandbox-")
    if not runUid.isdigit() or not _FIRST_RUN_UID <= int(runUid) < _FIRST_RUN_UID + _RUN_UID_COUNT:
        logger.warning(f"Sandbox sweep left {name!r}, which is not a run folder.")
        return
    runFolder = os.path.join(runRoot, name)
    lock = _lockRun(runFolder)
    if lock is None:
        return
    try:
        logger.warning(f"Sandbox sweep is clearing the orphaned run {name}.")
        _removeRun(runFolder, _clearUid(int(runUid), runFolder))
    finally:
        os.close(lock)


def sweepOrphanedRuns() -> None:
    """Clear runs whose worker died: their processes, and the copies of customer tables in their folders."""
    runRoot = os.environ.get("SANDBOX_RUN_ROOT")
    if not runRoot:
        return
    try:
        for name in os.listdir(runRoot):
            _sweepRun(runRoot, name)
    except Exception as e:
        logger.error(f"Sandbox sweep failed: {e}")


def serveTables(projectId: str, requests, replies, tablesFolder: str, runEnded: threading.Event) -> None:
    """Answer table requests until the run ends. Nothing the child sends can name a project."""
    spooled: dict[str, str] = {}
    spooledBytes = 0
    fileNumbers = itertools.count()
    while not runEnded.is_set():
        try:
            line = requests.readline(_MAX_REQUEST_BYTES)
        except OSError:
            return
        if not line:
            return
        tableName = None
        try:
            tableName = json.loads(line)
            if not isinstance(tableName, str):
                raise ValueError("A table request must be a table name.")
            if tableName not in spooled:
                if spooledBytes >= _MAX_SPOOL_BYTES:
                    raise ValueError("This run has already read as much table data as it can.")
                path = spool_table(projectId, tableName, os.path.join(tablesFolder, str(next(fileNumbers))), runEnded)
                os.chmod(path, 0o644)
                spooled[tableName] = os.path.basename(path)
                spooledBytes += os.path.getsize(path)
            reply = {"file": spooled[tableName]}
        except ValueError as e:
            reply = {"error": str(e)}
        except Exception as e:
            logger.warning(f"Sandbox table read failed for project {projectId}: {e}")
            reply = {"error": f"Could not read table {tableName!r}."}
        try:
            replies.write(json.dumps(reply).encode("utf-8") + b"\n")
        except OSError:
            return


def _refuseTables(projectId: str, requests, replies, tablesFolder: str, runEnded: threading.Event) -> None:
    """Answer every table request with a refusal: a trusted run is handed its bytes and reads nothing else."""
    while not runEnded.is_set():
        try:
            if not requests.readline(_MAX_REQUEST_BYTES):
                return
            replies.write(json.dumps({"error": "This run has no table access."}).encode("utf-8") + b"\n")
        except OSError:
            return


def _startChild(launcherName: str, runUid: int | None, workFolder: str, childEnd: socket.socket, stdout: IO[bytes], stderr: IO[bytes], limits: RunLimits, script: str | None = None) -> subprocess.Popen:
    """Start the launcher. Its output goes to files, so nothing a process holds open can block the parent."""
    env = {k: os.environ[k] for k in _INHERITED_ENV if k in os.environ}
    env["PYTHONPATH"] = os.pathsep.join(filter(None, (_ROOT, env.get("PYTHONPATH"))))
    if limits.threads > 0:
        # Each library sizes its pool when it loads, so the cap has to be in the environment before the child starts.
        env.update(dict.fromkeys(_THREAD_VARIABLES, str(limits.threads)))
    else:
        env.update({k: os.environ[k] for k in _THREAD_TUNING_ENV if k in os.environ})
    env.update(HOME=workFolder, TMPDIR=workFolder, USER="sandbox", LOGNAME="sandbox")
    options: dict = {}
    if runUid is not None:
        options.update(user=runUid, group=runUid, extra_groups=[])
    if os.name == "posix":
        options["pass_fds"] = (childEnd.fileno(),)
    return subprocess.Popen(
        [sys.executable, "-s", script or os.path.join(_ROOT, "utils", launcherName)],
        stdin=subprocess.PIPE,
        stdout=stdout,
        stderr=stderr,
        text=True,
        env=env,
        cwd=workFolder,
        close_fds=True,
        **options,
    )


def _channelFor(proc: subprocess.Popen, childEnd: socket.socket) -> dict:
    """Say how the child finds its end of the socket. Windows can only hand one to a known process."""
    if os.name == "nt":
        return {"share": base64.b64encode(childEnd.share(proc.pid)).decode("ascii")}
    return {"fd": childEnd.fileno()}


def readResult(path: str, runUid: int | None, maxBytes: int | None = None) -> bytes | None:
    """Read a file the child wrote. The child owns that folder, so a link there could point anywhere the parent can read, and a sparse file could fill its memory."""
    maxBytes = maxBytes or _MAX_RESULT_BYTES
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
        if info.st_size > maxBytes:
            raise ResultTooLargeError(f"Sandbox result is too large: {info.st_size} bytes.")
        return result.read(maxBytes)


def _waitForChild(proc: subprocess.Popen, payload: str, timeoutSeconds: int | None) -> str:
    """Send the payload and wait for the launcher, killing it at the timeout. Returns a note for stderr."""
    with contextlib.suppress(OSError):  # A launcher that died reports that through its exit code.
        with proc.stdin:
            proc.stdin.write(payload)
    try:
        proc.wait(timeoutSeconds if timeoutSeconds and timeoutSeconds > 0 else None)
        return ""
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        return f"\nExecution timed out after {timeoutSeconds} seconds.\n"


def _readOutput(output: IO[bytes]) -> str:
    """Read what the child printed, up to the cap."""
    output.seek(0)
    printed = output.read(_MAX_OUTPUT_BYTES + 1)
    if len(printed) > _MAX_OUTPUT_BYTES:
        return printed[:_MAX_OUTPUT_BYTES].decode("utf-8", "replace") + "\nOutput truncated.\n"
    return printed.decode("utf-8", "replace")


def _watchRun(proc: subprocess.Popen, runUid: int | None, workFolder: str, limits: RunLimits) -> RunWatchdog | None:
    """Start the watchdog for a run uid. A development run under the parent's own uid has none, nor has a run whose watched limits are all off."""
    if runUid is None or not (limits.maxRunMemory or limits.oomScoreAdj or limits.maxDiskBytes or limits.maxFiles):
        return None
    watchdog = RunWatchdog(runUid, workFolder, limits, lambda reason: proc.kill())
    watchdog.start()
    return watchdog


def _writeOomAdjustment(pid: int, value: int) -> None:
    with open(f"/proc/{pid}/oom_score_adj", "w") as target:
        target.write(str(value))


def _giveRunPriority(proc: subprocess.Popen, runUid: int | None, limits: RunLimits) -> None:
    """Set the run's OOM adjustment from the parent, before any generated code exists, so the watchdog can judge every process. A failed write ends the run."""
    if runUid is None or not limits.oomScoreAdj:
        return
    try:
        _writeOomAdjustment(proc.pid, limits.oomScoreAdj)
    except OSError:
        proc.kill()
        proc.wait()
        raise


def _stopReason(returncode: int, watchdogReason: str | None, timeoutNote: str, limits: RunLimits) -> str | None:
    """Why a limit ended the run, if one did. The watchdog's reason counts only if the run did not end well anyway."""
    if watchdogReason and returncode != 0:
        return watchdogReason
    if timeoutNote:
        return timeoutNote.strip()
    if returncode == -_SIGXCPU:
        return f"The run was stopped: it used more than {limits.maxCpuSeconds} CPU-seconds."
    if returncode == -_SIGKILL:
        return "The run was stopped: the server ran out of memory."
    return None


def _runChild(launcherName: str, projectId: str, code: str, timeoutSeconds: int | None, withResult: bool, runUid: int | None, runFolder: str, tablesFolder: str, workFolder: str, limits: RunLimits, kind: _RunKind = _RunKind()) -> SandboxResult:
    """Start the launcher, serve its table reads (a trusted run is refused them) and collect what it produced."""
    resultPath = os.path.join(workFolder, kind.resultName)
    previewPath = os.path.join(workFolder, kind.previewName) if kind.previewName else None
    parentEnd, childEnd = socket.socketpair()
    runEnded = threading.Event()
    with (
        parentEnd,
        parentEnd.makefile("rb") as requests,
        parentEnd.makefile("wb", buffering=0) as replies,
        tempfile.TemporaryFile(dir=runFolder) as stdout,
        tempfile.TemporaryFile(dir=runFolder) as stderr,
    ):
        with childEnd:
            proc = _startChild(launcherName, runUid, workFolder, childEnd, stdout, stderr, limits, kind.script)
            _giveRunPriority(proc, runUid, limits)
            payload = json.dumps({
                "projectId": projectId,
                "code": code,
                "result_path": resultPath,
                **({"preview_path": previewPath} if previewPath else {}),
                **(kind.payload or {}),
                "channel": {**_channelFor(proc, childEnd), "tablesFolder": tablesFolder, "parentPid": os.getpid(), "limits": limits.toChannel()},
            })
        server = threading.Thread(target=serveTables if kind.servesTables else _refuseTables, args=(projectId, requests, replies, tablesFolder, runEnded), daemon=True)
        server.start()
        watchdog = None
        try:
            watchdog = _watchRun(proc, runUid, workFolder, limits)
            timeoutNote = _waitForChild(proc, payload, timeoutSeconds)
        finally:
            if watchdog is not None:
                watchdog.stop()
            # Out of the child's reach: nothing it wrote to the socket can stop this or block it.
            runEnded.set()
            with contextlib.suppress(OSError):
                parentEnd.shutdown(socket.SHUT_RDWR)
            server.join()
        resultBytes = previewBytes = None
        if withResult and proc.returncode == 0:
            resultBytes = readResult(resultPath, runUid, kind.resultCap)
            previewBytes = readResult(previewPath, runUid, _MAX_PREVIEW_BYTES) if previewPath else None
        stopped = _stopReason(proc.returncode, watchdog.reason if watchdog else None, timeoutNote, limits)
        stopNote = f"\n{stopped}\n" if stopped and stopped not in timeoutNote else ""
        return SandboxResult(_readOutput(stdout), _readOutput(stderr) + timeoutNote + stopNote, proc.returncode, resultBytes, previewBytes, stopped)


def _inFreshRun(prepare: Callable[[int | None, str], None], run: Callable[[int | None, str, str, str, RunLimits], SandboxResult]) -> SandboxResult:
    """Take a slot and a uid, give the run its folders and call `run`. `prepare` runs once the folders exist, before any child."""
    limits = currentRunLimits()
    # The slot comes first, so a run that waits holds no uid and no folder.
    slot = acquireRunSlot(os.environ.get("SANDBOX_RUN_ROOT"), limits.maxConcurrentRuns, limits.slotWaitSeconds)
    try:
        runUid, runFolder, lock = _claimRun()
        uidIsClear = False
        try:
            # A worker that died mid-run can leave a process holding this uid, so nothing is handed over until it's gone.
            uidIsClear = _clearUid(runUid, runFolder)
            if not uidIsClear:
                raise RuntimeError("Could not clear the sandbox run's uid.")
            tablesFolder, workFolder = _prepareFolders(runUid, runFolder)
            uidIsClear = False
            try:
                prepare(runUid, tablesFolder)
                return run(runUid, runFolder, tablesFolder, workFolder, limits)
            finally:
                uidIsClear = _clearUid(runUid, runFolder)
        finally:
            _removeRun(runFolder, uidIsClear)
            if lock is not None:
                os.close(lock)
    finally:
        releaseRunSlot(slot)


def runSandbox(launcherName: str, projectId: str, code: str, timeoutSeconds: int | None, withResult: bool = False) -> SandboxResult:
    """Run generated code in a launcher subprocess, serving its table reads from this process.

    Only a launcher that writes a result sets `withResult`: the file is the child's, so it isn't read otherwise.
    """
    return _inFreshRun(
        lambda runUid, tablesFolder: None,
        lambda runUid, runFolder, tablesFolder, workFolder, limits: _runChild(launcherName, projectId, code, timeoutSeconds, withResult, runUid, runFolder, tablesFolder, workFolder, limits),
    )


def _writeInput(tablesFolder: str, inputBytes: bytes, runUid: int | None) -> str:
    """Write the run's input where it can read it and nothing else can change it. Returns its path.

    The folder is root's and closed to the run, so the file is created new (never through a link), owned by root
    with the run's group, and only then opened to that group: it is never readable by anyone else.
    """
    path = os.path.join(tablesFolder, _TRUSTED_INPUT_NAME)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "wb") as target:
        if runUid is not None:
            os.fchown(fd, os.geteuid(), runUid)
            os.fchmod(fd, _INPUT_FILE_MODE)
        target.write(inputBytes)
    return path


def runTrustedSandbox(launcherName: str, projectId: str, inputBytes: bytes, request: dict, timeoutSeconds: int, maxResultBytes: int, limits: RunLimits | None = None) -> SandboxResult:
    """Run one of our own launchers on bytes this process hands it, under every limit a code run has.

    The run is given no table access and no secret: `request` is the only thing besides the input that reaches it,
    so it must hold no credential. The launcher must call `enterSandbox` before it reads the input. Its answer is
    the file `work/result`, read through `readResult` up to `maxResultBytes`; a larger one raises ResultTooLargeError.
    The slot cap and the slot wait come from the environment (`currentRunLimits()`), not from `limits`.
    `request` must be a JSON-serialisable dict and `maxResultBytes` positive; both are checked before a slot is taken.
    Raises SandboxBusyError when no slot is free, as `runSandbox` does.
    """
    if not isinstance(request, dict):
        raise TypeError("A trusted run's request must be a dict.")
    json.dumps(request)  # Raises TypeError or ValueError for anything the child could not be sent.
    if maxResultBytes <= 0:
        raise ValueError("A trusted run needs a positive result cap.")
    script = _TRUSTED_LAUNCHERS.get(launcherName)
    if script is None:
        raise ValueError(f"{launcherName!r} is not a trusted launcher.")
    inputPath: list[str] = []

    def runIt(runUid, runFolder, tablesFolder, workFolder, runLimits):
        kind = _RunKind(
            resultName=_TRUSTED_RESULT_NAME,
            resultCap=maxResultBytes,
            previewName=None,
            servesTables=False,
            script=os.path.join(_ROOT, *script.split("/")),
            payload={"input_path": inputPath[0], "request": request},
        )
        return _runChild(launcherName, projectId, "", timeoutSeconds, True, runUid, runFolder, tablesFolder, workFolder, limits or runLimits, kind)

    return _inFreshRun(lambda runUid, tablesFolder: inputPath.append(_writeInput(tablesFolder, inputBytes, runUid)), runIt)


def _withinHardLimit(wanted: int, hard: int) -> int:
    """Ask for no more than the hard limit, which only a privileged process can raise. RLIM_INFINITY is negative here."""
    return wanted if hard < 0 else min(wanted, hard)


def _lockDownChild(channel: dict) -> None:
    """Child side: limits a run's uid can't raise, set before any generated code runs.

    A run under this process's own uid is local development, where these would hit the developer's other processes.
    """
    if sys.platform != "linux" or not _FIRST_RUN_UID <= os.getuid() < _FIRST_RUN_UID + _RUN_UID_COUNT:
        return
    import resource

    libc = ctypes.CDLL(None, use_errno=True)
    for option, value in ((_PR_SET_NO_NEW_PRIVS, 1), (_PR_SET_PDEATHSIG, signal.SIGKILL)):
        if libc.prctl(option, ctypes.c_ulong(value), ctypes.c_ulong(0), ctypes.c_ulong(0), ctypes.c_ulong(0)) != 0:
            raise OSError(ctypes.get_errno(), "prctl failed")
    if os.getppid() != channel["parentPid"]:  # The worker died before the signal was set.
        sys.exit(1)
    limits = RunLimits.fromChannel(channel["limits"])
    if limits.oomScoreAdj:
        # Make this run the OOM killer's first pick. A failed write must stop the run, as a failed prctl does.
        fd = os.open("/proc/self/oom_score_adj", os.O_WRONLY)
        try:
            os.write(fd, str(limits.oomScoreAdj).encode("ascii"))
        except OSError as error:
            raise OSError(error.errno, "could not set the memory priority") from error
        finally:
            os.close(fd)
    # Limits every process of a run shares. Threads count towards the process limit.
    wanted = [
        (resource.RLIMIT_NPROC, limits.maxProcesses, limits.maxProcesses),
        (resource.RLIMIT_FSIZE, limits.maxFileBytes, limits.maxFileBytes),
        (resource.RLIMIT_CORE, 0, 0),
        (resource.RLIMIT_NOFILE, _MAX_RUN_OPEN_FILES, _MAX_RUN_OPEN_FILES),
        (resource.RLIMIT_DATA, limits.maxProcessMemory, limits.maxProcessMemory),
        (resource.RLIMIT_CPU, limits.maxCpuSeconds, limits.maxCpuSeconds + _CPU_HARD_LIMIT_GRACE_SECONDS),
    ]
    for limit, soft, hard in wanted:
        # Zero turns a limit off, except for core files, which are always off.
        if limit != resource.RLIMIT_CORE and soft == 0:
            continue
        ceiling = resource.getrlimit(limit)[1]
        resource.setrlimit(limit, (_withinHardLimit(soft, ceiling), _withinHardLimit(hard, ceiling)))
    if limits.blockNetwork:
        # Last step: refuses non-local sockets and uncounted shared memory. A failure stops the run.
        installFilter()


def _connectTableChannel(channel: dict) -> None:
    """Child side: send every table read to the parent, which holds the credentials and the project."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    if os.name == "nt":
        parent = socket.fromshare(base64.b64decode(channel["share"]))
    else:
        parent = socket.socket(fileno=channel["fd"])
    requests = parent.makefile("wb")
    replies = parent.makefile("rb")
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
            return pa.ipc.open_stream(pa.memory_map(path)).read_all()
        return pq.read_table(path)

    use_table_source(readTable)


def _capArrowThreads(limits: RunLimits) -> None:
    """Child side: Arrow's pools are sized from the machine, not the environment, so cap them here."""
    if limits.threads > 0:
        import pyarrow as pa

        pa.set_cpu_count(limits.threads)
        pa.set_io_thread_count(limits.threads)


def enterSandbox(channel: dict) -> None:
    """Child side: every launcher calls this once, before it runs generated code."""
    _lockDownChild(channel)
    _capArrowThreads(RunLimits.fromChannel(channel["limits"]))
    _connectTableChannel(channel)
