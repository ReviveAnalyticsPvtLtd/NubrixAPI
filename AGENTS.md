# AGENTS.md

NubrixAI Analytics Hub API — FastAPI + Celery + LangChain monolith.

## Stack
- Python 3.13 (pinned via `.python-version`). Use `uv` for deps; lockfile is `uv.lock`.
- FastAPI on gunicorn (uvicorn worker class), port 7860, mounted at `/api/latest`.
- Celery worker + beat on Redis for billing/subscription/credit/cron jobs.
- LangChain + LangGraph orchestrating LLM components (Gemini via Google GenAI, Groq, Cerebras, OpenAI via OpenRouter).
- Polars as the primary data engine for LLM-generated code (pandas fallback retained for backward compat).
- Supabase (Postgres + Storage), Razorpay billing, Langfuse tracing, Logtail logging.

## Run / Build
- Dev server: `uv run uvicorn main:app --reload --port 7860`. Swagger at `/api/latest/documentation/docs`.
- Production (Docker): `uv sync` then `gunicorn main:app ...` (see `supervisord.conf`). `startup.sh` runs supervisord (fastapi + celery worker + celery beat).
- Single celery task locally: `uv run celery -A nubrix.triggers.celery.celeryApp worker --loglevel=info --concurrency=2`.
- Beat only: `uv run celery -A nubrix.triggers.celery.celeryApp beat --loglevel=info`.

## Layout
- `main.py` — FastAPI app; root_path `/api/latest`; mounts 12 routers (see `app.include_router(...)`).
- `api/routers/` — HTTP layer. `api/services/` — business logic. `api/commons.py` — Supabase client, JWT auth deps (`verifyUser`, `verifyToken`, `requireActiveSubscription`, `requireCredits`, etc.).
- `nubrix/components/` — LLM agents: `codeGenerator`, `codeDebugger`, `queryRephraser`, `metadataGenerator`, `insightGenerator`, `domainKpiMapper`, `reportGenerator`, `dashboardNameGenerator`, `pdfTableExtractor`, `imageToInsights`, `speechToText`, `signalEngine`, `transformationAgent`, `subscriptionManager`, etc.
- `nubrix/components/transformationExecutor.py` — Subprocess-based transformation code execution.
- `nubrix/components/llmChainFactory.py` — `buildLlmChain(section, promptKey)` factory (replaces 6 boilerplate chain-builder classes).
- `nubrix/workflows/` — LangGraph compositions: `reportingToolWorkflow`, `parallelReportingToolWorkflow`. Both have `_route_large_tables_to_scan()` + `_injectProjectId()` + `json.dumps` normalizer post-processors.
- `nubrix/triggers/celery.py` — `celeryApp` + beat schedule for recurring jobs.
- `nubrix/triggers/tasks/` — concrete Celery tasks.
- `utils/` — `llm.py` (LLM factories + `_LLM_CACHE` + `cleanThinkTokens`), `logger.py` (loguru + Logtail + `logs/runLogs.log`), `codeExecutor.py` (subprocess sandbox), `sandbox_launcher.py` (subprocess entry for code execution), `transform_launcher.py` (subprocess entry for transformation validate), `inspect_launcher.py` (subprocess entry for transformation inspect), `exceptionHandler.py`, `langfuseClient.py`, `llmOutputParser.py`, `sizing.py` (server-aware auto-sizing).
- `utils/initMethods.py` — Data layer: `fetch_data` (pandas), `fetch_data_pl` (Polars eager), `scan_data` (Polars lazy), Arrow LRU cache, `classify_table_size`, `serializer`, `invalidate_data_cache`.
- `prompts.yaml` — LLM prompt templates keyed by component. All Polars-first, `{{...}}` escaped. `codeTemplates.yaml` — output scaffolding. `config.ini` — model/temperature/maxTokens per component. `nubrix.utils.getConfig(section)` reads `config.ini`.
- `config/credits.json`, `config/tax_rules.json` — static configs.

## Conventions & Gotchas
- `utils/llm.py:14-16` forces `GOOGLE_GENAI_USE_VERTEXAI=false` and `NO_GCE_CHECK=true` — Gemini always uses the API key path. Don't set `GOOGLE_GENAI_USE_VERTEXAI=true` in `.env` for local runs; the override wins but logs will be confusing.
- CORS is `allow_origins=["*"]` with `allow_credentials=True` (main.py:50). Don't tighten without checking the frontend.
- Auth: JWT HS256 signed with `SECRET_KEY` env var; token must contain `email`. See `api/commons.py:_verifyTokenInternal`. Auth deps are **sync** (not async).
- HTTPException detail can be a `dict` with `status`+`message` — main.py:40 flattens that into the response body instead of wrapping in `{"detail": ...}`. Use `raiseHttpException(...)` / `CustomException` from `utils/exceptionHandler.py` to get the flat shape.
- Celery: beat schedule is in `nubrix/triggers/celery.py` (UTC). All times are UTC. `pastDueSuspension` and `entitlementBoundary` run every 30 min; `reconciliation` every 15 min; `creditReconciliation` hourly; billing/expiry sweeps are daily at 00:00–02:00 UTC. No queue split — single celery worker handles all tasks.
- `pyproject.toml` has no `[tool.pytest]` / lint config and there is no test directory — there are no automated tests. CI is a single GitHub Actions workflow `.github/workflows/subscriptionCron.yaml` that runs `python nubrix/components/subscriptionManager.py` daily. Do not invent a test command.
- `prompts.yaml`, `codeTemplates.yaml`, `config.ini` are loaded by absolute path via `os.path.join(os.getcwd(), ...)` in `CodeGenerator` and friends — run from repo root.
- `.env` is required and gitignored. Required keys: `SECRET_KEY`, `SUPABASE_URL`, `SUPABASE_KEY`, `DATABASE_URL`, `REDIS_HOST/PORT/PASSWORD`, plus provider keys (`GEMINI_API_KEY`/`GOOGLE_API_KEY`, `GROQ_API_KEY`, `CEREBRAS_API_KEY`, `OPENAI_API_KEY`, `LANGSMITH_*`, `LOGTAIL_*`, `RAZORPAY_*`, `BREVO_API_KEY`, `BREVO_SENDER_EMAIL`, `BREVO_SENDER_NAME`, `DASHBOARD_BASE_URL`, `LANGFUSE_*`).
- `AGENTS.md` and `CLAUDE.md` are gitignored on purpose — this file is for the local agent, not for git.
- `OPENBLAS_NUM_THREADS=2` is set in `.env` to throttle BLAS (numpy/scipy) in the container.

## Sandbox Execution
- **Code execution** (`utils/codeExecutor.py` + `utils/sandbox_launcher.py`): LLM-generated code runs in a subprocess (`subprocess.Popen` with `close_fds=True`). No fork, no RLIMIT caps, no inherited sockets. Restricted builtins (`__import__` whitelisted). Timeout configurable via `CODE_EXEC_TIMEOUT_SECONDS` (default `0` = no timeout). Soft per-project Redis ZSET semaphore (default 50/project).
- **Transformation execution** (`nubrix/components/transformationExecutor.py` + `utils/transform_launcher.py` + `utils/inspect_launcher.py`): Same subprocess pattern. Child writes parquet to temp file; parent reads it. Preview uses `pl.read_parquet(...).head(100)` — only 100 rows loaded. Timeout via `TRANSFORMATION_TIMEOUT_SECONDS` (default `0` = no timeout).
- **Project scope**: all three launchers raise `PermissionError` when `fetch_data`, `fetch_data_pl` or `scan_data` names another project. This catches generated code that asks for the wrong project. It is not a security boundary: the sandboxes still hold the service key and generated code can reach it. In `transform_launcher.py` and `inspect_launcher.py` the first argument stays named `projectId`, because generated code uses `projectId` and may pass it by keyword.
- **Child environment**: `inherit_envs` in `codeExecutor.py` and `transformationExecutor.py` passes only what a table read uses: `SUPABASE_URL`, `SUPABASE_KEY`, `REDIS_*` and the path, locale and tuning variables. Generated code can read all of it through `pd.io.common.os.environ`, so never add a secret the launcher import chain doesn't read. This doesn't hide the parent's environment: a child with the parent's uid can read it from `/proc`.
- **Why subprocess not fork**: Fork inherits parent's open Redis/HTTP sockets → deadlock when child uses `fetch_data_pl`. `close_fds=True` in `Popen` prevents this.

## Data Layer (`utils/initMethods.py`)
- `fetch_data(projectId, table)` → `pd.DataFrame` (backward compat).
- `fetch_data_pl(projectId, table)` → `pl.DataFrame` (eager, zero-copy from Arrow cache).
- `scan_data(projectId, table)` → `pl.LazyFrame` (predicate/projection pushdown).
- Arrow LRU cache bounded by `DF_CACHE_MAX_BYTES` + `DF_CACHE_MAX_ENTRIES`.
- `classify_table_size(projectId, table)` → `{rows_estimate, columns, size_class, hint}`. Drives workflow auto-routing.
- `serializer` — converts Polars types for `json.dumps`.
- `invalidate_data_cache(projectId, table)` — call after transformations.
- Every AnalyticsHub read goes through `utils/projectStorage.py` with the service key, in the API, Celery and the sandboxes. Never add a read through a public URL: `test_storage_read_paths.py` fails on `FILE_URL` and on stray `urlopen` calls. `DOMAIN_FILE_URL` is a separate public bucket.

## Table Viewer Endpoint
- `GET /api/latest/projects/viewTable/{projectId}/{tableName}?page=1&pageSize=100`
- Uses `readTablePage` (`utils/projectStorage.py`): an authenticated HEAD, the parquet footer and the row groups holding the page, read with Range GETs.
- Do not go back to `pl.scan_parquet` here. Polars 1.42 silently drops `storage_options` on HTTP URLs, and a signed URL makes storage read the whole object on Polars' HEAD.
- Auth via `verifyToken`. Max page size 500.

## Active/Inactive Tables
- Every table in `metadata.json` has an `isActive` boolean. Missing key = active (backward compat).
- `PATCH /api/latest/projects/toggleTableActive/{projectId}/{tableName}` — toggles `isActive`, persists to storage. Auth via `verifyToken`.
- `managementService.filterActiveTables(metadata)` — shared helper, returns only active entries. All LLM-facing consumers use it:
  - `reportingService._getProjectMetadata` (filters on return, caches full metadata)
  - `transformationService._get_metadata` (same pattern)
  - `managementService._generateRawInsights` (filters before LLM)
  - `blendService.getDataSources` / `getFieldsFromSources` (raw tables only)
  - `reportGenerator.getAllTables` (only active tables)
  - `insightContextBuilder.build` (filters metadata)
- `getMetadata` endpoint returns ALL tables with `isActive` flag (frontend shows active/inactive status).
- New tables get `isActive: true` automatically. Metadata regeneration preserves existing `isActive` for unchanged tables.

## Table Name Collision Guard
- `managementService.validateTableNameAvailable(projectId, tableName)` — raises `409` if name exists in metadata (active or inactive).
- Called at every table creation entry point: CSV/Excel/MySQL/PostgreSQL/MongoDB load + transformation preview.
- Transformation: validated at preview only, not re-validated at apply (name already confirmed).

## Quick orientation for new tasks
- New HTTP endpoint: add a function to the relevant `api/routers/*.py` (or a new router + `app.include_router` in `main.py:77-88`), put logic in `api/services/`, secure with `Depends(verifyUser)` or stronger dep from `api/commons.py`.
- New LLM component: copy the pattern in `nubrix/components/codeGenerator.py` — load `prompts.yaml` and `config.ini` via `nubrix.utils`, get the model via `utils/llm.getGenaiLlm`, post-process with `cleanThinkTokens` if the model emits ` iNdEx` blocks. Or use `buildLlmChain(section, promptKey)` from `llmChainFactory.py`.
- New scheduled job: implement class in `nubrix/triggers/tasks/`, wire into `celery.py` (task decorator + beat entry), import at top of that file.
- New table creation entry point: call `managementService.validateTableNameAvailable(projectId, tableName)` before upload to prevent name collisions.
- Don't add: async auth deps, tenant concurrency caps, Celery queue splits, proxy clients, session caching, rollups, Docker cgroup tweaks. These were tried and reverted — see MEMORY.md.

## Resources
- `resources/EndpointSpec.md` — full 89-endpoint reference (auth, projects, data loading, reporting, dashboards, transformations, subscriptions, billing, webhooks, credits).
- `resources/highlevel-architecture.html` — system architecture diagram (FastAPI, Celery, LLM agents, Supabase, Redis).
- `resources/lowlevel-architecture.html` — detailed component interaction diagram (subprocess sandbox, data flow, cache layers).
