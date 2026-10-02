"""
dashboardService.py

This module provides the DashboardService class, which encapsulates business logic for managing dashboards, pages, widgets, and data retrieval for NubrixAI projects. It interacts with the Supabase client and manages dashboard configurations and widget data in storage.
"""

__version__ = "1.0.0"
__author__ = "Rauhan Ahmed Siddiqui"
__all__ = ["dashboardService"] 


from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from api.commons import updateProjectModifiedAt
from utils.exceptionHandler import CustomException
from utils.codeExecutor import replManager, _remove_code_fences
from sqlalchemy import URL, create_engine
from urllib.request import urlopen
from utils.logger import logger
from api.commons import client
from api.models import (
    DeleteDashboardElement,
    ExportToDashboard,
    EditWidgetPosition,
    CreatePage,
    GetData
)
import pandas as pd
import tempfile
import uuid
import json
import time
import ast
import os
import io
import re

class _FetchDataFilterTransformer(ast.NodeTransformer):
    def __init__(self, filters: list):
        self.filters = filters

    def visit_Call(self, node):
        if isinstance(node.func, ast.Name) and node.func.id == "fetch_data":
            filters_node = ast.parse(repr(self.filters)).body[0].value
            node.args.append(filters_node)
        return self.generic_visit(node)

class DashboardService:
    """
    Service class for managing dashboards, pages, widgets, and data retrieval.

    Handles creation and management of dashboard pages and widgets, exporting widgets, retrieving data, editing widget positions, deleting dashboard elements, and extracting all columns from project tables.
    Interacts with the Supabase client and manages dashboard configurations and widget data in storage.
    """
    def __init__(self) -> None:
        """
        Initializes the DashboardService and sets up the Supabase client.
        """
        logger.info("Initializing Dashboard Service.")
        self.client = client

    @staticmethod
    def _addCodeFences(code: str):
        """Add code fences to a string."""
        return "```python\n" + code.strip() + "\n```"

    @staticmethod
    def _applyFilterToAWidget(widget: dict, filters: list, codeExecutor: callable, projectId: str) -> dict:
        """
        Apply filters to a widget's generated code and update its data accordingly.
        """
        widget = widget.copy()
        code = widget.get("generatedCode")
        if "```" in code: code = _remove_code_fences(code)
        tree = ast.parse(code)
        transformer = _FetchDataFilterTransformer(filters)
        tree = transformer.visit(tree)
        ast.fix_missing_locations(tree)
        code = ast.unparse(tree)
        code = DashboardService._addCodeFences(code)
        result = codeExecutor.run(code, projectId=projectId)
        widget["generatedCode"] = code
        try:
            resultDict = json.loads(result)
            widget.update(resultDict)
        except:
            widgetChartType = widget.get("chartType")
            if widgetChartType == "card":
                widget["data"] = None
            else:
                dataKey = widget.get("data")
                datasets = dataKey.get("datasets")
                for dataset in datasets:
                    dataset["data"] = list()
        return widget
    
    @staticmethod
    def _getDataTypes(projectId: str, tableName: str) -> list[dict]:   
        """
        Retrieve data types and metadata for all columns in a given table.

        Args:
            projectId (str): The project identifier.
            tableName (str): The table name.

        Returns:
            list[dict]: List of dictionaries containing column metadata.
        """
        fileUrl = os.environ["FILE_URL"].format(projectId = projectId, fileName = tableName)
        df = pd.read_parquet(fileUrl)
        numericals = ["int64", "float64", "float32", "int32"]
        categoricals = ["bool", "category", "object", "string"]
        datetimeTypes = ["datetime64[ns]", "datetime64[ns, tz]"]
        allColumns = list()
        for column in df.columns:
            dtype = df[column].dtype
            if dtype in numericals:
                columnInfo = dict()
                columnInfo["columnName"] = column
                columnInfo["type"] = dtype.name
                columnInfo["min"] = df[column].min()
                columnInfo["max"] = df[column].max()
                allColumns.append(columnInfo)
            elif df[column].dtype in datetimeTypes:
                columnInfo = dict()
                columnInfo["columnName"] = column
                columnInfo["type"] = dtype.name
                columnInfo["min"] = str(df[column].min())
                columnInfo["max"] = str(df[column].max())
                allColumns.append(columnInfo)
            else:
                columnInfo = dict()
                columnInfo["columnName"] = column
                columnInfo["type"] = dtype.name
                columnInfo["uniqueValues"] = [str(x) for x in df[column].unique().tolist()]
                allColumns.append(columnInfo)
        return allColumns

    def createPage(self, details: CreatePage) -> str:
        """
        Create a new dashboard page for a project.

        Args:
            details (CreatePage): Details of the page to create.

        Returns:
            str: The unique page ID of the newly created page.

        Raises:
            CustomException: For any errors during page creation.
        """
        try:
            pageId = str(uuid.uuid4())
            if "dashboardConfig.json" in [x.get("name") for x in self.client.storage.from_("AnalyticsHub").list(path = details.projectId)]:
                fileUrl = os.environ["FILE_URL"].format(projectId = details.projectId, fileName = "dashboardConfig.json").replace(".parquet", "") + f"?cb={int(time.time())}"
                dashboardConfig = json.loads(urlopen(fileUrl).read())
                dashboardConfig[pageId] = {"name": details.pageName, "widgets": []}
            else:
                dashboardConfig = {pageId: {"name": details.pageName, "widgets": []}}
            with io.BytesIO() as buffer:
                buffer.write(json.dumps(dashboardConfig, indent=4).encode("utf-8"))
                buffer.seek(0)
                _ = self.client.storage.from_("AnalyticsHub").upload(path = f"{details.projectId}/dashboardConfig.json", file = buffer.getvalue(), file_options = {"upsert": "true"})   
            updateProjectModifiedAt(details.projectId)
            return pageId
        except Exception as e:
            exception = CustomException(e)
            logger.error(exception)
            raise exception  

    def getAllPages(self, projectId: str) -> list:
        """
        Retrieve all dashboard pages for a given project.

        Args:
            projectId (str): The project identifier.

        Returns:
            list: List of dictionaries containing page names and IDs.

        Raises:
            CustomException: For any errors during retrieval.
        """
        try:
            if "dashboardConfig.json" in [x.get("name") for x in self.client.storage.from_("AnalyticsHub").list(path = projectId)]:
                fileUrl = os.environ["FILE_URL"].format(projectId = projectId, fileName = "dashboardConfig.json").replace(".parquet", "") + f"?cb={int(time.time())}"
                dashboardConfig = json.loads(urlopen(fileUrl).read())
                pages = [{"pageName": dashboardConfig[x]["name"], "pageId": x} for x in dashboardConfig.keys()]
            else:
                pages = list()
            return pages
        except Exception as e:
            exception = CustomException(e)
            logger.error(exception)
            raise exception   
        
    def exportToDashboard(self, details: ExportToDashboard) -> str:
        """
        Export a widget to a dashboard page.

        Args:
            details (ExportToDashboard): Details of the widget and page to export to.

        Returns:
            str: The unique widget ID of the newly exported widget.

        Raises:
            CustomException: For any errors during export.
        """
        try:
            fileUrl = os.environ["FILE_URL"].format(projectId = details.projectId, fileName = "dashboardConfig.json").replace(".parquet", "") + f"?cb={int(time.time())}"
            dashboardConfig = json.loads(urlopen(fileUrl).read())
            pageDict = dashboardConfig.get(details.page)
            widgetId = str(uuid.uuid4())
            newWidget = {
                "id": widgetId,
                "chartType": details.chartType,
                "title": details.title,
                "label": details.label,
                "xLabels": details.xLabels,
                "yLabels": details.yLabels,
                "data": details.data,
                "map": details.map,
                "layout": details.layout,
                "generatedCode": details.generatedCode
            }
            pageDict["widgets"].append(newWidget)
            dashboardConfig[details.page] = pageDict
            with io.BytesIO() as buffer:
                buffer.write(json.dumps(dashboardConfig, indent=4).encode("utf-8"))
                buffer.seek(0)
                _ = self.client.storage.from_("AnalyticsHub").upload(path = f"{details.projectId}/dashboardConfig.json", file = buffer.getvalue(), file_options = {"upsert": "true"}) 
            updateProjectModifiedAt(details.projectId)
            return widgetId     
        except Exception as e:
            exception = CustomException(e)
            logger.error(exception)
            raise exception  
        
    def getData(self, details: GetData) -> dict:
        """
        Retrieve data for a dashboard page, applying filters if provided.

        Args:
            details (GetData): Details specifying the project, page, and filters.

        Returns:
            dict: Dictionary containing page and widget data.

        Raises:
            CustomException: For any errors during data retrieval.
        """
        try:
            fileUrl = os.environ["FILE_URL"].format(projectId = details.projectId, fileName = "dashboardConfig.json").replace(".parquet", "") + f"?cb={int(time.time())}"
            dashboardConfig = json.loads(urlopen(fileUrl).read())
            pageInfo = dashboardConfig.get(details.page)
            pageInfo["id"] = details.page
            if (not details.filters) and (not details.refresh):
                pass
            elif (details.filters) and (not details.refresh):
                widgets = pageInfo.get("widgets")
                numWidgets = len(widgets)
                with ProcessPoolExecutor(max_workers = 4) as executor:
                    results = executor.map(self._applyFilterToAWidget, widgets, [details.filters] * numWidgets, [replManager] * numWidgets, [details.projectId] * numWidgets)
                pageInfo["widgets"] = [x for x in results]
            elif (not details.filters) and (details.refresh):
                widgets = pageInfo.get("widgets")
                numWidgets = len(widgets)
                with ProcessPoolExecutor(max_workers = 4) as executor:
                    results = executor.map(self._applyFilterToAWidget, widgets, [details.filters] * numWidgets, [replManager] * numWidgets, [details.projectId] * numWidgets)
                pageInfo["widgets"] = [x for x in results] 
                prevPageInfo = dashboardConfig.get(details.page)
                for prevWidget in prevPageInfo.get("widgets"):
                    for newWidget in pageInfo.get("widgets"):
                        if newWidget.get("id") == prevWidget.get("id"):
                            prevWidget.update(newWidget)
                        else:
                            continue
                dashboardConfig[details.page] = prevPageInfo
                with io.BytesIO() as buffer:
                    buffer.write(json.dumps(dashboardConfig, indent=4).encode("utf-8"))
                    buffer.seek(0)
                    _ = self.client.storage.from_("AnalyticsHub").upload(path = f"{details.projectId}/dashboardConfig.json", file = buffer.getvalue(), file_options = {"upsert": "true"})    
            else:
                raise ValueError("Filters and Refresh cannot be implemented simultaneously.")
            return pageInfo
        except Exception as e:
            exception = CustomException(e)
            logger.error(exception)
            raise exception
        
    def editWidgetPosition(self, details: EditWidgetPosition) -> dict:
        """
        Edit the position/layout of widgets on a dashboard page.

        Args:
            details (EditWidgetPosition): Details specifying the project, page, and new widget layouts.

        Returns:
            dict: Updated page information with new widget layouts.

        Raises:
            CustomException: For any errors during update.
        """
        try:
            fileUrl = os.environ["FILE_URL"].format(projectId = details.projectId, fileName = "dashboardConfig.json").replace(".parquet", "") + f"?cb={int(time.time())}"
            dashboardConfig = json.loads(urlopen(fileUrl).read())
            pageInfo = dashboardConfig.get(details.pageId)
            pageInfo["name"] = details.pageName
            widgets = pageInfo.get("widgets")
            if details.widgets:
                for newWidget in details.widgets:
                    newWidgetId = newWidget.get("id")
                    for widget in widgets:
                        widgetId = widget.get("id")
                        if widgetId == newWidgetId:
                            widget["layout"] = newWidget["layout"]
                        else:
                            continue
            with io.BytesIO() as buffer:
                buffer.write(json.dumps(dashboardConfig, indent = 4).encode("utf-8"))
                buffer.seek(0)
                self.client.storage.from_("AnalyticsHub").upload(path = f"{details.projectId}/dashboardConfig.json", file = buffer.getvalue(), file_options = {"upsert": "true"})
            updateProjectModifiedAt(details.projectId)
            return pageInfo
        except Exception as e:
            exception  = CustomException(e)
            logger.error(exception)
            raise exception

    def _reconcileInsightChartedStatus(self, projectId: str, dashboardConfig: dict) -> None:
        """
        Reconcile the isCharted flag in insights.json after a dashboard
        element has been deleted.

        Collects all 'query' values from remaining widgets across all pages,
        then sets isCharted=False for any insight whose query is no longer
        represented on any dashboard page.

        Args:
            projectId (str): The project identifier.
            dashboardConfig (dict): The *already-updated* dashboard config
                                    (after the element has been removed).
        """
        try:
            # Collect all query strings still present on the dashboard
            remainingQueries = set()
            for pageId, pageData in dashboardConfig.items():
                for widget in pageData.get("widgets", []):
                    query = widget.get("query")
                    if query:
                        remainingQueries.add(query)

            # Load insights.json
            insightsFileNames = [
                x.get("name")
                for x in self.client.storage.from_("AnalyticsHub").list(path=projectId)
            ]
            if "insights.json" not in insightsFileNames:
                return

            insightsUrl = os.environ["FILE_URL"].format(
                projectId=projectId, fileName="insights.json"
            ).replace(".parquet", "") + f"?cb={int(time.time())}"
            insights = json.loads(urlopen(insightsUrl).read())

            # Reset isCharted for insights whose query is no longer on the dashboard
            changed = False
            for insight in insights.get("insights", []):
                wasCharted = insight.get("isCharted", False)
                shouldBeCharted = insight.get("query") in remainingQueries
                if wasCharted and not shouldBeCharted:
                    insight["isCharted"] = False
                    changed = True

            if changed:
                with io.BytesIO() as buffer:
                    buffer.write(json.dumps(insights, indent=4).encode("utf-8"))
                    buffer.seek(0)
                    self.client.storage.from_("AnalyticsHub").upload(
                        path=f"{projectId}/insights.json",
                        file=buffer.getvalue(),
                        file_options={"upsert": "true"},
                    )
                logger.info(f"Reconciled isCharted flags in insights.json for project {projectId}.")
        except Exception as e:
            # Non-fatal: log but do not propagate so the delete itself still succeeds
            logger.warning(f"Failed to reconcile insight charted status for project {projectId}: {e}")

    def deleteDashboardElement(self, details: DeleteDashboardElement) -> None:
        """
        Delete a dashboard element (page or widget) from a project.
        After deletion, reconciles insights.json to reset the isCharted flag
        for any KPIs whose widgets are no longer on any dashboard page.

        Args:
            details (DeleteDashboardElement): Details specifying the element to delete.

        Raises:
            CustomException: For any errors during deletion.
        """
        try:
            fileUrl = os.environ["FILE_URL"].format(projectId = details.projectId, fileName = "dashboardConfig.json").replace(".parquet", "") + f"?cb={int(time.time())}"
            dashboardConfig = json.loads(urlopen(fileUrl).read())
            if details.deletionObject == "page":
                dashboardConfig.pop(details.id)
            elif details.deletionObject == "widget":
                for pageId in dashboardConfig.keys():
                    page = dashboardConfig.get(pageId)
                    pageWidgets = page.get("widgets")
                    for widget in pageWidgets:
                        if widget.get("id") == details.id:
                            pageWidgets.remove(widget)
                            break
                        else: continue
            with io.BytesIO() as buffer:
                buffer.write(json.dumps(dashboardConfig, indent=4).encode("utf-8"))
                buffer.seek(0)
                _ = self.client.storage.from_("AnalyticsHub").upload(path = f"{details.projectId}/dashboardConfig.json", file = buffer.getvalue(), file_options = {"upsert": "true"}) 
            
            # Reconcile insights.json — reset isCharted for removed KPIs
            self._reconcileInsightChartedStatus(details.projectId, dashboardConfig)
            
            updateProjectModifiedAt(details.projectId)
            return 
        except Exception as e:
            exception = CustomException(e)
            logger.error(exception)
            raise exception
        
    def getAllColumns(self, projectId: str) -> dict:
        """
        Retrieve all columns and their metadata for all tables in a project.

        Args:
            projectId (str): The project identifier.

        Returns:
            dict: Dictionary mapping table names to lists of column metadata.

        Raises:
            CustomException: For any errors during retrieval.
        """
        try:
            dataTables = ["".join(os.path.splitext(x.get("name"))[:-1]) for x in client.storage.from_("AnalyticsHub").list(path = projectId) if x.get("name").endswith(".parquet")]
            with ThreadPoolExecutor(max_workers = 5) as executor:
                results = executor.map(self._getDataTypes, [projectId] * len(dataTables), dataTables)
            results = list(results)
            results = {x: y for x, y in zip(dataTables, results)}
            return results
        except Exception as e:
            exception  = CustomException(e)
            logger.error(exception)
            raise exception
        
    @staticmethod   
    def _pullData(connection: dict):
        """
        Retrieve data from a database connection as a pandas DataFrame.

        Args:
            connection (dict): Dictionary containing database connection details,
                including type, user, password, host, port, db, and table.

        Returns:
            pandas.DataFrame: DataFrame containing the contents of the specified table.

        Raises:
            CustomException: For any errors during data retrieval or connection issues.
        """
        if connection.get("type") == "MySQL/PostgreSQL":
            connStr = URL.create(
                "mysql+pymysql",
                username=connection.get("user"),
                password=connection.get("password"),
                host=connection.get("host"),
                port=connection.get("port"),
                database=connection.get("db"),
            )
            engine = create_engine(connStr)
            dataFrame = pd.read_sql(f"SELECT * FROM {connection.get('table')}", engine, parse_dates = True)
            return dataFrame
        else:
            ...
        
    def _updateMetadataShapes(self, projectId: str, shapes: dict) -> None:
        """Override the row/column counts in metadata.json with fresh deterministic values.

        Args:
            projectId (str): The project identifier.
            shapes (dict): ``{tableName: [rows, cols]}`` from the just-refreshed data.
        """
        try:
            files = [x.get("name") for x in client.storage.from_("AnalyticsHub").list(path = projectId)]
            if "metadata.json" not in files:
                return
            metadataUrl = os.environ["FILE_URL"].format(projectId = projectId, fileName = "metadata.json").replace(".parquet", "") + f"?cb={int(time.time())}"
            metadata = json.loads(urlopen(metadataUrl).read())
            changed = False
            for tableName, shape in shapes.items():
                if metadata.get(tableName, {}).get("shape") != shape:
                    metadata.setdefault(tableName, {})["shape"] = shape
                    changed = True
            if not changed:
                return
            with io.BytesIO() as buffer:
                buffer.write(json.dumps(metadata, indent=4).encode("utf-8"))
                buffer.seek(0)
                client.storage.from_("AnalyticsHub").upload(
                    path = f"{projectId}/metadata.json",
                    file = buffer.getvalue(),
                    file_options = {"upsert": "true"}
                )
        except Exception as e:
            logger.warning(f"Failed to update metadata shapes after refresh for {projectId}: {e}")

    def pullDataInParallel(self, projectId: str) -> dict:
        """
        Retrieve data from multiple databases in parallel and upload results to the NubrixAI storage bucket.

        Args:
            projectId (str): The project identifier used to locate the connections.json file.

        Returns:
            dict: Dictionary containing the operation status, e.g., {"status": "SUCCESS"}.

        Raises:
            CustomException: For any errors encountered during data extraction or upload.
        """
        try:
            if "connections.json" in [x.get("name") for x in client.storage.from_("AnalyticsHub").list(path = projectId)]:
                databaseConnectionsUrl = os.environ["FILE_URL"].format(projectId = projectId, fileName = "connections.json").replace(".parquet", "") + f"?cb={int(time.time())}"
                databaseConnections = json.loads(urlopen(databaseConnectionsUrl).read())
            else:
                return {"status": "SUCCESS"}
            connections = databaseConnections.values()
            with ThreadPoolExecutor(max_workers = 4) as executor:
                futures = [executor.submit(self._pullData, connection) for connection in connections]
                results = [x.result() for x in futures]
                results = {connection.get("table"): result for connection, result in zip(connections, results)}
            for result in results:
                with tempfile.NamedTemporaryFile(delete = True, suffix = ".parquet") as temp:
                    results[result].to_parquet(temp.name, compression = "snappy")
                    _ = self.client.storage.from_("AnalyticsHub").upload(
                        file = temp.name,
                        path = f"{projectId}/{result + '.parquet'}",
                        file_options = {"upsert": "true"}
                    )
                temp.close()
            # Deterministically refresh row/column counts in metadata.json so
            # they never drift from the just-refreshed parquet files.
            shapes = {name: [int(len(df.index)), int(len(df.columns))] for name, df in results.items()}
            self._updateMetadataShapes(projectId, shapes)
            # Drop the in-process Arrow LRU so fetch/classify see the new data.
            try:
                from utils.initMethods import invalidate_data_cache
                invalidate_data_cache(projectId=projectId)
            except Exception as e:
                logger.warning(f"Failed to invalidate data cache after refresh for {projectId}: {e}")
            updateProjectModifiedAt(projectId)
            return {"status": "SUCCESS"}
        except Exception as e:
            exception  = CustomException(e)
            logger.error(exception)
            raise exception
    

dashboardService = DashboardService()
