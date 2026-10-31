"""
codeExecutor.py — sandboxed subprocess code execution with per-project concurrency lock.

Runs LLM-generated code in an isolated subprocess with stdout/stderr capture.
Uses a Redis Sorted Set (ZSET) soft concurrency gate per projectId; no hard
CPU/memory caps (RLIMIT_AS crashes polars import).
"""

__all__ = ["replManager", "REPLManager", "_remove_code_fences"]


import sys
import os
import json
import subprocess
import time
import uuid
import re
from api.services.credits.creditService import creditService
from utils.logger import logger


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
            env_val = os.environ.get("CODE_EXEC_TIMEOUT_SECONDS", "0")
            timeoutSeconds = int(env_val) if env_val else None
        self.timeoutSeconds = timeoutSeconds
        self.maxConcurrentPerProject = maxConcurrentPerProject

    def run(self, codeString: str, projectId: str = None) -> str:
        if not projectId:
            return "Error: projectId is required for sandboxed code execution."

        if "```" in codeString:
            codeString = _remove_code_fences(codeString)

        # 1. Per-tenant concurrency limits: ZSET-based semaphore (soft cap)
        r = creditService._redis()
        sem_key = f"semaphore:{projectId}"
        slot_id = str(uuid.uuid4())

        acquired = False
        start_time = time.time()
        while time.time() - start_time < 30.0:
            try:
                r.zremrangebyscore(sem_key, 0, time.time() - (self.timeoutSeconds + 60))
                count = r.zcard(sem_key)
                if count < self.maxConcurrentPerProject:
                    r.zadd(sem_key, {slot_id: time.time()})
                    r.expire(sem_key, self.timeoutSeconds + 60)
                    acquired = True
                    break
            except Exception as e:
                logger.warning(f"Failed to check/acquire concurrency semaphore for project {projectId}: {e}")
                acquired = True
                break
            time.sleep(0.2)

        if not acquired:
            return f"Concurrency limit reached: Too many running code execution tasks for project '{projectId}'. Please try again in a few seconds."

        # 2. Launch subprocess
        stdout = ""
        stderr = ""
        launcher_path = os.path.join(os.path.dirname(__file__), "sandbox_launcher.py")
        try:
            app_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            inherit_envs = (
                "PATH", "PYTHONPATH", "HOME", "LANG", "LC_ALL", "TZ",
                "REDIS_HOST", "REDIS_PORT", "REDIS_PASSWORD", "REDIS_DB",
                "REDIS_SEMAPHORE_DB", "SUPABASE_URL", "SUPABASE_KEY", "DATABASE_URL",
                "STORAGE_URL",
                "GOOGLE_API_KEY", "GEMINI_API_KEY", "GROQ_API_KEY", "CEREBRAS_API_KEY",
                "OPENAI_API_KEY", "LANGSMITH_API_KEY", "LOGTAIL_SOURCE_TOKEN",
                "RAZORPAY_KEY_ID", "RAZORPAY_KEY_SECRET", "BREVO_API_KEY",
                "LANGFUSE_SECRET_KEY", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_HOST",
                "POLARS_MAX_THREADS", "OPENBLAS_NUM_THREADS",
                "DF_CACHE_MAX_BYTES", "DF_CACHE_MAX_ENTRIES",
            )
            clean_env = {k: os.environ[k] for k in inherit_envs if k in os.environ}
            clean_env["PYTHONPATH"] = app_root + os.pathsep + clean_env.get("PYTHONPATH", "")
            proc = subprocess.Popen(
                [sys.executable, "-s", launcher_path],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=clean_env,
                close_fds=True,
            )

            payload = json.dumps({"projectId": projectId, "code": codeString})
            try:
                if self.timeoutSeconds:
                    stdout, stderr = proc.communicate(input=payload, timeout=self.timeoutSeconds)
                else:
                    stdout, stderr = proc.communicate(input=payload)
            except subprocess.TimeoutExpired:
                proc.kill()
                stdout, stderr = proc.communicate()
                stderr += f"\nExecution timed out after {self.timeoutSeconds} seconds.\n"
        except Exception as e:
            stderr = f"Error during subprocess execution: {e}\n"
        finally:
            if acquired:
                try:
                    r.zrem(sem_key, slot_id)
                except Exception as e:
                    logger.warning(f"Failed to release semaphore for project {projectId}: {e}")

        if stdout:
            return stdout
        return stderr


replManager = REPLManager(timeoutSeconds=None, maxConcurrentPerProject=50)