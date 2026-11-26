# Nubrix backend

Nubrix's backend serves the FastAPI API and runs background jobs with Celery and Redis.

## Run tests locally

Install uv and use Python 3.12, as specified in `.python-version`. From the backend repository root, run

```sh
uv sync --locked --group dev
uv run --no-sync pytest -q -rs
```

The default suite uses test configuration and skips integrations that need explicitly configured services. Use disposable local services for integration tests.

## GitHub Actions

[Backend CI](.github/workflows/subscriptionModel.yaml) runs on pull requests to `main` and the existing subscription integration branches, on pushes to `main`, and on manual dispatch.

| Job | What it verifies |
| --- | --- |
| `unit` | Default pytest suite, with opt-in integration skips reported in JUnit |
| `postgres` | Selected billing and subscription tests on disposable PostgreSQL 17, with zero skips required |
| `sandbox-isolation` | Linux sandbox tests inside the built image, with skips limited to the two cases that require a non-root parent |
| `backend-required` | Passes only when all three test jobs pass |

Download `pytest-unit`, `pytest-postgres`, and `pytest-sandbox` from the workflow run to see the JUnit reports. `backend-required` is the stable job to select when configuring required checks on `main`.

Celery Beat owns recurring subscription jobs. The [manual subscription expiry workflow](.github/workflows/subscriptionCron.yaml) uses service credentials and can update subscription records.
