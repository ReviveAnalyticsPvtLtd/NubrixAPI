"""
reportingService module provides services for generating charts and panel charts using reporting workflows and code templates.

This module defines the ReportingService class, which offers methods to generate single and panel charts for reporting purposes. It leverages workflows, code templates, and data blending to produce various chart types based on user input.
"""

__version__ = "1.0.0"
__author__ = "Rauhan Ahmed Siddiqui"
__all__ = ["reportingService"] 


from api.models import GenerateChartInput, PanelChartDetails, GenerateChartsInParallel, SaveQuery, DeleteQuery
from nubrix.components.llmChainFactory import buildLlmChain
from nubrix.workflows.reportingToolWorkflow import buildReportingWorkflow
from nubrix.workflows.parallelReportingToolWorkflow import buildParallelReportingWorkflow
from api.services.credits.creditTrackingCallback import CreditTrackingCallback
from api.commons import updateProjectModifiedAt
from utils.exceptionHandler import CustomException
from concurrent.futures import ThreadPoolExecutor
from utils.initMethods import fetch_data, scan_data
from utils.projectStorage import authHeaders, objectUrl
from nubrix.utils import readYaml
from utils.logger import logger
from api.commons import client
import httpx
import redis
import pandas as pd
import asyncio
import string
import orjson
import random
import json
import threading
import uuid
import time
import os
import io

_REPORTING_REDIS_POOL: redis.ConnectionPool | None = None


def _reporting_redis_pool() -> redis.ConnectionPool:
    global _REPORTING_REDIS_POOL
    if _REPORTING_REDIS_POOL is None:
        _REPORTING_REDIS_POOL = redis.ConnectionPool(
            host=os.environ["REDIS_HOST"],
            port=int(os.environ["REDIS_PORT"]),
            password=os.environ["REDIS_PASSWORD"],
            max_connections=16,
            decode_responses=False,
            socket_connect_timeout=5,
            socket_timeout=10,
        )
    return _REPORTING_REDIS_POOL


threadLocal = threading.local()

def initWorkflow():
    logger.disable("")
    try:
        threadLocal.workflow = buildReportingWorkflow()
    finally:
        logger.enable("")

def initParallelWorkflow():
    logger.disable("")
    try:
        threadLocal.parallelWorkflow = buildParallelReportingWorkflow()
    finally:
        logger.enable("")

class ReportingService:
    """
    Service class for generating charts and panel charts for reporting purposes.

    This class provides methods to generate single charts and panel (multi-chart) visualizations. It supports data blending, aggregation, and uses code templates to generate code snippets for reproducibility.
    """
    def __init__(self) -> None:
        """
        Initializes the ReportingService.

        Loads code templates from YAML, sets up the reporting workflow, and initializes the storage client.
        """
        logger.info("Initializing Reporting Service.")
        self.codeTemplates = readYaml(os.path.join(os.getcwd(), "codeTemplates.yaml"))
        self.reportingToolWorkflow = buildReportingWorkflow()
        self.client = client
        self._httpClient = httpx.AsyncClient(timeout=20)
        self._metadataCache: dict[str, tuple[float, dict]] = {}
        self._metadataCacheTtl = 30.0
        self._redis = redis.Redis(connection_pool=_reporting_redis_pool())

    async def _fetchJson(self, url: str) -> dict | list:
        """Async fetch with a short in-memory cache to avoid re-fetching metadata within a session."""
        response = await self._httpClient.get(url, headers=authHeaders())
        response.raise_for_status()
        return response.json()

    def _projectMetadataUrl(self, projectId: str, fileName: str = "metadata.json") -> str:
        """Build the authenticated URL for a project file."""
        return objectUrl(projectId, fileName)

    @staticmethod
    def _augment_with_size_hints(metadata: dict, projectId: str) -> dict:
        """Annotate each table entry in metadata with a deterministic size_class.

        The metadata shape is ``{<tableName>: {"shape": [rows, cols], "columns": [...]}}``.
        The hint steers the LLM away from guessing memory cost and toward lazy
        Polars for large tables.
        """
        from utils.initMethods import classify_table_size
        size_by_table = (metadata or {}).get(...) if isinstance(metadata, dict) else None
        if size_by_table is None:
            size_by_table = {}
        for table_name, entry in (metadata or {}).items():
            if not isinstance(entry, dict) or table_name.startswith("_"):
                continue
            cls = classify_table_size(projectId, table_name)
            entry["size_class"] = cls.get("size_class", "unknown")
            entry["size_hint"] = cls.get("hint", "")
            existing_rows = cls.get("rows_estimate")
            if existing_rows is not None and "shape" in entry and isinstance(entry["shape"], list):
                entry["_size_check"] = (existing_rows == entry["shape"][0])
            # Pre-warm the in-process Arrow cache so the post-processor can route
            # by row count synchronously without an extra parquet read.
        return metadata

    async def _getProjectMetadata(self, projectId: str) -> dict:
        """Fetch project metadata with a two-layer cache + per-table size hint annotation.
        Returns only active tables (isActive != False)."""
        cached = self._metadataCache.get(projectId)
        if cached and (time.time() - cached[0]) < self._metadataCacheTtl:
            return self._filterActive(cached[1])
        redis_key = f"{projectId}::metadata"
        try:
            hit = self._redis.get(redis_key)
            if hit is not None:
                metadata = orjson.loads(hit)
                self._augment_with_size_hints(metadata, projectId)
                self._metadataCache[projectId] = (time.time(), metadata)
                return self._filterActive(metadata)
        except Exception as e:
            logger.warning(f"Metadata Redis cache read failed for {projectId}: {e}")
        url = self._projectMetadataUrl(projectId, "metadata.json")
        metadata = await self._fetchJson(url)
        self._augment_with_size_hints(metadata, projectId)
        self._metadataCache[projectId] = (time.time(), metadata)
        try:
            self._redis.set(redis_key, orjson.dumps(metadata), ex=120)
        except Exception as e:
            logger.warning(f"Metadata Redis cache write failed for {projectId}: {e}")
        return self._filterActive(metadata)

    @staticmethod
    def _filterActive(metadata: dict) -> dict:
        """Return only active table entries. Delegates to managementService."""
        from api.services.managementService import managementService
        return managementService.filterActiveTables(metadata)

    @staticmethod
    def _generatePanelChart(projectId: str, chartType: str, xAxis: str, yAxis: str, aggregationMetric: str | None, dataSourceName: str, tablesUsed: list[str] | str, joinTypes: list[str] | None = None, blendOn: list[str] | None = None, **kwargs) -> dict:
        """
        Prepares and aggregates data for charting based on the specified parameters.

        Args:
            projectId (str): The project ID.
            chartType (str): The type of chart to generate (e.g., bar, line, pie, table, pivot, geoMap, etc.).
            xAxis (str): The column to use for the X axis.
            yAxis (str): The column to use for the Y axis.
            aggregationMetric (str, optional): The aggregation metric (sum, mean, etc.).
            dataSourceName (str): The name of the data source.
            tablesUsed (list[str] | str): Tables to use for the chart. Can be a single table or a list for blending.
            joinTypes (list[str], optional): Join types for merging tables (if blending).
            blendOn (list[str], optional): Columns to join on (if blending).
            **kwargs: Additional keyword arguments for few charts (index, columns, values, selectedColumns, mapType, isFilterApplied, filters).

        Returns:
            dict: Chart-ready data structure suitable for frontend rendering.
        """
        filters = kwargs.get("filters")
        hasFilters = filters and len(filters) > 0
        
        # Route to Polars lazy for large/medium tables to avoid loading
        # the full dataset into pandas. Small tables use eager pandas.
        from utils.initMethods import classify_table_size
        _AGG_POLARS = {
            "sum": "sum", "mean": "mean", "median": "median",
            "max": "max", "min": "min", "count": "count",
            "std": "std", "var": "var",
        }

        def _use_lazy(table_name):
            if not table_name or isinstance(table_name, list):
                return False
            try:
                info = classify_table_size(projectId, table_name)
                return info.get("size_class") in ("large", "massive")
            except Exception:
                return False

        def _polars_agg(lf, x, y, agg):
            import polars as pl
            expr = getattr(pl.col(y), agg)()
            return lf.group_by(x).agg(expr.alias(y)).collect()

        single_table = tablesUsed if not isinstance(tablesUsed, list) else None
        use_lazy = _use_lazy(single_table) and not hasFilters and chartType != "pivot"

        if use_lazy:
            import polars as pl
            lf = scan_data(projectId, single_table)
            if aggregationMetric in _AGG_POLARS:
                finalResult = _polars_agg(lf, xAxis, yAxis, _AGG_POLARS[aggregationMetric])
            else:
                finalResult = lf.select(pl.col(xAxis), pl.col(yAxis)).collect()
            # Build response from Polars DataFrame
            if chartType in ["bar", "line", "radar", "polarArea"]:
                return {
                    "chartType": chartType,
                    "title": f"{chartType.capitalize()} Chart of {xAxis} vs {yAxis}",
                    "xLabels": xAxis, "yLabels": yAxis,
                    "data": {
                        "labels": finalResult[xAxis].to_list(),
                        "datasets": [{"label": f"{aggregationMetric} of {yAxis}", "data": finalResult[yAxis].to_list()}],
                    },
                }
            elif chartType in ["pie", "doughnut"]:
                return {
                    "chartType": chartType,
                    "title": f"{chartType.capitalize()} Chart of {xAxis} vs {yAxis}",
                    "data": {
                        "labels": finalResult[xAxis].to_list(),
                        "datasets": [{"label": f"{aggregationMetric} of {yAxis}", "data": finalResult[yAxis].to_list()}],
                    },
                }
            elif chartType == "scatter":
                return {
                    "chartType": chartType,
                    "title": f"{chartType.capitalize()} Chart of {xAxis} vs {yAxis}",
                    "xLabels": xAxis, "yLabels": yAxis,
                    "data": {
                        "datasets": [{"label": f"{aggregationMetric} of {yAxis}",
                            "data": [{"x": r[xAxis], "y": r[yAxis]} for r in finalResult.to_dicts()]}],
                    },
                }
            elif chartType == "card":
                val = finalResult[yAxis].to_list()[0] if len(finalResult) > 0 else 0
                return {
                    "chartType": "card",
                    "title": f"Card Chart of {xAxis} vs {yAxis}",
                    "label": f"{aggregationMetric} of {yAxis}",
                    "data": val,
                }
            elif chartType == "table":
                cols = kwargs.get("selectedColumns") or finalResult.columns
                tableData = finalResult.select(cols).to_dicts()
                return {
                    "chartType": "table",
                    "title": f"{dataSourceName} Data",
                    "columns": list(cols) if not isinstance(cols, list) else cols,
                    "data": tableData,
                }
            else:
                # Fall through to pandas path for unsupported chart types
                pass

        # --- Eager pandas path (small tables, blends, filters, pivot) ---
        if isinstance(tablesUsed, list):
            allTables = [fetch_data(projectId, x) for x in tablesUsed]
            result = allTables[0]
            for i in range(len(joinTypes)):
                result = pd.merge(left = result, right = allTables[i+1], on = blendOn[i], how = joinTypes[i], suffixes = ['_left', '_right'])
        else:
            result = fetch_data(projectId, tablesUsed)

        if hasFilters:
            for filter_item in filters:
                for column_path, condition in filter_item.items():
                    column = column_path.split(".")[-1]
                    if column not in result.columns:
                        continue
                    if isinstance(condition, dict):
                        if result[column].dtype == "object":
                            if "contains" in condition:
                                result = result[result[column].str.contains(condition["contains"], case=False, na=False)]
                            if "startswith" in condition:
                                result = result[result[column].str.startswith(condition["startswith"], na=False)]
                            if "endswith" in condition:
                                result = result[result[column].str.endswith(condition["endswith"], na=False)]
                        else:
                            if "min" in condition:
                                result = result[result[column] >= condition["min"]]
                            if "max" in condition:
                                result = result[result[column] <= condition["max"]]
                    elif isinstance(condition, (list, tuple, set)):
                        result = result[result[column].isin(condition)]
                    else:
                        result = result[result[column] == condition]
        if chartType != "pivot":
            if aggregationMetric == "sum":
                finalResult = result.groupby(xAxis)[yAxis].sum().reset_index()
            elif aggregationMetric == "mean":
                finalResult = result.groupby(xAxis)[yAxis].mean().reset_index()
            elif aggregationMetric == "median":
                finalResult = result.groupby(xAxis)[yAxis].median().reset_index()
            elif aggregationMetric == "max":
                finalResult = result.groupby(xAxis)[yAxis].max().reset_index()
            elif aggregationMetric == "min":
                finalResult = result.groupby(xAxis)[yAxis].min().reset_index()
            elif aggregationMetric == "count":
                finalResult = result.groupby(xAxis)[yAxis].count().reset_index()
            elif aggregationMetric == "std":
                finalResult = result.groupby(xAxis)[yAxis].std().reset_index()
            elif aggregationMetric == "var":
                finalResult = result.groupby(xAxis)[yAxis].var().reset_index()
            else:
                finalResult = result
        else:
            finalResult = result
        if chartType in ["bar", "line", "radar", "polarArea"]:
            response = {
                "chartType": chartType,
                "title": f"{chartType.capitalize()} Chart of {xAxis} vs {yAxis}",
                "xLabels": xAxis,
                "yLabels": yAxis,
                "data": {
                    "labels": finalResult[xAxis].tolist(),
                    "datasets": [
                        {
                            "label": f"{aggregationMetric} of {yAxis}",
                            "data": finalResult[yAxis].tolist()
                        }
                    ]
                }
            }
        elif chartType in ["pie", "doughnut"]:
            response = {
                "chartType": chartType,
                "title": f"{chartType.capitalize()} Chart of {xAxis} vs {yAxis}",
                "data": {
                    "labels": finalResult[xAxis].tolist(),
                    "datasets": [
                        {
                            "label": f"{aggregationMetric} of {yAxis}",
                            "data": finalResult[yAxis].tolist()
                        }
                    ]
                }
            }
        elif chartType == "scatter":
            response = {
                "chartType": chartType,
                "title": f"{chartType.capitalize()} Chart of {xAxis} vs {yAxis}",
                "xLabels": xAxis,
                "yLabels": yAxis,
                "data": {
                    "datasets": [
                        {
                            "label": f"{aggregationMetric} of {yAxis}",
                            "data": [
                                {"x": row[xAxis], "y": row[yAxis]} for _, row in finalResult.iterrows()
                            ]
                        }
                    ]
                }
            }
        elif chartType == "card":
            if len(finalResult) > 0:
                single_value = finalResult[yAxis].iloc[0]
                response = {
                    "chartType": "card",
                    "title": f"{chartType.capitalize()} Chart of {xAxis} vs {yAxis}",
                    "label": f"{aggregationMetric} of {yAxis}",
                    "data": single_value
                }
            else:
                response = {
                    "chartType": "card",
                    "title": f"{chartType.capitalize()} Chart of {xAxis} vs {yAxis}",
                    "label": f"{aggregationMetric} of {yAxis}",
                    "data": 0
                }
        elif chartType == "table":
            if kwargs.get("selectedColumns") is None:
                selectedColumns = finalResult.columns.tolist()
            else:
                selectedColumns = kwargs.get("selectedColumns")
            response = {
                "chartType": "table",
                "title": f"{dataSourceName} Data",
                "columns": selectedColumns,
                "data": finalResult[selectedColumns].to_dict(orient="records")
            }
        elif chartType == "pivot":
            pivotData = pd.pivot_table(finalResult, index=kwargs.get("index"), columns=kwargs.get("columns"), aggfunc=aggregationMetric, values=kwargs.get("values")).to_json()
            response = {
                "chartType": "pivot",
                "title": f"Pivot for {dataSourceName}",
                "data": orjson.loads(pivotData)
            }
        elif chartType == "geoMap":
            geoCodeCol = kwargs.get("geoCodeColumn")
            points = []
            hasGeoCode = geoCodeCol and geoCodeCol not in ["None", ""]

            geocode_cache = {}

            if hasGeoCode:
                from geopy.geocoders import Nominatim
                from geopy.extra.rate_limiter import RateLimiter
                import math
                geolocator = Nominatim(user_agent="NubrixAI", timeout=10)
                rate_limited_geocode = RateLimiter(geolocator.geocode, min_delay_seconds=1.0)

            def geocode_with_cache(value, geocode_func):
                if value in geocode_cache:
                    return geocode_cache[value]
                try:
                    res = geocode_func(value)
                    geocode_cache[value] = res
                except Exception:
                    geocode_cache[value] = None
                return geocode_cache[value]

            for _, row in finalResult.iterrows():
                lat = None
                lon = None

                if xAxis in finalResult.columns and yAxis in finalResult.columns:
                    latEx = row[xAxis]
                    lonEx = row[yAxis]
                    if pd.notna(latEx) and pd.notna(lonEx):
                        lat = latEx
                        lon = lonEx

                if (lat is None or lon is None) and hasGeoCode:
                    g = str(row.get(geoCodeCol, ""))
                    if g and g != "nan":
                        res = geocode_with_cache(g, rate_limited_geocode)
                        if res:
                            latC = res.latitude
                            lonC = res.longitude
                            if pd.notna(latC) and pd.notna(lonC):
                                lat = latC
                                lon = lonC

                if lat is not None and lon is not None:
                    points.append({
                        "id": "".join(random.choice(string.ascii_letters + string.digits) for i in range(16)),
                        "lat": lat,
                        "long": lon
                    })

            response = {
                "chartType": "geoMap",
                "map": {
                    "mapType": "scatterMap",
                    "data": {
                        "points": points
                    }
                }
            }
        return response
    
    async def generateChart(self, chartDetails: GenerateChartInput, userId: str | None = None) -> dict:
        """
        Generates a chart based on the provided chart details using the reporting workflow.

        The LangGraph workflow is sync, so it runs in a thread executor to avoid blocking
        the FastAPI event loop. Metadata is fetched asynchronously via httpx with a cache.
        """
        try:
            from utils.llm import getLangfuseConfig
            reportConfig = getLangfuseConfig(
                trace_name="ReportingWorkflow", projectId=chartDetails.projectId, userId=userId,
                tags=["reporting", "generateChart"],
            )
            if userId:
                reportConfig.setdefault("callbacks", []).append(
                    CreditTrackingCallback(userId=userId, operationType="reporting_query")
                )

            metadata = await self._getProjectMetadata(chartDetails.projectId)
            loop = asyncio.get_running_loop()
            response = await loop.run_in_executor(
                None,
                lambda: self.reportingToolWorkflow.invoke(
                    {
                        "metadata": metadata,
                        "inputQuery": chartDetails.inputQuery,
                        "projectId": chartDetails.projectId,
                    },
                    config=reportConfig or None,
                ),
            )
            updateProjectModifiedAt(chartDetails.projectId)
            return response
        except Exception as e:
            exception = CustomException(e)
            logger.error(exception)
            raise exception

    @staticmethod
    def _generateSingleChartForParallel(metadata: dict, projectId: str, query: str, userId: str | None = None) -> dict:
        """
        Helper function to generate a single chart in parallel.

        Args:
            metadata: The metadata for the project.
            projectId: The project ID.
            query: The input query for the chart.
            userId: The user ID for credit/observability tracking.

        Returns:
            dict: The generated chart data.
        """
        try:
            from utils.llm import getLangfuseConfig
            workflow = threadLocal.parallelWorkflow
            reportConfig = getLangfuseConfig(
                trace_name="ParallelReportingWorkflow", projectId=projectId, userId=userId,
                tags=["reporting", "parallelChart"],
            )
            if userId:
                reportConfig.setdefault("callbacks", []).append(
                    CreditTrackingCallback(userId=userId, operationType="reporting_query")
                )
            response = workflow.invoke({
                "metadata": metadata,
                "inputQuery": query,
                "projectId": projectId
            }, config=reportConfig or None)
            _ = response.pop("metadata", None)
            _ = response.pop("rephrasedQuery", None)
            _ = response.pop("codeOutput", None)
            return response
        except Exception as e:
            logger.error(f"Failed to generate parallel chart for query '{query}': {e}")
            return {
                "finalOutput": {
                    "chartType": "card",
                    "title": f"Chart: {query}",
                    "label": "Status",
                    "xLabels": "Status",
                    "yLabels": "Value",
                    "data": "Error/No data"
                },
                "generatedCode": f"# Failed to generate code for: {query}\n# Error: {e}"
            }

    async def generateChartsInParallel(self, details: GenerateChartsInParallel, userId: str | None = None) -> dict:
        """
        Generates multiple charts in parallel and exports them to an automatic dashboard page.

        Concurrency model:
          - Worker count scales with the box (default = cpu_count, capped at 16 so very
            large query batches don't oversubscribe the LLM API rate limit).
          - The warm code-exec process pool (size = cpu_count/2) is the CPU bottleneck;
            the thread pool here covers the I/O-bound LLM steps, so a higher thread count
            than process workers is intentional and safe.
        """
        try:
            metadata = await self._getProjectMetadata(details.projectId)
            insightsUrl = self._projectMetadataUrl(details.projectId, "insights.json")
            try:
                insights = await self._fetchJson(insightsUrl)
            except Exception as e:
                logger.warning(f"Could not fetch insights.json for project {details.projectId}: {e}")
                insights = {"insights": []}

            # Remove any potential duplicate queries while preserving order
            uniqueQueries = list(dict.fromkeys(details.inputQueries))

            loop = asyncio.get_running_loop()
            # Scale workers with the box; cap to avoid LLM rate-limit thrash on huge batches.
            from utils.sizing import parallel_chart_workers
            max_workers = parallel_chart_workers()
            with ThreadPoolExecutor(max_workers=max_workers, initializer=initParallelWorkflow) as executor:
                futures = [
                    executor.submit(self._generateSingleChartForParallel, metadata, details.projectId, query, userId)
                    for query in uniqueQueries
                ]
                responses = [f.result() for f in futures]

            # Check if there is already an existing dashboard configuration
            file_exists = "dashboardConfig.json" in [x.get("name") for x in self.client.storage.from_("AnalyticsHub").list(path = details.projectId)]
            dashboardConfig = {}
            if file_exists:
                dashboardConfig = await self._fetchJson(self._projectMetadataUrl(details.projectId, "dashboardConfig.json"))

            # Find the existing automatic page
            pageId = None
            for pid, pdata in dashboardConfig.items():
                if pdata.get("isAutomatic"):
                    pageId = pid
                    break

            # Fallback for old automatic dashboard which didn't have the isAutomatic flag
            if not pageId and dashboardConfig:
                pageId = list(dashboardConfig.keys())[0]

            if not pageId:
                # Generate a dynamic dashboard name only if creating a new one
                from utils.llm import getLangfuseConfig
                dashboardNameChain = buildLlmChain("DASHBOARDNAMEGENERATOR", "dashboardNameGeneratorPrompt", fallbackTokens=64)
                nameConfig = getLangfuseConfig(
                    trace_name="DashboardNameGenerator", projectId=details.projectId, userId=userId,
                    tags=["reporting", "dashboardNaming"],
                )
                if userId:
                    nameConfig.setdefault("callbacks", []).append(
                        CreditTrackingCallback(userId=userId, operationType="dashboard_naming")
                    )
                dashboardName = await loop.run_in_executor(
                    None,
                    lambda: dashboardNameChain.invoke(
                        {
                            "queries": "\n".join(uniqueQueries),
                            "metadata": json.dumps(metadata)
                        },
                        config=nameConfig or None,
                    ).strip(),
                )

                # Create a new dashboard page
                pageId = str(uuid.uuid4())
                dashboardConfig[pageId] = {"name": dashboardName, "isAutomatic": True, "widgets": []}

            # Export to dashboard
            pageDict = dashboardConfig.get(pageId)
            
            existingWidgets = pageDict.get("widgets", [])
            newWidgets = []
            
            for widget, originalQuery in zip(responses, uniqueQueries):
                widgetId = str(uuid.uuid4())
                chartType = widget.get("finalOutput", {}).get("chartType")
                data = widget.get("finalOutput", {}).get("data")
                if chartType == "card":
                    if isinstance(data, (int, float)):
                        data = float(f"{data:.2f}")
                    newWidgets.append({
                        "id": widgetId,
                        "chartType": chartType,
                        "title": widget.get("finalOutput", {}).get("title"),
                        "label": widget.get("finalOutput", {}).get("label"),
                        "xLabels": widget.get("finalOutput", {}).get("xLabels"),
                        "yLabels": widget.get("finalOutput", {}).get("yLabels"),
                        "data": data,
                        "query": originalQuery,
                        "layout": {"x": 0, "y": 0, "w": 4, "h": 6},
                        "generatedCode": widget.get("generatedCode")
                    })
                else:
                    newWidgets.append({
                        "id": widgetId,
                        "chartType": chartType,
                        "title": widget.get("finalOutput", {}).get("title"),
                        "label": widget.get("finalOutput", {}).get("label"),
                        "xLabels": widget.get("finalOutput", {}).get("xLabels"),
                        "yLabels": widget.get("finalOutput", {}).get("yLabels"),
                        "data": data,
                        "query": originalQuery,
                        "layout": {"x": 0, "y": 0, "w": 6, "h": 10},
                        "generatedCode": widget.get("generatedCode")
                    })
            
            allWidgets = existingWidgets + newWidgets
            cards = [w for w in allWidgets if w.get("chartType") == "card"]
            otherWidgets = [w for w in allWidgets if w.get("chartType") != "card"]
            
            current_y = 0
            current_x = 0
            
            for card in cards:
                card["layout"] = {
                    "x": current_x,
                    "y": current_y,
                    "w": 4,
                    "h": 6
                }
                current_x += 4
                if current_x >= 12:
                    current_x = 0
                    current_y += 6
            
            if current_x > 0:
                current_y += 6
                current_x = 0
                
            for widget in otherWidgets:
                widget["layout"] = {
                    "x": current_x,
                    "y": current_y,
                    "w": 6,
                    "h": 10
                }
                current_x += 6
                if current_x >= 12:
                    current_x = 0
                    current_y += 10
                    
            pageDict["widgets"] = cards + otherWidgets
            dashboardConfig[pageId] = pageDict
            with io.BytesIO() as buffer:
                buffer.write(json.dumps(dashboardConfig, indent=4).encode("utf-8"))
                buffer.seek(0)
                _ = self.client.storage.from_("AnalyticsHub").upload(path = f"{details.projectId}/dashboardConfig.json", file = buffer.getvalue(), file_options = {"upsert": "true"}) 
            
            # Updating insights.json
            for insight in insights.get("insights"):
                if insight.get("query") in uniqueQueries:
                    insight["isCharted"] = True
                else:
                    continue
            with io.BytesIO() as buffer:
                buffer.write(json.dumps(insights, indent=4).encode("utf-8"))
                buffer.seek(0)
                self.client.storage.from_("AnalyticsHub").upload(path = f"{details.projectId}/insights.json", file = buffer.getvalue(), file_options = {"upsert": "true"})  
            updateProjectModifiedAt(details.projectId)
            return dashboardConfig.get(pageId) 
        except Exception as e:
            exception = CustomException(e)
            logger.error(exception)
            raise exception

    async def generatePanelChart(self, panelChartDetails: PanelChartDetails) -> dict:
        """
        Generates a panel chart, optionally using blend configuration if available.

        This method checks for the presence of a data source or blend configuration, prepares the data, and generates the panel chart. It also generates the code used for chart creation using code templates.
        """
        try:
            allFiles = [x.get("name") for x in self.client.storage.from_("AnalyticsHub").list(path = panelChartDetails.projectId)]
            if "".join([panelChartDetails.dataSource, ".parquet"]) in allFiles:
                response = await asyncio.to_thread(
                    self._generatePanelChart,
                    projectId=panelChartDetails.projectId,
                    chartType=panelChartDetails.chartType,
                    xAxis=panelChartDetails.xAxis,
                    yAxis=panelChartDetails.yAxis,
                    aggregationMetric=panelChartDetails.aggregationMetric,
                    dataSourceName=panelChartDetails.dataSource,
                    tablesUsed=panelChartDetails.dataSource,
                    index=panelChartDetails.index,
                    columns=panelChartDetails.columns,
                    values=panelChartDetails.values,
                    selectedColumns=panelChartDetails.selectedColumns,
                    mapType=panelChartDetails.mapType,
                    isFilterApplied=panelChartDetails.isFilterApplied,
                    filters=panelChartDetails.filters,
                    geoCodeColumn=panelChartDetails.zipCodeColumn
                )
                generatedCodeTemplate = string.Template(self.codeTemplates.get("panelChartWithoutBlend"))
                generatedCode = generatedCodeTemplate.substitute(
                    projectId = panelChartDetails.projectId,
                    chartType = panelChartDetails.chartType,
                    xAxis = panelChartDetails.xAxis,
                    yAxis = panelChartDetails.yAxis,
                    aggregationMetric = panelChartDetails.aggregationMetric,
                    dataSourceName = panelChartDetails.dataSource,
                    tablesUsed = panelChartDetails.dataSource,
                    index=panelChartDetails.index,
                    columns=panelChartDetails.columns,
                    values=panelChartDetails.values,
                    selectedColumns=panelChartDetails.selectedColumns,
                    mapType=panelChartDetails.mapType,
                    isFilterApplied=panelChartDetails.isFilterApplied,
                    filters=panelChartDetails.filters,
                    geoCodeColumn='"{}"'.format(panelChartDetails.zipCodeColumn) if panelChartDetails.zipCodeColumn else "None"
                )
            elif "blendConfig.json" in allFiles:
                blendConfig = await self._fetchJson(self._projectMetadataUrl(panelChartDetails.projectId, "blendConfig.json"))
                tablesUsed = blendConfig[panelChartDetails.dataSource].get("tables")
                joinTypes = blendConfig[panelChartDetails.dataSource].get("joinTypes")
                blendOn = blendConfig[panelChartDetails.dataSource].get("blendOn")
                response = await asyncio.to_thread(
                    self._generatePanelChart,
                    projectId=panelChartDetails.projectId,
                    chartType=panelChartDetails.chartType,
                    xAxis=panelChartDetails.xAxis,
                    yAxis=panelChartDetails.yAxis,
                    aggregationMetric=panelChartDetails.aggregationMetric,
                    dataSourceName=panelChartDetails.dataSource,
                    tablesUsed=tablesUsed,
                    joinTypes=joinTypes,
                    blendOn=blendOn,
                    index=panelChartDetails.index,
                    columns=panelChartDetails.columns,
                    values=panelChartDetails.values,
                    selectedColumns=panelChartDetails.selectedColumns,
                    mapType=panelChartDetails.mapType,
                    isFilterApplied=panelChartDetails.isFilterApplied,
                    filters=panelChartDetails.filters,
                    geoCodeColumn=panelChartDetails.zipCodeColumn
                )
                generatedCodeTemplate = string.Template(self.codeTemplates.get("panelChartWithBlend"))
                generatedCode = generatedCodeTemplate.substitute(
                    projectId = panelChartDetails.projectId,
                    chartType = panelChartDetails.chartType,
                    xAxis = panelChartDetails.xAxis,
                    yAxis = panelChartDetails.yAxis,
                    aggregationMetric = panelChartDetails.aggregationMetric,
                    dataSourceName = panelChartDetails.dataSource,
                    tablesUsed = tablesUsed,
                    joinTypes = joinTypes,
                    blendOn = blendOn,
                    index=panelChartDetails.index,
                    columns=panelChartDetails.columns,
                    values=panelChartDetails.values,
                    selectedColumns=panelChartDetails.selectedColumns,
                    mapType=panelChartDetails.mapType,
                    isFilterApplied=panelChartDetails.isFilterApplied,
                    filters=panelChartDetails.filters,
                    geoCodeColumn='"{}"'.format(panelChartDetails.zipCodeColumn) if panelChartDetails.zipCodeColumn else "None"
                )
            else:
                raise CustomException(ValueError("No data source or blend configuration found for this project."), statusCode=400, uiMessage="No data source or blend configuration found.")
            response.update({"generatedCode": generatedCode})
            updateProjectModifiedAt(panelChartDetails.projectId)
            return response
        except CustomException:
            raise
        except Exception as e:
            exception = CustomException(e)
            logger.error(exception)
            raise exception

    async def saveQuery(self, details: SaveQuery) -> str:
        """
        Save a user-marked favourite query to a queryConfig.json file in the project's Supabase storage folder.
        """
        try:
            queryId = str(uuid.uuid4())
            allFiles = [x.get("name") for x in self.client.storage.from_("AnalyticsHub").list(path = details.projectId)]
            if "queryConfig.json" in allFiles:
                queryConfig = await self._fetchJson(self._projectMetadataUrl(details.projectId, "queryConfig.json"))
                if not isinstance(queryConfig, dict):
                    queryConfig = {}
                queryConfig[queryId] = details.query
            else:
                queryConfig = {queryId: details.query}
            with io.BytesIO() as buffer:
                buffer.write(json.dumps(queryConfig, indent=4).encode("utf-8"))
                buffer.seek(0)
                _ = self.client.storage.from_("AnalyticsHub").upload(path = f"{details.projectId}/queryConfig.json", file = buffer.getvalue(), file_options = {"upsert": "true"})
            updateProjectModifiedAt(details.projectId)
            return queryId
        except Exception as e:
            exception = CustomException(e)
            logger.error(exception)
            raise exception

    async def getQueries(self, projectId: str) -> dict:
        """
        Retrieve all saved favourite queries for a project from the queryConfig.json file.
        """
        try:
            if "queryConfig.json" in [x.get("name") for x in self.client.storage.from_("AnalyticsHub").list(path = projectId)]:
                queryConfig = await self._fetchJson(self._projectMetadataUrl(projectId, "queryConfig.json"))
                if not isinstance(queryConfig, dict):
                    return {}
                return queryConfig
            return {}
        except Exception as e:
            exception = CustomException(e)
            logger.error(exception)
            raise exception

    async def deleteQuery(self, details: DeleteQuery) -> None:
        """
        Delete a saved favourite query from the queryConfig.json file by its query ID.
        """
        try:
            queryConfig = await self._fetchJson(self._projectMetadataUrl(details.projectId, "queryConfig.json"))
            if not isinstance(queryConfig, dict):
                queryConfig = {}
            queryConfig.pop(details.queryId, None)
            with io.BytesIO() as buffer:
                buffer.write(json.dumps(queryConfig, indent=4).encode("utf-8"))
                buffer.seek(0)
                _ = self.client.storage.from_("AnalyticsHub").upload(path = f"{details.projectId}/queryConfig.json", file = buffer.getvalue(), file_options = {"upsert": "true"})
            updateProjectModifiedAt(details.projectId)
        except Exception as e:
            exception = CustomException(e)
            logger.error(exception)
            raise exception

reportingService = ReportingService()
