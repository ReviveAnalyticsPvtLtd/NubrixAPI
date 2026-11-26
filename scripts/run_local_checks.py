"""Run backend checks against disposable Docker services on this machine."""

import argparse
import os
import subprocess
import tempfile
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("check", nargs="?", choices=("all", "integration", "sandbox", "smoke"), default="all")
    parser.add_argument("--reports", type=Path, help="JUnit output directory (default: a new system temp folder)")
    args = parser.parse_args()

    environment = {key: value for key, value in os.environ.items() if not key.startswith("COMPOSE_")}
    environment["COMPOSE_DISABLE_ENV_FILE"] = "1"
    project = f"nubrix-checks-{uuid.uuid4().hex[:12]}"
    compose = ["docker", "compose", "-f", str(ROOT / "test/local/compose.yaml"),
               "--project-name", project, "--profile", "checks", "--profile", "smoke"]

    def run(*command, check=True):
        return subprocess.run([*compose, *command], cwd=ROOT, env=environment, check=check)

    try:
        context = environment.get("DOCKER_CONTEXT")
        host = environment.get("DOCKER_HOST")
        if context or not host:
            if not context:
                context = subprocess.check_output(["docker", "context", "show"], text=True, env=environment).strip()
            environment["DOCKER_CONTEXT"] = context
            environment.pop("DOCKER_HOST", None)
            host = subprocess.check_output(
                ["docker", "context", "inspect", context, "--format", "{{.Endpoints.docker.Host}}"],
                text=True, env=environment,
            ).strip()
        if not host.startswith(("unix://", "npipe:////./pipe/")):
            parser.error("the disposable stack requires a local Docker context")
    except (OSError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"Docker could not be checked: {exc}\n")

    reports = args.reports.resolve() if args.reports else Path(tempfile.mkdtemp(prefix="nubrix-reports-"))
    reports.mkdir(parents=True, exist_ok=True)
    # The root-only logs parent keeps the host mount out of sandbox runs.
    report_mount = f"{reports}:/app/logs/check-reports"
    print(f"Docker project: {project}\nJUnit reports: {reports}", flush=True)
    result = 0
    try:
        if args.check in {"all", "integration"}:
            run("run", "--build", "--rm", "-T", "--volume", report_mount, "integration")
        if args.check in {"all", "sandbox"}:
            run("run", "--build", "--rm", "-T", "--volume", report_mount, "sandbox")
        if args.check in {"all", "smoke"}:
            run("up", "--build", "--wait", "--wait-timeout", "150", "app")
            run("exec", "-T", "app", "python", "-m", "test.local.smoke")
    except subprocess.CalledProcessError as exc:
        run("logs", "--no-color", "--tail", "40", check=False)
        result = exc.returncode
    finally:
        cleanup = run("down", "--volumes", "--rmi", "local", check=False)
    return result or cleanup.returncode


if __name__ == "__main__":
    raise SystemExit(main())
