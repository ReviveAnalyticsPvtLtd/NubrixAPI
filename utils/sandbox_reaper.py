"""
sandbox_reaper.py — ends whatever a sandbox run left behind.

Generated code can fork, start its own session and outlive the launcher, and the
run's uid comes round again for another project. sandboxRunner.py starts this as
root, before and after a run, with -I -S, so it imports only the standard library.

Before it touches anything it gives up root for good: its effective uid becomes
the run's and its real and saved uids a spare uid no run has. The effective uid
is its whole reach, so it can only signal that uid's processes and only remove
what that uid could remove. The other two put it out of that uid's reach. A
process can only signal or trace another whose real or saved uid match its own,
so nothing a run left can stop this or make it report a clean exit. An exec
would copy the effective uid over the saved one, so this never starts anything.
"""

import ctypes
import os
import signal
import sys
import time

# Every folder a run's uid could write to outside its own run folder. sandboxRunner.py
# closes them, and a uid can't list a closed one, so only one it couldn't close costs anything.
_SHARED_TEMP_FOLDERS = ("/tmp", "/var/tmp", "/dev/shm", "/dev/mqueue", "/run/lock")
_PROCESS_EXIT_SECONDS = 10
_IPC_RMID = 0


def _capabilities() -> int:
    """Return the capabilities this process still holds, which should be none."""
    with open("/proc/self/status") as status:
        return sum(int(line.split()[1], 16) for line in status if line.startswith(("CapPrm", "CapEff", "CapAmb")))


def _becomeRunUid(uid: int, spareUid: int) -> None:
    """Give up root. kill(-1) with a real uid of 0 or any capability left would reach every process in the container."""
    if os.geteuid() != 0 or uid == 0 or spareUid == 0 or uid == spareUid:
        sys.exit("sandbox_reaper must start as root, with two separate uids that aren't root.")
    os.setgroups([])
    os.setresgid(spareUid, uid, spareUid)
    os.setresuid(spareUid, uid, spareUid)
    if os.getresuid() != (spareUid, uid, spareUid) or _capabilities():
        sys.exit("sandbox_reaper could not give up root.")


def _liveProcesses(uid: int) -> int:
    """Count the processes holding this uid, leaving out this one. A zombie holds nothing and can only wait for its parent."""
    live = 0
    for pid in os.listdir("/proc"):
        if not pid.isdigit() or int(pid) == os.getpid():
            continue
        try:
            with open(f"/proc/{pid}/status", "rb") as status:  # Bytes, because a process names itself.
                fields = dict(line.split(b":", 1) for line in status if line.startswith((b"State:", b"Uid:")))
        except OSError:
            continue  # It has just gone.
        if fields.get(b"State", b"Z").split()[0] not in (b"Z", b"X") and str(uid).encode() in fields.get(b"Uid", b"").split():
            live += 1
    return live


def _endProcesses(uid: int) -> int:
    """End every process of this uid and return how many are still alive when time runs out."""
    deadline = time.monotonic() + _PROCESS_EXIT_SECONDS
    while True:
        try:
            # One call, because the kernel can't let a forking process slip past it. A scan of /proc could.
            os.kill(-1, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass  # Nothing to signal. Some kernels say so with EPERM, and the count below is what decides.
        live = _liveProcesses(uid)
        if not live or time.monotonic() >= deadline:
            return live
        time.sleep(0.02)


def _entries() -> list:
    with os.scandir(".") as entries:
        return list(entries)


def _leaveFolder() -> bool:
    """Go up a level. If the folder above has been locked, this walk can't go on."""
    try:
        os.chdir("..")
    except OSError:
        return False
    return True


def _removeOwned(folder: str, uid: int) -> int:
    """Remove everything this uid owns below a folder and return how many entries it couldn't.

    A run can lock its folders or nest them past the longest path, so this moves
    with chdir, which costs no descriptors and no path length. It enters only this
    uid's folders, or one run's files could use up every other run's clear. What
    another uid left in them goes too: the owner of a folder can remove any file in
    it, sticky bit or not. Another uid's folder only goes if it's empty, and the
    count then keeps this uid claimed.
    """
    try:
        if os.lstat(folder).st_uid == uid:
            os.chmod(folder, 0o700)
        os.chdir(folder)
        walk = [("", iter(_entries()))]
    except OSError:
        return 0
    left = 0
    while walk:
        name, entries = walk[-1]
        removing = len(walk) > 1
        entry = next(entries, None)
        if entry is None:
            walk.pop()
            if walk:
                if not _leaveFolder():
                    return left + 1
                try:
                    os.rmdir(name)
                except OSError:
                    left += 1
            continue
        mustGo = removing
        try:
            owned = entry.stat(follow_symlinks=False).st_uid == uid
            mustGo = removing or owned
            if not entry.is_dir(follow_symlinks=False):
                if mustGo:
                    os.unlink(entry.name)
            elif owned:
                os.chmod(entry.name, 0o700)
                os.chdir(entry.name)
                try:
                    walk.append((entry.name, iter(_entries())))
                except OSError:
                    if not _leaveFolder():
                        return left + 1
                    raise
            elif removing:
                os.rmdir(entry.name)
        except OSError:
            left += mustGo
    return left


def _ipcObjects(table: str, uid: int) -> list[int]:
    """List the System V objects this uid owns or created. The owner can be changed, the creator can't."""
    try:
        with open(f"/proc/sysvipc/{table}") as source:
            header, *rows = [line.split() for line in source]
    except (OSError, ValueError):
        return []  # This kernel doesn't show them.
    owner, creator = header.index("uid"), header.index("cuid")
    return [int(row[1]) for row in rows if str(uid) in (row[owner], row[creator])]


def _removeIpc(uid: int) -> int:
    """Remove this uid's System V objects, which outlive every process, and return how many remain."""
    libc = ctypes.CDLL(None, use_errno=True)
    removers = {
        "shm": lambda objectId: libc.shmctl(objectId, _IPC_RMID, None),
        "msg": lambda objectId: libc.msgctl(objectId, _IPC_RMID, None),
        "sem": lambda objectId: libc.semctl(objectId, 0, _IPC_RMID, None),
    }
    for table, remove in removers.items():
        for objectId in _ipcObjects(table, uid):
            remove(objectId)
    return sum(len(_ipcObjects(table, uid)) for table in removers)


def main() -> None:
    uid, spareUid = int(sys.argv[1]), int(sys.argv[2])
    _becomeRunUid(uid, spareUid)
    live = _endProcesses(uid)
    # Extra folders are the run's own, passed by the parent.
    entries = sum(_removeOwned(folder, uid) for folder in (*_SHARED_TEMP_FOLDERS, *sys.argv[3:]))
    objects = _removeIpc(uid)
    if live or entries or objects:
        sys.exit(f"{live} processes, {entries} files and {objects} System V objects remain.")


if __name__ == "__main__":
    main()
