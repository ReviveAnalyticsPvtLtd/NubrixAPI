"""Seccomp filter for a run: refuses network sockets, io_uring and shared memory the run's memory limits don't count.

The filter is a classic BPF program over `struct seccomp_data` (nr at 0, arch at 4, args[0] at 16).
Anything it doesn't name is allowed, so ordinary code, threads and forks work as before.
"""

import ctypes
import errno
import platform

AF_UNIX = 1
_PR_SET_NO_NEW_PRIVS = 38
_SECCOMP_SET_MODE_FILTER = 1
_SECCOMP_FILTER_FLAG_TSYNC = 1  # Apply to every thread of the process, not just the caller.

_SECCOMP_RET_KILL_PROCESS = 0x80000000
_SECCOMP_RET_ERRNO = 0x00050000
_SECCOMP_RET_ALLOW = 0x7FFF0000
_REFUSE = _SECCOMP_RET_ERRNO | errno.EPERM

_LD_W_ABS = 0x20
_JEQ_K = 0x15
_JGE_K = 0x35
_RET_K = 0x06

_OFFSET_NR = 0
_OFFSET_ARCH = 4
_OFFSET_ARG0 = 16  # Low 32 bits on little-endian machines, which both supported ones are.

_X32_FLAG = 0x40000000

# machine: (AUDIT_ARCH, {syscall: number})
_SECCOMP_SYSCALL = {"x86_64": 317, "aarch64": 277}
_ARCHES = {
    "x86_64": (0xC000003E, {"socket": 41, "socketpair": 53, "io_uring_setup": 425, "io_uring_enter": 426,
                            "io_uring_register": 427, "memfd_create": 319, "shmget": 29}),
    "aarch64": (0xC00000B7, {"socket": 198, "socketpair": 199, "io_uring_setup": 425, "io_uring_enter": 426,
                             "io_uring_register": 427, "memfd_create": 279, "shmget": 194}),
}
_SOCKET_CALLS = ("socket", "socketpair")
_REFUSED_CALLS = ("io_uring_setup", "io_uring_enter", "io_uring_register", "memfd_create", "shmget")


class _Filter(ctypes.Structure):
    _fields_ = [("code", ctypes.c_ushort), ("jt", ctypes.c_ubyte), ("jf", ctypes.c_ubyte), ("k", ctypes.c_uint)]


class _Program(ctypes.Structure):
    _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.POINTER(_Filter))]


def buildFilter(machine: str) -> list[tuple[int, int, int, int]]:
    """The program for one machine as (code, jt, jf, k) rows. Raises OSError for a machine it doesn't know."""
    if machine not in _ARCHES:
        raise OSError(errno.ENOSYS, f"no seccomp filter for machine {machine!r}")
    arch, numbers = _ARCHES[machine]
    # Rows hold label names for jumps, resolved to relative offsets below.
    rows: list[tuple] = [
        ("label", "start"),
        ("ld", _OFFSET_ARCH),
        ("jeq", arch, "arch_ok", "kill"),
        ("label", "arch_ok"),
    ]
    rows += [("ld", _OFFSET_NR)]
    if machine == "x86_64":
        rows += [("jge", _X32_FLAG, "kill", "numbered")]
    rows += [("label", "numbered")]
    for name in _SOCKET_CALLS:
        rows += [("jeq", numbers[name], "family", f"after_{name}"), ("label", f"after_{name}")]
    for name in _REFUSED_CALLS:
        rows += [("jeq", numbers[name], "refuse", f"after_{name}"), ("label", f"after_{name}")]
    rows += [
        ("ret", _SECCOMP_RET_ALLOW),
        ("label", "family"),
        ("ld", _OFFSET_ARG0),
        ("jeq", AF_UNIX, "allow", "refuse"),
        ("label", "allow"),
        ("ret", _SECCOMP_RET_ALLOW),
        ("label", "refuse"),
        ("ret", _REFUSE),
        ("label", "kill"),
        ("ret", _SECCOMP_RET_KILL_PROCESS),
    ]
    labels: dict[str, int] = {}
    index = 0
    for row in rows:
        if row[0] == "label":
            labels[row[1]] = index
        else:
            index += 1
    program: list[tuple[int, int, int, int]] = []
    for row in rows:
        if row[0] == "label":
            continue
        here = len(program)
        if row[0] == "ld":
            program.append((_LD_W_ABS, 0, 0, row[1]))
        elif row[0] == "ret":
            program.append((_RET_K, 0, 0, row[1]))
        else:
            code = _JEQ_K if row[0] == "jeq" else _JGE_K
            jt, jf = labels[row[2]] - here - 1, labels[row[3]] - here - 1
            if not (0 <= jt <= 255 and 0 <= jf <= 255):
                raise OSError(errno.EINVAL, "seccomp jump out of range")
            program.append((code, jt, jf, row[1]))
    return program


def installFilter(machine: str | None = None) -> None:
    """Install the filter on every thread of this process and its future children. Raises OSError on any failure."""
    machine = machine or platform.machine()
    rows = buildFilter(machine)
    instructions = (_Filter * len(rows))(*[_Filter(*row) for row in rows])
    program = _Program(len(rows), instructions)  # ctypes keeps `instructions` alive for as long as `program` is, which covers the syscall below.
    libc = ctypes.CDLL(None, use_errno=True)
    zero = ctypes.c_ulong(0)
    if libc.prctl(_PR_SET_NO_NEW_PRIVS, ctypes.c_ulong(1), zero, zero, zero) != 0:
        raise OSError(ctypes.get_errno(), "prctl failed")
    # prctl(PR_SET_SECCOMP) would filter only this thread. TSYNC covers the threads native libraries already started.
    # It returns the id of a thread it couldn't sync, so any non-zero result is a failure.
    result = libc.syscall(_SECCOMP_SYSCALL[machine], _SECCOMP_SET_MODE_FILTER, _SECCOMP_FILTER_FLAG_TSYNC, ctypes.byref(program))
    if result != 0:
        raise OSError(ctypes.get_errno() if result == -1 else errno.EBUSY, "could not install the seccomp filter")
