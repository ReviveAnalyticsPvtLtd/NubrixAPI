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
- `utils/` — `llm.py` (LLM factories + `_LLM_CACHE` + `cleanThinkTokens`), `logger.py` (loguru + Logtail + `logs/runLogs.log`), `codeExecutor.py` (subprocess sandbox), `sandboxRunner.py` (starts every launcher, serves its table reads), `sandbox_launcher.py` (subprocess entry for code execution), `transform_launcher.py` (subprocess entry for transformation validate), `inspect_launcher.py` (subprocess entry for transformation inspect), `exceptionHandler.py`, `langfuseClient.py`, `llmOutputParser.py`, `sizing.py` (server-aware auto-sizing).
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
- **Code execution** (`utils/codeExecutor.py` + `utils/sandbox_launcher.py`): LLM-generated code runs in a subprocess started by `runSandbox` in `utils/sandboxRunner.py` (`subprocess.Popen` with `close_fds=True`). No fork, no `RLIMIT_AS` (it crashes polars), and only the one inherited socket described below. Restricted builtins (`__import__` whitelisted). Timeout configurable via `CODE_EXEC_TIMEOUT_SECONDS` (default `0` = no timeout). Soft per-project Redis ZSET semaphore (default 50/project).
- **Transformation execution** (`nubrix/components/transformationExecutor.py` + `utils/transform_launcher.py` + `utils/inspect_launcher.py`): Same subprocess pattern. Child writes parquet to a file in its own folder, and the first 100 rows beside it as JSON, with cells clipped and rows dropped to fit `_MAX_PREVIEW_BYTES`, and stops a result too big to read back. None of that binds generated code, which runs in that process and can write both files itself and exit first, so the parent relies on none of it. Only `_run_transformation` asks `runSandbox` for those files. The parent never decodes a result, because a run can write a file that decodes to far more than the parent can hold, whatever its footer says: it refuses a file over `_MAX_RESULT_BYTES`, checks the parquet frame with `_check_parquet_shape`, which refuses a footer over `_MAX_FOOTER_BYTES` from its 4-byte length, and reads up to `_MAX_PREVIEW_BYTES` of preview as JSON. Keep a preview as rows: rows that each have their own keys make a frame as wide as it is long, which is why `_execute_code` builds nothing. Timeout via `TRANSFORMATION_TIMEOUT_SECONDS` (default `0` = no timeout).
- **Project scope**: all three launchers raise `PermissionError` when `fetch_data`, `fetch_data_pl` or `scan_data` names another project. This only gives generated code a clear error. The boundary is in the parent: a child sends a table name over a private socket, and the parent reads that table from the project it started the run for. Never add a project to that protocol. In `transform_launcher.py` and `inspect_launcher.py` the first argument stays named `projectId`, because generated code uses `projectId` and may pass it by keyword.
- **Child environment**: `_INHERITED_ENV` in `sandboxRunner.py` holds only path, locale and tuning variables, plus `SYSTEMROOT`, which sockets need on Windows. A child has no Supabase or Redis settings, and generated code can read everything it does have through `pd.io.common.os.environ`, so never add a secret.
- **Table reads**: `serveTables` runs on a thread while `_runChild` waits, and calls `spool_table` in `initMethods.py`, which reads Redis then Supabase and writes the table to a per-run folder. It only moves bytes, and the child decodes them: a stored table can be the output of generated code. A cold read stays parquet, streamed to disk, and a file up to `_REDIS_MAX_SPOOLED_BYTES` is cached in Redis as it stands. A Redis hit is written out as it stands, as `.parquet` or as an Arrow stream in `.arrow`. `TransformationExecutor.apply` deletes the table's Redis key, so never store a table there. A run stops spooling at `_MAX_SPOOL_BYTES`, and the last table can overshoot it. The parent ends serving with `shutdown` on its own end of the socket, never with anything sent on it, because a child can write to that socket.
- **Separate uid**: each run gets its own uid and group from a range, with no passwd entry, so a child can't read the parent's `/proc/<pid>/environ`, write to `/app`, read `/app/logs` (0700) or reach a run that is going on at the same time. A uid comes round again for other projects, so `_clearUid` runs `sandbox_reaper.py` before and after each run, and the run folder, which is the claim on the uid, stays unless everything the uid owned has gone: processes, files in `work/` and in any shared temp folder still open, and System V objects. The reaper starts as root and calls `setresuid(spare, run, spare)`, where the spare uid is the run's uid plus `_RUN_UID_COUNT`. Its effective uid is its whole reach, and its real and saved uids put it beyond the reach of anything the run left, which can't signal or trace it. Never let it exec, which would copy the effective uid over the saved one, never give it a real uid of 0, and keep a separate spare uid per run, because reapers sharing a real uid can signal each other. Don't run it by hand as your own uid: it ends every process of the uid it reaches, and it refuses unless started as root. Don't share one sandbox user: concurrent runs for different projects could then read each other's tables and memory. The Dockerfile sets `SANDBOX_RUN_ROOT`, which makes a separate uid mandatory. A root parent without it refuses to run, and so does a non-root parent with it. For local development set `SANDBOX_ALLOW_SAME_UID=1`, which runs children under the developer's own uid, without the limits or the reaper. `test/conftest.py` sets it for tests. The parent needs `CAP_SETUID`, `CAP_SETGID`, `CAP_CHOWN`, `CAP_DAC_OVERRIDE` and `CAP_KILL`.
- **Shared folders**: `_closeSharedFolders` sets `/tmp`, `/var/tmp`, `/dev/shm`, `/dev/mqueue` and `/run/lock` to 0711 whenever a run claims its uid, so `work/` is the only place a run can write. Every clear has to list those folders to find its uid's files, so one run filling one would make every other run's clear time out and lose its uid: a million entries in `/tmp` took 10 to 27 s of a clear's 30. Nothing that uses them here runs as a run uid, and a clear can't list a closed folder, so it costs nothing. A run has no `/dev/shm`, so no POSIX semaphores: joblib warns and runs serially, and a `multiprocessing` lock fails. If a folder can't be closed the run goes on with a warning in the log, and every clear goes back to paying for it. The reaper only enters folders its uid owns, so another uid's tree costs it one entry. What another run left in one of those folders goes when its owner is cleared: any file does, sticky bit or not, and an empty folder does. A folder that isn't empty can't be removed, so the owner's uid stays claimed. In `work/` the parent removes it as root and the next sweep frees the uid. In a shared folder that stayed open it lasts until the container is replaced.
- **Orphaned runs**: a run's worker holds a `flock` on the run folder, which the kernel drops if the worker dies. `sweepOrphanedRuns` runs on a thread when an API worker starts, and clears any folder it can lock: a killed worker leaves processes and copies of customer tables. A worker that dies between API worker starts waits for the next one.
- **Run limits**: every launcher calls `enterSandbox` before generated code runs. On a run uid that sets hard limits on processes and threads, file size, core dumps and open files, sets no-new-privileges, and kills the launcher if its worker dies. The image has no setuid or setgid files. stdout and stderr go to files in the run folder, so nothing a process holds open can block the parent, and it reads at most `_MAX_OUTPUT_BYTES` of each.
- **Run folder**: the child's cwd, `HOME`, `TMPDIR` and result file are in `work/`, which the child owns, so the parent reads the result through `readResult`, which refuses links, pipes and files over the cap. The parent writes tables to `tables/`, which the child can only read. `logger.py` writes `logs/` relative to the cwd, which is why the cwd can't stay `/app`.
- **Still open**: a child can reach any host, including the private network, but holds no credentials and only its own project's data. Its disk use is capped per file, not in total. Memory and CPU have no cap. A stored table is still decoded in the API by `fetch_data`, `dashboardService` and `reportGenerator`, so a table a run saved can reach that memory. Metadata generation after an apply parses the footer and decodes one batch for its sample row, so keep `iter_batches` there. A run's own tree of millions of files can time out its own clear, which keeps that uid claimed until a sweep gets through it.
- **Why subprocess not fork**: Fork inherits parent's open Redis/HTTP sockets → deadlock when child uses `fetch_data_pl`. `close_fds=True` in `Popen` prevents this.

## Data Layer (`utils/initMethods.py`)
- `fetch_data(projectId, table)` → `pd.DataFrame` (backward compat).
- `fetch_data_pl(projectId, table)` → `pl.DataFrame` (eager, zero-copy from Arrow cache).
- `scan_data(projectId, table)` → `pl.LazyFrame` (predicate/projection pushdown).
- Arrow LRU cache bounded by `DF_CACHE_MAX_BYTES` + `DF_CACHE_MAX_ENTRIES`.
- `classify_table_size(projectId, table)` → `{rows_estimate, columns, size_class, hint}`. Drives workflow auto-routing.
- `serializer` — converts Polars types for `json.dumps`.
- `invalidate_data_cache(projectId, table)` — call after transformations.
- Every AnalyticsHub read goes through `utils/projectStorage.py` with the service key, in the API and Celery. Sandboxes have no key, so `use_table_source` sends their reads to the parent. Its sync reads raise `StorageReadError` for a bad status or a bad Range answer, because `httpx.HTTPStatusError` can't be unpickled and breaks a `ProcessPoolExecutor`. Transport errors such as `httpx.ConnectError` stay httpx types, which do pickle. The async reads in `reportingService.py` and `transformationService.py` keep `raise_for_status`. Never add a read through a public URL: `test_storage_read_paths.py` fails on `FILE_URL` and on stray `urlopen` calls. `DOMAIN_FILE_URL` is a separate public bucket.

## Table Viewer Endpoint
- `GET /api/latest/projects/viewTable/{projectId}/{tableName}?page=1&pageSize=100`
- Uses `readTablePage` (`utils/projectStorage.py`): an authenticated HEAD for the size, then Range GETs for the parquet footer and the row groups holding the page, decoded batch by batch up to the end of the page.
- Keep the HEAD. On the local file backend a one byte Range GET took 500 ms on a 280 MB object, against 9 ms for HEAD, so a Range GET for the size is only the fallback when an edge drops `Content-Length`.
- Tables written with pandas are often one row group, so the whole group's bytes are fetched but must not be decoded whole. Keep `iter_batches` here, not `read_row_groups`.
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
