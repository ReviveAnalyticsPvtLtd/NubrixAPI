"""Check the API, worker, and Beat from inside the disposable release container."""

from pathlib import Path
from time import monotonic, sleep
from urllib.request import urlopen

import psutil

from nubrix.triggers.celery import celeryApp


with urlopen("http://127.0.0.1:7860/readyz", timeout=5) as response:
    if response.status != 200:
        raise SystemExit(f"Unexpected API readiness status: {response.status}")
print("API readiness passed", flush=True)

def beat_ready():
    if not Path("celerybeat-schedule").is_file():
        return False
    for process in psutil.process_iter(["cmdline"]):
        command = process.info["cmdline"] or []
        if "beat" in command and any(Path(arg).name == "celery" for arg in command):
            return True
    return False


deadline = monotonic() + 60
worker_ready = False
while monotonic() < deadline:
    if not worker_ready and celeryApp.control.ping(timeout=3):
        worker_ready = True
    if worker_ready and beat_ready():
        print("Celery worker responded and Beat initialized its schedule", flush=True)
        break
    sleep(1)
else:
    if not worker_ready:
        raise SystemExit("The Celery worker did not respond")
    raise SystemExit("Celery Beat did not initialize its schedule")
