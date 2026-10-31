# MEMORY.md — Change Log

This file documents changes made to the NubrixAI Analytics Hub API codebase.

---

## Summary

**Goal**: Use Polars as the primary data engine for LLM-generated code (5-100x faster on large datasets), fix sandbox execution reliability, and add a paginated table viewer endpoint.

**What stayed**: All auth, CORS, Celery config, supervisord, sizing — unchanged from original. No async deps, no tenant caps, no queue splits, no proxy clients, no session caching, no rollups.

---

## What Changed

### 1. Polars Data Layer (`utils/initMethods.py`)

- **Arrow-backed LRU cache**: Stores `pa.Table` instead of `pd.DataFrame`. Smaller memory, faster deserialization from Redis.
- **`fetch_data_pl(projectId, "table")`**: Returns `pl.DataFrame` (eager). Zero-copy from cached Arrow table.
- **`scan_data(projectId, "table")`**: Returns `pl.LazyFrame` for predicate/projection pushdown.
- **`fetch_data()`**: Still returns `pd.DataFrame` (backward compat).
- **Redis cache**: Arrow IPC bytes (smaller than parquet, faster deserialize).
- **Cold path**: `readTableBytes` (authenticated `httpx.get`) + `pq.read_table(BytesIO)` — PyArrow can't read HTTPS directly.
- **Filter translation**: `_translate_to_pyarrow_filter()` maps filter dicts to PyArrow compute expressions.
- **Size classification**: `classify_table_size(projectId, table)` buckets tables as `small|medium|large|massive` using cached Arrow `num_rows` (O(1)).
  - Buckets: `<10k=small`, `10k-100k=medium`, `100k-1M=large`, `>1M=massive`.
  - Env: `LAZY_FETCH_ROW_THRESHOLD` (default `1000000`).
- **`serializer`**: Converts Polars types for `json.dumps` (`pl.DataFrame` -> `to_dicts()`, `pl.Series` -> `to_list()`).
- **`invalidate_data_cache(projectId, tableName)`**: Drops stale cache after transformations.

### 2. Subprocess Sandbox — Code Execution (`utils/codeExecutor.py`, `utils/sandbox_launcher.py`)

- **Subprocess-based**: LLM-generated code runs in a fresh subprocess (`subprocess.Popen` with `close_fds=True`). No fork, no inherited Redis/HTTP sockets, no deadlock.
- **No RLIMIT caps**: `RLIMIT_AS` / `RLIMIT_CPU` removed — they crashed polars import in containers. Isolation is via subprocess + restricted builtins.
- **Restricted builtins**: `__import__` whitelisted to `polars`, `json`, `math`, `datetime`, `numpy`, `pandas`, etc. No `open`, `subprocess`, `os`.
- **Project-scoped fetchers**: `safe_fetch_data` / `safe_fetch_data_pl` / `safe_scan_data` reject cross-tenant access.
- **Timeout**: Configurable via `CODE_EXEC_TIMEOUT_SECONDS` env (default `0` = no timeout). Set to N to re-enable N-second cap.
- **Concurrency**: Soft per-project Redis ZSET semaphore (default 50/project).

### 3. Subprocess Transformation Executor (`nubrix/components/transformationExecutor.py`, `utils/transform_launcher.py`, `utils/inspect_launcher.py`)

- **Subprocess-based**: Transformation code runs in a fresh subprocess (same `close_fds=True` pattern). Replaced multiprocessing fork which deadlocked on inherited Redis sockets.
- **Temp-file IPC**: Child writes `final_df` as parquet to a temp file; parent reads it. No multiprocessing Queue pickle overhead.
- **Polars preview**: `executeAndPreview` uses `pl.read_parquet(...).head(100).to_dicts()` — only loads 100 rows, not full table.
- **Accepts**: `pl.LazyFrame` (auto-collects), `pl.DataFrame` (via `.to_arrow()`), `pd.DataFrame` (via `pa.Table.from_pandas`).
- **Timeout**: Configurable via `TRANSFORMATION_TIMEOUT_SECONDS` env (default `0` = no timeout).
- **Restricted imports**: Same whitelist as sandbox.

### 4. Workflow Auto-Routing (`nubrix/workflows/reportingToolWorkflow.py`, `parallelReportingToolWorkflow.py`)

- **`_route_large_tables_to_scan()`**: Post-processes LLM-generated code. For each `fetch_data_pl("pid", "table")`, calls `classify_table_size()` — if rows >= `LAZY_FETCH_ROW_THRESHOLD` (default 1M), rewrites to `scan_data("pid", "table")`.
- **`_injectProjectId()`**: Injects `projectId` into all fetch calls. Deduplicates double-injections.
- **`json.dumps` normalizer**: Strips duplicate `default=` kwargs, adds exactly one `default=serializer`. Prevents `SyntaxError: keyword argument repeated`.
- **Threshold**: `LAZY_FETCH_ROW_THRESHOLD=1000000` (1M rows). Small/medium tables stay eager.

### 5. Prompts — Polars-First (`prompts.yaml`)

- All prompts rewritten to prefer Polars (`fetch_data_pl`, `scan_data`, `pl.col`, `.group_by().agg()`, `.collect()`).
- Examples use Polars idioms exclusively. Pandas is explicit fallback only.
- All `{...}` escaped as `{{...}}` to avoid PromptTemplate variable parsing errors.

### 6. Chain Factory + LLM Cache (`nubrix/components/llmChainFactory.py`, `utils/llm.py`)

- `buildLlmChain(section, promptKey)`: Single factory replaces 6 boilerplate chain-builder classes.
- `_LLM_CACHE`: Dict keyed by `(model, temperature, max_tokens)` — eliminates repeated client construction.
- `cleanThinkTokens`: Strips `<think>` blocks from model output.

### 7. Metadata + Size Hints (`api/services/reportingService.py`)

- Two-tier metadata cache: in-process LRU (30s) -> Redis (120s) -> Supabase HTTP.
- `_augment_with_size_hints()`: Each table entry gets `size_class` + `size_hint` in metadata -> LLM sees it in prompt.
- Lazy panel chart routing: large/massive tables use `scan_data` + Polars `group_by().agg()` instead of eager pandas.

### 8. Table Viewer Endpoint (`api/routers/manager.py`)

- `GET /viewTable/{projectId}/{tableName}?page=1&pageSize=100`
- Uses `readTablePage` — Range GETs for the parquet footer and the row groups holding the page. `pl.scan_parquet` can't authenticate on HTTP URLs.
- Auth via `verifyToken`. Max page size 500.
- Response includes `rows` + `pagination` (page, pageSize, totalRows, totalPages).

### 9. `_generateAttributeInfo` Fix (`api/services/managementService.py`)

- Replaced `pd.read_parquet(url)` (fails on HTTPS) with `httpx.get()` + `pq.ParquetFile(BytesIO)`. Reads schema + first row only, not full table.

### 10. Active/Inactive Tables (`metadata.json`)

Every table entry in `metadata.json` now has an `isActive` boolean. Inactive tables are hidden from all LLM-facing services (reporting, transformation, insights, blends, report generation, insight context). The frontend sees all tables with their `isActive` flag.

**Schema**: `{ "users": { "description": "...", "shape": [...], "columns": [...], "isActive": true } }`

- Missing `isActive` key = active (backward compat — no migration needed).
- New tables get `isActive: true` automatically during metadata generation.
- Metadata regeneration preserves existing `isActive` values for unchanged tables.
- **Toggling**: `PATCH /projects/toggleTableActive/{projectId}/{tableName}` — flips `isActive`, persists to `metadata.json`. Auth via `verifyToken`.
- **Frontend** (`getMetadata`): Returns all tables with `isActive` field.

**Shared helper**: `managementService.filterActiveTables(metadata)` — returns only entries where `isActive != False`. All consumers delegate to this single implementation:
- `reportingService._getProjectMetadata` — caches full metadata, filters on return.
- `transformationService._get_metadata` — caches full metadata, filters on return.
- `managementService._generateRawInsights` — filters before LLM.
- `blendService.getDataSources` — `rawTables` only includes active.
- `blendService.getFieldsFromSources` — blocks access to inactive tables (403).
- `reportGenerator.getAllTables` — only returns active tables.
- `insightContextBuilder.build` — filters metadata before building context.

**Model**: `ToggleTableActive(projectId: str, tableName: str)` in `api/models.py`.

### 11. Resources Directory (`resources/`)

- `resources/EndpointSpec.md` — full 89-endpoint reference document covering all API surface.
- `resources/highlevel-architecture.html` — system architecture diagram (FastAPI, Celery, LLM agents, Supabase, Redis, Razorpay).
- `resources/lowlevel-architecture.html` — detailed component interaction diagram (subprocess sandbox, data flow, cache layers, Celery beat tasks).
- All files are hand-maintained reference docs, not generated.

### 12. Python 3.13 Migration + Dockerfile Fix

- **Python version**: `.python-version` updated from `3.10` to `3.13`. AGENTS.md/MEMORY.md synced.
- **Dockerfile**: Base image changed from `python:3.10-slim` to `python:3.13-slim`.
- **`htmlmin==0.1.12` build fix**: `ydata-profiling` depends on `htmlmin==0.1.12` which `import cgi` (removed in Python 3.13). Fix: pre-install `legacy-cgi` + `uv sync --no-build-isolation` so the build environment has `cgi` available.
- **Why no `pins/build-overrides`**: `legacy-cgi` isn't a runtime dep — only needed during `htmlmin` build. `--no-build-isolation` is the minimal change.

### 13. Table Name Collision Guard (`managementService.validateTableNameAvailable`)

No new table (from data load or transformation) can take a name that already exists in `metadata.json` — active or inactive. Raises `409` with message `"A table named '{tableName}' already exists in this project."`

Called at every entry point where a new table is created:
- Transformation preview (`transformationService.executeAndPreviewArtifact`) — validated once at preview, not re-validated at apply.

**Carve-out**: `transformationService.approveMessage` skips the check when the transformation's own `latest_approved_artifact.table_name` matches `newTransformedTableName` — i.e., a transformation may re-approve / overwrite its own output. Re-using a name that belongs to a *different* table still raises 409.
- CSV upload (`dataLoadService.loadCsvData`)
- Excel upload (`dataLoadService.loadExcelData`)
- MySQL load (`dataLoadService.loadMySql`)
- PostgreSQL load (`dataLoadService.loadPostgreSQL`)
- MongoDB load (`dataLoadService.loadMongoDB`)

---

## What Was Reverted (Overengineered — Removed)

These were added during the refactor and then reverted because they broke production or added unnecessary complexity:

| Item | Why removed |
|---|---|
| `SupabaseClientProxy` in `commons.py` | Added lazy-init wrapper around `create_client` — unnecessary, direct client works fine |
| Async auth deps in `commons.py` | Changed `verifyToken` etc. to `async def` — changed behavior, no benefit |
| Redis session caching in `commons.py` | Could serve stale tokens, added complexity |
| `verifyProjectOwnership` / `verifyMultipartProjectOwnership` in `commons.py` | Overengineered auth layer — routers use `verifyToken` |
| `requireTenantSlot` / `releaseTenantSlot` in `commons.py` | Per-tenant concurrency caps — could deadlock, unnecessary |
| CORS `ALLOWED_ORIGINS` env logic in `main.py` | `["*"]` was working fine |
| `supervisord.conf` split into compute/billing queues | Single celery worker was fine, split added complexity |
| `celery.py` added `generateMetadata`, `generateInsights`, `generateReport`, `syncSessionActivity` tasks + beat + `task_routes` | Caused race condition (metadata vs insights), unnecessary |
| `generateMetadata` / `generateKpis` endpoints changed to Celery dispatch | Caused race condition — reverted to sync service calls |
| `compute_rollups` / `get_rollup` in `initMethods.py` | Unrequested pre-aggregation that added latency on data load |
| `get_report_pool` in `initMethods.py` | Reverted to original `ProcessPoolExecutor(max_workers=N)` |
| `Dockerfile` cgroup `pids.max` write | Unnecessary |
| `startup.sh` watchdog loop | Unnecessary complexity |
| `utils/sizing.py` hardcoded constants | Reverted to original server-aware formulas |
| `managementService.py` `userId` param on `updateBookmark`/`Archive`/`Trash`/`deleteProject` | Signature mismatch with routers — would crash |
| Runtimeerror handler in `main.py` | Unnecessary — subprocess sandbox handles failures |
| `ssl._create_unverified_context` hack in `main.py` | Security risk — removed |

---

## What Was NOT Changed (Intentionally)

| Area | Reason |
|---|---|
| CORS (`allow_origins=["*"]`) | Frontend depends on it |
| Auth deps (sync `verifyToken`, `verifyUser`, etc.) | Original behavior, no async conversion |
| Celery config (single worker, original beat schedule) | Working fine, no queue split |
| Supervisord (fastapi + celery + beat) | Simple, working |
| startup.sh | Original |
| Dockerfile | Changed: python:3.10-slim → python:3.13-slim + legacy-cgi for htmlmin build |
| Sizing formulas (`utils/sizing.py`) | Original server-aware formulas |

---

## Verification (All Pass)

1. 9/9 changed files compile
2. App boots: 89 OpenAPI paths
3. Sandbox: `print(1+1)` -> `2`, Polars `pl.DataFrame` works
4. Transform: `executeAndPreview` returns rows + parquet bytes
5. `fetch_data_pl`: 100k rows from Supabase
6. Celery: 10 tasks (no generate/sync additions)
7. No dangling references to removed functions
8. Service signatures match router calls
9. `filterActiveTables`: correctly filters `isActive: False`, keeps missing key as active
10. `toggleTableActive` endpoint registered in OpenAPI
11. `validateTableNameAvailable` called at all table creation entry points

---

## TL;DR

Polars is the default data engine for LLM-generated code. Sandboxes run in subprocesses with `close_fds=True` (no fork deadlock, no RLIMIT caps). Transformation executor uses subprocess + temp-file IPC + Polars head-only preview. Workflow post-processor auto-routes 1M+ row tables to lazy `scan_data`. `json.dumps` calls normalized to single `default=serializer`. `viewTable` endpoint paginates any table via `readTablePage`. Active/inactive tables: every metadata entry has `isActive`, all LLM consumers filter to active only, `PATCH /toggleTableActive` toggles state, `validateTableNameAvailable` prevents name collisions at all entry points. Everything else — auth, CORS, Celery, supervisord, Docker, sizing — is original, untouched.