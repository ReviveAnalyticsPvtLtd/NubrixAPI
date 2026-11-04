"""Measures a sandbox run and stops it when it passes its memory, disk or file limit.

Why: the kernel limits a single process, but a run can have many processes, and
its work folder is not covered by a per-process limit. So the parent adds up
what the run uid uses and kills the launcher on a breach. It only reads /proc
and the work folder, and a process that exits mid-read counts as zero.
"""

import contextlib
import os
import threading
from collections.abc import Callable, Iterator

from utils.logger import logger
from utils.sandboxLimits import RunLimits, _formatBytes

_MEMORY_INTERVAL_SECONDS = 0.1
_DISK_INTERVAL_SECONDS = 1.0
_KIB = 1024


def _runFields(statusPath: str) -> tuple[int, int] | None:
    """(real uid, anonymous plus shared anonymous bytes) from a status file, read only as far as needed."""
    uid = anonymous = shared = None
    with open(statusPath, "rb") as source:
        for line in source:
            name, _, rest = line.partition(b":")
            if name == b"Uid":
                uid = int(rest.split()[0])
            elif name == b"RssAnon":
                anonymous = int(rest.split()[0])
            elif name == b"RssShmem":
                shared = int(rest.split()[0])
            if uid is not None and anonymous is not None and shared is not None:
                break
    if uid is None:
        return None
    return uid, ((anonymous or 0) + (shared or 0)) * _KIB


def scanRun(runUid: int, wanted: int) -> tuple[int, bool]:
    """One pass over /proc: the memory the run's processes hold, and whether any has an OOM adjustment below `wanted`.

    Memory counts anonymous plus shared anonymous pages, which RSS alone would miss. A process whose entry belongs
    to neither the run uid nor root is skipped before anything in it is opened, because a run's processes can't change uid.
    A process that exits while being read counts as zero.
    """
    total = 0
    lowered = False
    with os.scandir("/proc") as listing:
        for entry in listing:
            if not entry.name.isdigit():
                continue
            try:
                # A process that is not dumpable can have its entry owned by root, so root's are read too.
                if entry.stat().st_uid not in (runUid, 0):
                    continue
                fields = _runFields(f"/proc/{entry.name}/status")
                if fields is None or fields[0] != runUid:
                    continue
                total += fields[1]
                if wanted > 0 and not lowered:
                    with open(f"/proc/{entry.name}/oom_score_adj", "rb") as source:
                        lowered = int(source.read().strip()) < wanted
            except (OSError, ValueError, IndexError):
                continue
    return total, lowered


def runMemoryBytes(runUid: int) -> int:
    """Memory the run's processes hold."""
    return scanRun(runUid, 0)[0]


def oomPriorityLowered(runUid: int, wanted: int) -> bool:
    """Whether any run process has an OOM score adjustment below the one the run was given."""
    return scanRun(runUid, wanted)[1]


def _entryStats(folder: str) -> Iterator[os.stat_result]:
    """Link-level stats of everything under a folder. Each subfolder is opened from its parent's descriptor without following links, so a folder swapped for a link mid-walk is not entered."""
    if hasattr(os, "fwalk"):
        for _, folders, files, descriptor in os.fwalk(folder, follow_symlinks=False):
            for name in folders + files:
                try:
                    yield os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                except OSError:
                    continue
        return
    pending = [folder]  # Windows has no descriptor walk. There are no run uids there.
    while pending:
        try:
            with os.scandir(pending.pop()) as listing:
                for entry in listing:
                    try:
                        info = entry.stat(follow_symlinks=False)
                        if entry.is_dir(follow_symlinks=False):
                            pending.append(entry.path)
                    except OSError:
                        continue
                    yield info
        except OSError:
            continue


def workUsage(folder: str, maxBytes: int, maxFiles: int) -> tuple[int, int, bool]:
    """Disk bytes and entries under a folder, without following links. Stops walking once a limit (0 = none) is passed."""
    used = entries = 0
    with contextlib.closing(_entryStats(folder)) as stats:
        for info in stats:
            entries += 1
            blocks = getattr(info, "st_blocks", None)
            used += blocks * 512 if blocks is not None else info.st_size
            if (maxBytes and used > maxBytes) or (maxFiles and entries > maxFiles):
                return used, entries, True
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
        used, lowered = scanRun(self.runUid, limits.oomScoreAdj)
        if limits.maxRunMemory and used > limits.maxRunMemory:
            self._breach(f"The run was stopped: it used more than {_formatBytes(limits.maxRunMemory)} of memory.")
            return True
        if lowered:
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
        try:
            self._watch()
        except Exception:
            # An unwatched run could pass every limit, so a watchdog that fails ends the run.
            logger.exception("Sandbox watchdog failed; stopping the run.")
            self._breach("The run was stopped: the server ran out of memory.")

    def _watch(self) -> None:
        elapsed = nextWork = 0.0
        while not self._stopped.wait(_MEMORY_INTERVAL_SECONDS):
            elapsed += _MEMORY_INTERVAL_SECONDS
            if self._checkMemory():
                return
            if elapsed >= nextWork:
                nextWork = elapsed + _DISK_INTERVAL_SECONDS
                if self._checkWork():
                    return
