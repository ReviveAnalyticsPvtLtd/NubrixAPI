"""A container-wide cap on how many sandbox runs are held at once.

The API is several processes (web workers and Celery), so the slots are files that processes lock with flock:
a lock goes when its holder does, however it dies.
"""

import json
import os
import stat
import threading
import time

from utils.logger import logger

SLOT_POLL_SECONDS = 0.1
SLOTS_FOLDER = "slots"
BUSY_CODE = "SANDBOX_BUSY"
BUSY_MESSAGE = "Too many analyses are running right now. Please try again in a moment."

# A shadow needs at least 2 free slots, so a cap below 2 refuses every one. That is said once per process.
_shadowDisabledWarned = False
_shadowDisabledLock = threading.Lock()


class SandboxBusyError(RuntimeError):
    """Every slot stayed held for the whole wait, or the slots can't be made safe."""

    def __init__(self, message: str = BUSY_MESSAGE) -> None:
        super().__init__(message)


def isBusy(error: BaseException) -> bool:
    """True for the busy error itself, or for the 503 an executor made from it."""
    return isinstance(error, SandboxBusyError) or getattr(error, "errorCode", None) == BUSY_CODE


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
            except BlockingIOError:  # Held by another run.
                os.close(fd)
            except OSError as e:
                os.close(fd)
                logger.error(f"Could not lock sandbox slot {number}: {e}")
                raise SandboxBusyError() from e
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


def freeRunSlots(runRoot: str | None, maxRuns: int) -> int | None:
    """How many of the `maxRuns` slots are free right now, or None where the cap is off or can't apply here.

    Tries each slot's lock and lets it go at once, so every slot is left as it was found. The answer can be out of date by
    the time it is used, which is fine for a caller that only wants to know whether the sandbox is clearly idle.
    """
    if not runRoot or maxRuns <= 0:
        return None
    try:
        import fcntl
    except ImportError:
        return None
    folder = _slotsFolder(runRoot)
    free = 0
    for number in range(maxRuns):
        try:
            fd = os.open(os.path.join(folder, str(number)), os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
        except OSError as e:
            logger.error(f"Could not open sandbox slot {number}: {e}")
            raise SandboxBusyError() from e
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
            free += 1
        except BlockingIOError:  # Held by a run.
            pass
        except OSError as e:
            logger.error(f"Could not lock sandbox slot {number}: {e}")
            raise SandboxBusyError() from e
        finally:
            os.close(fd)
    return free


def requireHeadroom(runRoot: str | None, maxRuns: int) -> None:
    """Raise SandboxBusyError unless at least `max(2, maxRuns // 2)` slots are free, so a shadow run leaves room for served ones.

    Does nothing where the cap is off. It takes no slot: the caller takes its own afterwards. A cap below 2 refuses every
    shadow; the first such refusal in a process logs one `chart_shadow_disabled` warning saying why.
    """
    global _shadowDisabledWarned
    free = freeRunSlots(runRoot, maxRuns)
    if free is not None and free < max(2, maxRuns // 2):
        if maxRuns < 2:
            with _shadowDisabledLock:
                warn, _shadowDisabledWarned = not _shadowDisabledWarned, True
            if warn:
                logger.warning("chart_shadow_disabled " + json.dumps({"reason": "sandbox cap below 2"}))
        raise SandboxBusyError()
