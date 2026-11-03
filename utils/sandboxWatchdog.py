"""Measures a sandbox run and stops it when it passes its memory, disk or file limit.

Why: the kernel limits a single process, but a run can have many processes, and
its work folder is not covered by a per-process limit. So the parent adds up
what the run uid uses and kills the launcher on a breach. It only reads /proc
and the work folder, and a process that exits mid-read counts as zero.
"""

import contextlib
import os
import threading
from collections.abc import Callable

from utils.sandboxLimits import RunLimits, _formatBytes

_MEMORY_INTERVAL_SECONDS = 0.1
_DISK_INTERVAL_SECONDS = 1.0
_KIB = 1024


def _statusFields(status: bytes) -> dict[bytes, int]:
    """The numeric fields of a /proc status file that this module reads, by name."""
    fields = {}
    for line in status.splitlines():
        name, _, rest = line.partition(b":")
        if name in (b"Uid", b"RssAnon", b"RssShmem", b"Seccomp_filters"):
            with contextlib.suppress(ValueError, IndexError):
                fields[name] = int(rest.split()[0])
    return fields


def _runProcesses(runUid: int) -> list[tuple[str, dict[bytes, int]]]:
    """Id and status fields of each process whose real uid is the run's. One that exits while being read is skipped."""
    found = []
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/status", "rb") as source:
                fields = _statusFields(source.read())
        except OSError:
            continue
        if fields.get(b"Uid") == runUid:
            found.append((name, fields))
    return found


def runMemoryBytes(runUid: int) -> int:
    """Memory the run's processes hold: anonymous plus shared anonymous pages, which RSS-only counting would miss."""
    return sum((fields.get(b"RssAnon", 0) + fields.get(b"RssShmem", 0)) * _KIB for _, fields in _runProcesses(runUid))


def oomPriorityLowered(runUid: int, wanted: int) -> bool:
    """Whether any run process has an OOM score adjustment below the one the run was given.

    A process counts once it has more seccomp filters than this one, because its own filter is installed after its
    adjustment was set. Before that it still has its parent's. Without that count (an older kernel) nothing is judged.
    """
    with open("/proc/self/status", "rb") as own:
        inherited = _statusFields(own.read()).get(b"Seccomp_filters")
    if wanted <= 0 or inherited is None:
        return False
    for name, fields in _runProcesses(runUid):
        if fields.get(b"Seccomp_filters", 0) <= inherited:
            continue
        try:
            with open(f"/proc/{name}/oom_score_adj", "rb") as source:
                if int(source.read().strip()) < wanted:
                    return True
        except (OSError, ValueError):
            continue
    return False


def workUsage(folder: str, maxBytes: int, maxFiles: int) -> tuple[int, int, bool]:
    """Disk bytes and entries under a folder, without following links. Stops walking once a limit (0 = none) is passed."""
    used = entries = 0
    pending = [folder]
    while pending:
        try:
            with os.scandir(pending.pop()) as listing:
                for entry in listing:
                    entries += 1
                    try:
                        info = entry.stat(follow_symlinks=False)
                        isFolder = entry.is_dir(follow_symlinks=False)
                    except OSError:
                        continue
                    blocks = getattr(info, "st_blocks", None)
                    used += blocks * 512 if blocks is not None else info.st_size
                    if (maxBytes and used > maxBytes) or (maxFiles and entries > maxFiles):
                        return used, entries, True
                    if isFolder:
                        pending.append(entry.path)
        except OSError:
            continue
    return used, entries, False


class RunWatchdog(threading.Thread):
    """Checks a run's memory and OOM priority every 0.1 s and its work folder every second, and calls `onBreach` once."""

    def __init__(self, runUid: int, workFolder: str, limits: RunLimits, onBreach: Callable[[str], None]):
        super().__init__(name="sandbox-watchdog", daemon=True)
        self.runUid = runUid
        self.workFolder = workFolder
        self.limits = limits
        self.onBreach = onBreach
        self.reason: str | None = None
        self._stopped = threading.Event()

    def stop(self) -> None:
        self._stopped.set()
        if self.is_alive() and threading.current_thread() is not self:
            self.join()

    def _breach(self, reason: str) -> None:
        self.reason = reason
        self.onBreach(reason)

    def _checkMemory(self) -> bool:
        limits = self.limits
        if limits.maxRunMemory and runMemoryBytes(self.runUid) > limits.maxRunMemory:
            self._breach(f"The run was stopped: it used more than {_formatBytes(limits.maxRunMemory)} of memory.")
            return True
        if oomPriorityLowered(self.runUid, limits.oomScoreAdj):
            self._breach("The run was stopped: it changed its own memory priority.")
            return True
        return False

    def _checkWork(self) -> bool:
        limits = self.limits
        if not (limits.maxDiskBytes or limits.maxFiles):
            return False
        used, _, over = workUsage(self.workFolder, limits.maxDiskBytes, limits.maxFiles)
        if not over:
            return False
        if limits.maxDiskBytes and used > limits.maxDiskBytes:
            self._breach(f"The run was stopped: it wrote more than {_formatBytes(limits.maxDiskBytes)} to disk.")
        else:
            self._breach(f"The run was stopped: it created more than {limits.maxFiles} files.")
        return True

    def run(self) -> None:
        elapsed = nextWork = 0.0
        while not self._stopped.wait(_MEMORY_INTERVAL_SECONDS):
            elapsed += _MEMORY_INTERVAL_SECONDS
            if self._checkMemory():
                return
            if elapsed >= nextWork:
                nextWork = elapsed + _DISK_INTERVAL_SECONDS
                if self._checkWork():
                    return
