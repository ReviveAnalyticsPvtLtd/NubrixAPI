# Nubrix backend

Nubrix's backend serves the FastAPI API and runs background jobs with Celery and Redis.

## Run tests locally

Install uv and use Python 3.12, as specified in `.python-version`. From the backend repository root, run

```sh
uv sync --locked --group dev
uv run --no-sync pytest -q -rs
```

The default suite uses test configuration and skips integrations that need explicitly configured services.

### Disposable integration and release checks

Start a local Docker engine with Docker Compose v2. From the repository root, run

```sh
python scripts/run_local_checks.py
```

This runs the PostgreSQL billing and subscription tests, Redis credit tests, website-visit repository and ACL tests, the migration rehearsal, and Linux sandbox tests. It also boots the release image and checks `/readyz`, a Celery worker ping, and the Beat scheduler. `/readyz` returns 503 when PostgreSQL or Redis is unavailable.

Use `integration`, `sandbox`, or `smoke` as the command argument to run one group. The command prints the JUnit output directory. Use `--reports <directory>` to retain reports at a specific path. Each run has a separate Docker project and removes its containers and volumes on exit. PostgreSQL and Redis use an internal Docker network without host ports, and the application uses fixture credentials supplied by Compose.

The migration rehearsal starts from the synthetic legacy billing schema shared with the PostgreSQL integration tests. It applies the tracked SQL files in version order and checks the resulting tables, publication, and grants. The transaction rolls back when the test closes the connection. The release-only billing contract has its own prerequisite tests in the billing suite.

The default Docker target is `runtime`, which installs locked runtime dependencies during the build. The `test` target shares those layers and adds the dev dependencies.

## GitHub Actions

[Backend CI](.github/workflows/subscriptionModel.yaml) runs on pull requests to `main` and the existing subscription integration branches, on pushes to `main`, and on manual dispatch.

| Job | What it verifies |
| --- | --- |
| `unit` | Default pytest suite, with opt-in integration skips reported in JUnit |
| `integration` | Disposable PostgreSQL and Redis, including credit recovery, website visits, and the migration rehearsal. All selected cases must run. |
| `sandbox-isolation` | Linux sandbox tests in the test image. The two cases that require a non-root parent can skip. |
| `release-smoke` | Boots the release image and checks `/readyz`, the Celery worker, and Beat. |
| `backend-required` | Shows whether all four jobs passed. Merge requirements are configured separately in GitHub. |

The integration, sandbox, and release smoke jobs use the same runner script as the local checks. Download `pytest-unit`, `pytest-integration`, and `pytest-sandbox` from the workflow run for the JUnit reports. The release smoke results appear in the job log.

Celery Beat owns recurring subscription jobs. The [manual subscription expiry workflow](.github/workflows/subscriptionCron.yaml) uses service credentials and can update subscription records.
