"""The seccomp filter a run gets: what it refuses, what it leaves alone, and that a run can still read its tables.

The evaluator tests run anywhere. The `separateUids` tests need a root parent on Linux, as in test_sandbox_storage_reads.
"""

import errno

import pytest

from test.test_sandbox_storage_reads import PROJECT, runInSandbox, separateUids, sharedFoldersOpen, storage  # noqa: F401
from utils.sandboxSeccomp import buildFilter, installFilter

KILL = 0x80000000
ALLOW = 0x7FFF0000
REFUSED = 0x00050000 | errno.EPERM
X86 = {"arch": 0xC000003E, "socket": 41, "socketpair": 53, "io_uring": (425, 426, 427), "memfd_create": 319, "shmget": 29,
       "read": 0, "write": 1, "mmap": 9, "clone": 56}
ARM = {"arch": 0xC00000B7, "socket": 198, "socketpair": 199, "io_uring": (425, 426, 427), "memfd_create": 279, "shmget": 194,
       "read": 63, "write": 64, "mmap": 222, "clone": 220}
MACHINES = {"x86_64": X86, "aarch64": ARM}
AF_UNIX, AF_INET, AF_INET6, AF_NETLINK, AF_PACKET, AF_VSOCK = 1, 2, 10, 16, 17, 40


def evaluate(program, nr, arch, arg0=0):
    """Run a classic-BPF program over a synthetic seccomp_data. Fails on any opcode the filter isn't meant to use."""
    data = {0: nr, 4: arch, 16: arg0 & 0xFFFFFFFF}
    accumulator = 0
    counter = 0
    for _ in range(len(program) + 1):
        code, jt, jf, k = program[counter]
        counter += 1
        if code == 0x20:  # LD | W | ABS
            assert k in data, f"load from an offset the evaluator doesn't model: {k}"
            accumulator = data[k]
        elif code in (0x15, 0x35):  # JEQ | K, JGE | K
            hit = accumulator == k if code == 0x15 else accumulator >= k
            counter += jt if hit else jf
        elif code == 0x06:  # RET | K
            return k
        else:
            pytest.fail(f"opcode {code:#x} is not one the evaluator implements")
    pytest.fail("program did not return")


@pytest.mark.parametrize("machine", MACHINES)
@pytest.mark.parametrize("call", ("socket", "socketpair"))
def test_filterRefusesEveryFamilyButUnix(machine, call):
    program, numbers = buildFilter(machine), MACHINES[machine]
    for family in (AF_INET, AF_INET6, AF_NETLINK, AF_PACKET, AF_VSOCK):
        assert evaluate(program, numbers[call], numbers["arch"], family) == REFUSED, family
    assert evaluate(program, numbers[call], numbers["arch"], AF_UNIX) == ALLOW
    # Only the low 32 bits of the first argument are the family.
    assert evaluate(program, numbers[call], numbers["arch"], (1 << 32) | AF_INET) == REFUSED
    assert evaluate(program, numbers[call], numbers["arch"], (1 << 32) | AF_UNIX) == ALLOW


@pytest.mark.parametrize("machine", MACHINES)
def test_filterRefusesIoUringAndUncountedMemory(machine):
    program, numbers = buildFilter(machine), MACHINES[machine]
    for nr in (*numbers["io_uring"], numbers["memfd_create"], numbers["shmget"]):
        assert evaluate(program, nr, numbers["arch"]) == REFUSED, nr


@pytest.mark.parametrize("machine", MACHINES)
def test_filterKillsAWrongArchitecture(machine):
    program, numbers = buildFilter(machine), MACHINES[machine]
    other = ARM["arch"] if machine == "x86_64" else X86["arch"]
    assert evaluate(program, numbers["read"], other) == KILL
    assert evaluate(program, numbers["socket"], other, AF_UNIX) == KILL
    assert evaluate(program, numbers["read"], 0x40000003) == KILL  # 32-bit x86


def test_filterKillsX32Syscalls():
    program = buildFilter("x86_64")
    assert evaluate(program, 0x40000000, X86["arch"]) == KILL
    assert evaluate(program, 0x40000000 | X86["socket"], X86["arch"], AF_INET) == KILL
    assert evaluate(program, 0x3FFFFFFF, X86["arch"]) == ALLOW


@pytest.mark.parametrize("machine", MACHINES)
def test_filterAllowsOrdinarySyscalls(machine):
    program, numbers = buildFilter(machine), MACHINES[machine]
    for name in ("read", "write", "mmap", "clone"):
        assert evaluate(program, numbers[name], numbers["arch"]) == ALLOW, name


def test_unknownMachineFailsClosed():
    with pytest.raises(OSError):
        buildFilter("riscv64")
    with pytest.raises(OSError):
        installFilter("riscv64")


def _tableReads(storage):
    return [r for r in storage.requests if r["method"] == "GET" and r["path"].endswith(f"{PROJECT}/sales.parquet")]


@separateUids
def test_runCantConnectToTheNetworkButStillReadsTables(runInSandbox, storage, monkeypatch):  # noqa: F811
    # A failed run reports its traceback, so the table read is shown by what the storage served before the connection.
    code = (
        f'sales = fetch_data_pl("{PROJECT}", "sales")\n'
        f'outcome = pd.io.common.urlopen("{storage.url}/").status'
    )

    output = runInSandbox(code)
    assert "Operation not permitted" in output
    assert _tableReads(storage)

    monkeypatch.setenv("SANDBOX_BLOCK_NETWORK", "0")
    storage.requests.clear()
    output = runInSandbox(code)
    assert "Operation not permitted" not in output
    assert _tableReads(storage)
    assert [r for r in storage.requests if r["path"] == "/"]  # The connection reached the server.


@separateUids
def test_runCantCreateMemfdOrSysvMemory(runInSandbox):  # noqa: F811
    assert "PermissionError" in runInSandbox('outcome = pd.io.common.os.memfd_create("x")')
    code = (
        'os = pd.io.common.os\n'
        'ctypes = os.sys.modules["ctypes"]\n'
        'libc = ctypes.CDLL(None, use_errno=True)\n'
        'created = libc.shmget(0, 4096, 0o1600)\n'
        'outcome = ctypes.get_errno() if created == -1 else "created"'
    )

    assert runInSandbox(code).strip() == str(errno.EPERM)
