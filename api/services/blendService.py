"""
blendService.py

This module provides the BlendService class, which encapsulates business logic for creating data blends, retrieving data sources, and extracting fields from sources for NubrixAI projects. It interacts with the Supabase client and manages blend configurations and metadata in storage.
"""

__version__ = "1.0.0"
__author__ = "Rauhan Ahmed Siddiqui"
__all__ = ["blendService"] 


from api.commons import updateProjectModifiedAt
from utils.exceptionHandler import CustomException
from utils.projectStorage import readJson
from utils.logger import logger
from api.commons import client
from api.models import (
    CreateDataBlend,
    GetFieldsFromSources
)
import json
import io

class BlendService:
    """
    Service class for managing data blending operations.

    Handles creation of data blends, retrieval of data sources, and extraction of fields from sources.
    Interacts with the Supabase client and manages blend configurations and metadata in storage.
    """
    def __init__(self) -> None:
        """
        Initializes the BlendService and sets up the Supabase client.
        """
        logger.info("Initializing Blend Service.")
        self.client = client

    def createDataBlend(self, blendDetails: CreateDataBlend) -> None:
        """
        Create a new data blend configuration for a project.

        Args:
            blendDetails (CreateDataBlend): Details of the blend to create, including tables, join configuration, and blend name.

        Raises:
            CustomException: For any errors during blend creation.
        """
        try:
            joinConfig = {
                "tables": blendDetails.tables,
                "blendOn": blendDetails.blendOn,
                "joinTypes": blendDetails.joinTypes
            }
            if "blendConfig.json" in [x.get("name") for x in self.client.storage.from_("AnalyticsHub").list(path = blendDetails.projectId)]:
                blendConfig = readJson(blendDetails.projectId, "blendConfig.json")
                blendConfig[blendDetails.blendName] = joinConfig
            else:
                blendConfig = {blendDetails.blendName: joinConfig}
            with io.BytesIO() as buffer:
                buffer.write(json.dumps(blendConfig, indent=4).encode("utf-8"))
                buffer.seek(0)
                _ = self.client.storage.from_("AnalyticsHub").upload(path = f"{blendDetails.projectId}/blendConfig.json", file = buffer.getvalue(), file_options = {"upsert": "true"})
            updateProjectModifiedAt(blendDetails.projectId)
            return
        except Exception as e:
            exception = CustomException(e)
            logger.error(exception)
            raise exception   
        
    def getDataSources(self, projectId: str) -> dict:
        """
        Retrieve all data sources (raw tables and blends) for a given project.

        Args:
            projectId (str): The project identifier.

        Returns:
            dict: Dictionary containing blends, raw tables, and blended tables.

        Raises:
            CustomException: For any errors during retrieval.
        """
        try:
            if "blendConfig.json" in [x.get("name") for x in self.client.storage.from_("AnalyticsHub").list(path = projectId)]:
                blendConfig = readJson(projectId, "blendConfig.json")
                blendedTables = list(blendConfig.keys())
                blends = [
                    {"blendName": x, "tables": blendConfig[x].get("tables"), "joinTypes": blendConfig[x].get("joinTypes"), "blendOn": blendConfig[x].get("blendOn")} for x in blendedTables
                ]
            else:
                blends, blendedTables = list(), list()
            metadata = readJson(projectId, "metadata.json")
            rawTables = [k for k, v in metadata.items() if not k.startswith("_") and v.get("isActive", True) is not False]
            dataSources = {
                "blends": blends,
                "rawTables": rawTables,
                "blendedTables": blendedTables
            }
            return dataSources
        except Exception as e:
            exception = CustomException(e)
            logger.error(exception)
            raise exception   
        
    def getFieldsFromSources(self, details: GetFieldsFromSources) -> dict:
        """
        Extract and categorize fields (numerical, categorical, datetime) from a given data source (raw or blend).

        Args:
            details (GetFieldsFromSources): Details specifying the project and table/source name.

        Returns:
            dict: Dictionary with lists of numerical, categorical, and datetime columns.

        Raises:
            CustomException: For any errors during extraction.
        """
        try:
            allFiles = [x.get("name") for x in self.client.storage.from_("AnalyticsHub").list(path = details.projectId)]
            metadata = readJson(details.projectId, "metadata.json")
            if details.tableName in metadata.keys():
                if metadata[details.tableName].get("isActive", True) is False:
                    raise CustomException(
                        ValueError(f"Table '{details.tableName}' is inactive."),
                        statusCode=403,
                        uiMessage=f"Table '{details.tableName}' is inactive. Activate it first to use it."
                    )
                allFields = metadata[details.tableName]["columns"]
            elif "blendConfig.json" in allFiles:
                blendConfig = readJson(details.projectId, "blendConfig.json")
                allFields = list()
                tablesUsed = blendConfig[details.tableName].get("tables")
                blendOn = blendConfig[details.tableName].get("blendOn")

                # Collect column info for each table
                tableColumns = []
                for table in tablesUsed:
                    cols = [{"name": col["name"], "type": col["type"]} for col in metadata[table]["columns"]]
                    tableColumns.append(cols)

                # Simulate merge to determine final column names with suffixes
                resultColumns = list(tableColumns[0])  # Start with first table's columns
                resultColNames = {col["name"] for col in resultColumns}

                for i in range(len(tablesUsed) - 1):
                    joinKeys = blendOn[i] if isinstance(blendOn[i], list) else [blendOn[i]]
                    rightColumns = tableColumns[i + 1]
                    rightColNames = {col["name"] for col in rightColumns}

                    # Find overlapping columns (excluding join keys)
                    overlapping = resultColNames & rightColNames - set(joinKeys)

                    newResultColumns = []
                    for col in resultColumns:
                        if col["name"] in overlapping:
                            newResultColumns.append({"name": col["name"] + "_left", "type": col["type"]})
                        else:
                            newResultColumns.append(col)

                    for col in rightColumns:
                        if col["name"] in joinKeys:
                            newResultColumns.append(col)
                        elif col["name"] in overlapping:
                            newResultColumns.append({"name": col["name"] + "_right", "type": col["type"]})
                        else:
                            newResultColumns.append(col)

                    resultColumns = newResultColumns
                    resultColNames = {col["name"] for col in resultColumns}

                allFields = resultColumns
            else:
                pass
            numericals = ["int64", "float64", "float32", "int32"]
            categoricals = ["bool", "category", "object", "string"]
            datetimeTypes = ["datetime64[ns]", "datetime64[ns, tz]"]
            numericalColumns, categoricalColumns, datetimeColumns = list(), list(), list()
            for column in allFields:
                if column.get("type") in categoricals:
                    categoricalColumns.append(column["name"])
                elif column["type"] in numericals:
                    numericalColumns.append(column["name"])
                elif column["type"] in datetimeTypes:
                    datetimeColumns.append(column["name"])
            response = {
                "numericalColumns": numericalColumns,
                "categoricalColumns": categoricalColumns,
                "datetimeColumns": datetimeColumns
            } 
            return response
        except Exception as e:
            exception = CustomException(e)
            logger.error(exception)
            raise exception   
        
blendService = BlendService()