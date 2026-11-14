"""
codeExecutor.py — sandboxed subprocess code execution with per-project concurrency lock.

Runs LLM-generated code in an isolated subprocess with stdout/stderr capture.
Uses a Redis Sorted Set (ZSET) soft concurrency gate per projectId. Each run
also gets the sandbox's limits (utils/sandboxLimits.py): a 300 second default
timeout, caps on threads, processes, memory, CPU, disk and files, and a
container-wide cap on how many runs go at once. RLIMIT_AS is not used because it
counts address space that polars reserves without using.
"""

__all__ = ["replManager", "REPLManager", "_remove_code_fences"]


import contextlib
import os
import time
import uuid
import re
from api.services.credits.creditService import creditService
from utils.logger import logger
from utils.sandboxLimits import currentRunLimits, defaultTimeoutSeconds
from utils.sandboxRunner import SandboxResult, runSandbox, runSqlSandbox
from utils.sandboxSlots import SandboxBusyError


def _remove_code_fences(code: str) -> str:
    """Strip ```python ... ``` fences if present."""
    return re.sub(r"^```(?:python)?\s*\n|\s*```\s*$", "", code.strip(), flags=re.MULTILINE)


class REPLManager:
    """
    Executes code strings inside an isolated subprocess.

    No hard CPU/memory caps (RLIMIT_AS would crash polars import).
    Concurrency is bounded per project via Redis ZSET semaphore (soft cap).
    """

    def __init__(self, timeoutSeconds: int | None = None, maxConcurrentPerProject: int = 50):
        if timeoutSeconds is None:
            timeoutSeconds = defaultTimeoutSeconds("CODE_EXEC_TIMEOUT_SECONDS")
        self.timeoutSeconds = timeoutSeconds
        self.maxConcurrentPerProject = maxConcurrentPerProject

    @contextlib.contextmanager
    def _projectSlot(self, projectId: str, waitSeconds: float):
        """Take one entry of the project's Redis semaphore, trying for up to `waitSeconds` (at least one attempt).

        Yields whether an entry was taken and releases it on exit. A Redis failure counts as taken, as it always has.
        """
        r = creditService._redis()
        sem_key = f"semaphore:{projectId}"
        slot_id = str(uuid.uuid4())
        # An entry may wait for a run slot as well as run, so it isn't stale before both are over.
        staleSeconds = self.timeoutSeconds + 60 + currentRunLimits().slotWaitSeconds
        acquired = False
        start_time = time.time()
        while True:
            try:
                r.zremrangebyscore(sem_key, 0, time.time() - staleSeconds)
                count = r.zcard(sem_key)
                if count < self.maxConcurrentPerProject:
                    r.zadd(sem_key, {slot_id: time.time()})
                    r.expire(sem_key, int(staleSeconds))
                    acquired = True
                    break
            except Exception as e:
                logger.warning(f"Failed to check/acquire concurrency semaphore for project {projectId}: {e}")
                acquired = True
                break
            if time.time() - start_time >= waitSeconds:
                break
            time.sleep(0.2)
        try:
            yield acquired
        finally:
            if acquired:
                try:
                    r.zrem(sem_key, slot_id)
                except Exception as e:
                    logger.warning(f"Failed to release semaphore for project {projectId}: {e}")

    def runSql(self, request: dict, projectId: str, waitForSlot: bool = True) -> SandboxResult:
        """Run one SQL chart query under the project's semaphore. Raises SandboxBusyError when the project or the sandbox is full.

        With `waitForSlot` False it makes one attempt at each, so a caller that can retry later is never held up.
        """
        with self._projectSlot(projectId, 30.0 if waitForSlot else 0) as acquired:
            if not acquired:
                raise SandboxBusyError(f"Too many running code execution tasks for project '{projectId}'.")
            if waitForSlot:
                return runSqlSandbox(projectId=projectId, request=request, timeoutSeconds=self.timeoutSeconds)
            return runSqlSandbox(projectId=projectId, request=request, timeoutSeconds=self.timeoutSeconds, slotWaitSeconds=0)

    def run(self, codeString: str, projectId: str = None, filters: list | None = None) -> str:
        if not projectId:
            return "Error: projectId is required for sandboxed code execution."

        if "```" in codeString:
            codeString = _remove_code_fences(codeString)

        # 1. Per-tenant concurrency limits: ZSET-based semaphore (soft cap)
        with self._projectSlot(projectId, 30.0) as acquired:
            if not acquired:
                return f"Concurrency limit reached: Too many running code execution tasks for project '{projectId}'. Please try again in a few seconds."

            # 2. Launch subprocess
            stdout = ""
            stderr = ""
            returnCode = None
            stopped = None
            try:
                extra = {"payload": {"filters": filters}} if filters is not None else {}
                stdout, stderr, returnCode, _, _, stopped = runSandbox("sandbox_launcher.py", projectId, codeString, self.timeoutSeconds, **extra)
            except SandboxBusyError:
                # Not output: callers must not treat a full sandbox as a code failure. The semaphore entry is released on exit.
                raise
            except Exception as e:
                stderr = f"Error during subprocess execution: {e}\n"

        if stopped:
            # Tell the caller why the run ended, after whatever it printed. The sentence on the end keeps a stopped
            # run's output from parsing as a result, even when the output already holds it.
            if not stdout.strip():
                return stopped
            return stdout + "\n" + stopped
        # Callers accept any valid JSON as the result, so a failed run's partial stdout must not reach them.
        if returnCode:
            return stderr or f"Code execution exited with code {returnCode} and wrote no error output."
        return stdout or stderr


replManager = REPLManager(timeoutSeconds=None, maxConcurrentPerProject=50)