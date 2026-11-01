"""
sandbox_reaper.py — ends whatever a sandbox run left behind.

sandboxRunner.py starts this as the run's uid, so it can only reach that run's
processes and files. Generated code can fork, start its own session and outlive
the launcher, and the run's uid comes round again for another project.

It runs with -I -S, before and after a run, so it imports only the standard library.
"""

import os
import shutil
import signal
import sys

# Every folder a run's uid can write to outside its own run folder.
_SHARED_TEMP_FOLDERS = ("/tmp", "/var/tmp", "/dev/shm", "/dev/mqueue", "/run/lock")


def _removeOwnFiles(folder: str, uid: int) -> None:
    """Remove what this uid owns at the top of a shared folder. Sticky folders hold other uids' files too."""
    try:
        entries = list(os.scandir(folder))
    except OSError:
        return
    for entry in entries:
        try:
            if entry.stat(follow_symlinks=False).st_uid != uid:
                continue
            if entry.is_dir(follow_symlinks=False):
                shutil.rmtree(entry.path, ignore_errors=True)
            else:
                os.unlink(entry.path)
        except (OSError, RecursionError):
            continue


def main() -> None:
    uid = os.getuid()
    # kill(-1) signals everything this uid may signal, so it must never run as anyone else.
    if uid == 0 or uid != int(sys.argv[1]):
        sys.exit("sandbox_reaper must run as the run's own uid.")
    try:
        # One call, because the kernel can't let a forking process slip past it. A scan of /proc could.
        os.kill(-1, signal.SIGKILL)
    except ProcessLookupError:
        pass
    for folder in _SHARED_TEMP_FOLDERS:
        _removeOwnFiles(folder, uid)


if __name__ == "__main__":
    main()
