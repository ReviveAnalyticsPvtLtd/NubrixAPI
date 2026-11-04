"""A container-wide cap on how many sandbox runs are held at once.

The API is several processes (web workers and Celery), so the slots are files that processes lock with flock:
a lock goes when its holder does, however it dies.
"""

import os
import stat
import time

from utils.logger import logger

SLOT_POLL_SECONDS = 0.1
SLOTS_FOLDER = "slots"
BUSY_MESSAGE = "Too many analyses are running right now. Please try again in a moment."


class SandboxBusyError(RuntimeError):
    """Every slot stayed held for the whole wait, or the slots can't be made safe."""

    def __init__(self, message: str = BUSY_MESSAGE) -> None:
        super().__init__(message)


def _slotsFolder(runRoot: str) -> str:
    """Return the slots folder, made private to root where this process is root. Raise rather than run uncapped."""
    folder = os.path.join(runRoot, SLOTS_FOLDER)
    try:
        os.makedirs(folder, mode=0o700, exist_ok=True)
        if os.name == "posix" and os.geteuid() == 0:
            info = os.lstat(folder)
            if not stat.S_ISDIR(info.st_mode):
                raise OSError("the slots path is not a folder")
            if info.st_uid != 0:
                os.chown(folder, 0, 0)
            if stat.S_IMODE(info.st_mode) != 0o700:
                os.chmod(folder, 0o700)
            info = os.lstat(folder)
            if info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o700:
                raise OSError("the slots folder could not be made private")
    except OSError as e:
        logger.error(f"Sandbox slots folder {folder} is unusable: {e}")
        raise SandboxBusyError() from e
    return folder


def acquireRunSlot(runRoot: str | None, maxRuns: int, waitSeconds: float) -> int | None:
    """Hold one of `maxRuns` slots and return its fd, or None when the cap is off or can't apply here.

    Cycles through the slots until `waitSeconds` has passed, then raises SandboxBusyError. A wait of 0 tries each slot once.
    The fd is not inheritable, so a run's children never keep a slot alive.
    """
    if not runRoot or maxRuns <= 0:
        return None
    try:
        import fcntl
    except ImportError:  # Windows has no flock; development there runs uncapped.
        return None
    folder = _slotsFolder(runRoot)
    deadline = time.monotonic() + max(waitSeconds, 0)
    while True:
        for number in range(maxRuns):
            try:
                fd = os.open(os.path.join(folder, str(number)), os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
            except OSError as e:
                logger.error(f"Could not open sandbox slot {number}: {e}")
                raise SandboxBusyError() from e
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd
            except OSError:
                os.close(fd)
        if time.monotonic() >= deadline:
            raise SandboxBusyError()
        time.sleep(SLOT_POLL_SECONDS)


def releaseRunSlot(fd: int | None) -> None:
    """Free a slot. Closing the fd drops the lock."""
    if fd is not None:
        try:
            os.close(fd)
        except OSError:
            pass
