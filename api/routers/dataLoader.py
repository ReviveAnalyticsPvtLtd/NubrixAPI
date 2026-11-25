"""
API router for data loading operations.

This module provides endpoints for loading data from various sources (CSV, Excel, PDF, MySQL, PostgreSQL, MongoDB) and deleting tables.
"""

__version__ = "1.0.0"
__author__ = "Rauhan Ahmed Siddiqui"
__all__ = ["router"]


from api.models import (
    DatabaseConnection,
    LoadMySQLorPostgreSQL,
    LoadMongoDB,
    DeleteTable
)
from utils.exceptionHandler import CustomException, raiseHttpException
import asyncio
from fastapi import APIRouter, Depends, UploadFile, File, Form
from api.services.dataLoadService import dataLoadService
from fastapi.responses import ORJSONResponse
from api.commons import requireProductAccess, requireCredits, requireProjectOwner, UserContext
from typing import Annotated
from api.services.databaseSources import listTables, storedName, toSource
from api.services.importJobs import importJobs
from utils.connectionTargets import BLOCKED_ADDRESS_MESSAGE, BlockedAddressError
from utils.logger import logger

router = APIRouter()
"""
Router for data loading-related endpoints.
"""

@router.post("/loadCsvData")
async def loadCsvData(projectId: Annotated[str, Form()], files: list[UploadFile], user: UserContext = Depends(requireCredits("metadata_generation"))):
    """
    Load data from CSV file(s) into the specified project and generate metadata
    for the newly added tables.

    Args:
        projectId (str): The ID of the project to load data into.
        files (list[UploadFile]): The CSV files to upload.
        user: UserContext injected after credit validation.

    Returns:
        ORJSONResponse: Success with generated metadata for the new tables.
    """
    await asyncio.to_thread(requireProjectOwner, projectId, user)
    try:
        metadata = await dataLoadService.loadCsvData(projectId=projectId, files=files, userId=user.userId)
        return ORJSONResponse(status_code=200, content={"status": "SUCCESS", "metadata": metadata})
    except CustomException as e:
        raiseHttpException(e)
    
@router.post("/loadExcelData")
async def loadExcelData(projectId: Annotated[str, Form()], files: list[UploadFile], user: UserContext = Depends(requireCredits("metadata_generation"))):
    """
    Load data from Excel file(s) into the specified project and generate metadata
    for the newly added tables.

    Args:
        projectId (str): The ID of the project to load data into.
        files (list[UploadFile]): The Excel files to upload.
        user: UserContext injected after credit validation.

    Returns:
        ORJSONResponse: Success with generated metadata for the new tables.
    """
    await asyncio.to_thread(requireProjectOwner, projectId, user)
    try:
        metadata = await dataLoadService.loadExcelData(projectId=projectId, files=files, userId=user.userId)
        return ORJSONResponse(status_code=200, content={"status": "SUCCESS", "metadata": metadata})
    except CustomException as e:
        raiseHttpException(e)

@router.post("/loadPdfData")
async def loadPdfData(projectId: Annotated[str, Form()], files: list[UploadFile], user: UserContext = Depends(requireCredits("pdf_extraction_per_page"))):
    """
    Load data from PDF files into the specified project and generate metadata
    for the extracted tables.

    Args:
        projectId (str): The ID of the project to load data into.
        files (list[UploadFile]): The PDF files to upload.
        user: UserContext injected after credit validation.

    Returns:
        ORJSONResponse: Success with generated metadata for the new tables.
    """
    await asyncio.to_thread(requireProjectOwner, projectId, user)
    try:
        metadata = await dataLoadService.loadPdfData(projectId=projectId, files=files, userId=user.userId)
        return ORJSONResponse(status_code=200, content={"status": "SUCCESS", "metadata": metadata})
    except CustomException as e:
        raiseHttpException(e)

@router.post("/loadMySql")
async def loadMySql(connection: LoadMySQLorPostgreSQL, user: UserContext = Depends(requireCredits("metadata_generation"))):
    """
    Load data from a MySQL database connection and generate metadata for the
    loaded table.

    Args:
        connection (LoadMySQLorPostgreSQL): Connection details for MySQL.
        user: UserContext injected after credit validation.

    Returns:
        ORJSONResponse: Success with generated metadata for the new table.
    """
    await asyncio.to_thread(requireProjectOwner, connection.projectId, user)
    try:
        metadata = dataLoadService.loadMySql(connection=connection, userId=user.userId)
        return ORJSONResponse(status_code=200, content={"status": "SUCCESS", "metadata": metadata})
    except CustomException as e:
        raiseHttpException(e)
    
@router.post("/loadPostgreSQL")
async def loadPostgreSQL(connection: LoadMySQLorPostgreSQL, user: UserContext = Depends(requireCredits("metadata_generation"))):
    """
    Load data from a PostgreSQL database connection and generate metadata for the
    loaded table.

    Args:
        connection (LoadMySQLorPostgreSQL): Connection details for PostgreSQL.
        user: UserContext injected after credit validation.

    Returns:
        ORJSONResponse: Success with generated metadata for the new table.
    """
    await asyncio.to_thread(requireProjectOwner, connection.projectId, user)
    try:
        metadata = dataLoadService.loadPostgreSQL(connection=connection, userId=user.userId)
        return ORJSONResponse(status_code=200, content={"status": "SUCCESS", "metadata": metadata})
    except CustomException as e:
        raiseHttpException(e)
    
@router.post("/loadMongoDB")
async def loadMongoDB(connection: LoadMongoDB, user: UserContext = Depends(requireCredits("metadata_generation"))):
    """
    Load data from a MongoDB database connection and generate metadata for the
    loaded collection.

    Args:
        connection (LoadMongoDB): Connection details for MongoDB.
        user: UserContext injected after credit validation.

    Returns:
        ORJSONResponse: Success with generated metadata for the new table.
    """
    await asyncio.to_thread(requireProjectOwner, connection.projectId, user)
    try:
        metadata = dataLoadService.loadMongoDB(connection=connection, userId=user.userId)
        return ORJSONResponse(status_code=200, content={"status": "SUCCESS", "metadata": metadata})
    except CustomException as e:
        raiseHttpException(e)

@router.delete("/deleteTable")
async def deleteTable(tableDetails: DeleteTable, user: UserContext = Depends(requireProductAccess)):
    """
    Delete a table from the data source.

    Args:
        tableDetails (DeleteTable): Details of the table to delete.
        user: UserContext injected after session verification.

    Returns:
        ORJSONResponse: Success or error message.
    """
    await asyncio.to_thread(requireProjectOwner, tableDetails.projectId, user)
    try:
        dataLoadService.deleteTable(tableDetails = tableDetails)
        return ORJSONResponse(status_code = 200, content = {"status": "SUCCESS", "message": "Table deleted successfully"})
    except CustomException as e:
        raiseHttpException(e)


_CONNECT_FAILURE = "Unable to connect: check host/port/credentials."


def _refuse(error: Exception, status: int, message: str):
    raiseHttpException(CustomException(error, statusCode=status, uiMessage=message))


def _connectFailure(projectId: str, error: Exception):
    # Logged by type only: a driver's message can quote the host, user or password.
    logger.warning(f"Import in project {projectId} could not connect ({type(error).__name__}).")
    _refuse(ConnectionError(), 400, _CONNECT_FAILURE)


def _uniqueNames(tables: list) -> list[str]:
    """Stored names, with `_2`, `_3`, ... on the later of two tables that map to the same one."""
    taken, names = set(), []
    for table in tables:
        base = name = storedName(table)
        n = 1
        while name in taken:
            n += 1
            name = f"{base}_{n}"
        taken.add(name)
        names.append(name)
    return names


def _displayName(table: tuple) -> str:
    """The name the user sees: schema-qualified outside `public`."""
    name, schema = table
    return f"{schema}.{name}" if schema not in (None, "public") else name


async def _connect(body: DatabaseConnection) -> tuple:
    """Connect and list the tables, after the route's owner check: the source, its tables and their stored names."""
    try:
        source = await asyncio.to_thread(toSource, body.source, body.model_dump())
    except BlockedAddressError as e:
        _refuse(e, 400, BLOCKED_ADDRESS_MESSAGE)
    except ValueError as e:
        _refuse(e, 400, str(e))
    except Exception as e:
        _connectFailure(body.projectId, e)
    try:
        tables = await asyncio.to_thread(listTables, source)
    except Exception as e:
        # a driver's ValueError can quote the password, so nothing from listTables is passed on
        _connectFailure(body.projectId, e)
    if not tables:
        _refuse(LookupError("No tables found."), 422, "No tables found.")
    return source, tables, _uniqueNames(tables)


@router.post("/listDatabaseTables")
async def listDatabaseTables(body: DatabaseConnection, user: UserContext = Depends(requireProductAccess)):
    """
    List the tables of a database connection, so the user can choose which to import. Imports nothing and takes no
    credits.

    Returns:
        ORJSONResponse: 200 with each table's name and the name it would be stored under.
    """
    await asyncio.to_thread(requireProjectOwner, body.projectId, user)
    _, tables, stored = await _connect(body)
    return ORJSONResponse(status_code=200, content={
        "tables": [{"name": _displayName(table), "storedAs": storedAs} for table, storedAs in zip(tables, stored)]})


@router.post("/importDatabase", status_code=202)
async def importDatabase(body: DatabaseConnection, user: UserContext = Depends(requireCredits("metadata_generation"))):
    """
    Start a background import of every table of a database connection.

    Returns:
        ORJSONResponse: 202 with the jobId and the names the tables will be stored under.
    """
    await asyncio.to_thread(requireProjectOwner, body.projectId, user)
    source, tables, stored = await _connect(body)
    named = [(_displayName(table), storedAs) for table, storedAs in zip(tables, stored)]
    jobId = await asyncio.to_thread(importJobs.start, body.projectId, named)
    if jobId is None:
        _refuse(RuntimeError("import running"), 409, "An import is already running for this project.")
    from nubrix.triggers import celery as celeryModule
    from nubrix.triggers.tasks import databaseImportTask
    try:
        await asyncio.to_thread(
            databaseImportTask.sendBatch, celeryModule.celeryApp, body.projectId, user.userId, jobId,
            source, [[name, schema, storedAs] for (name, schema), storedAs in zip(tables, stored)], start=0)
    except Exception as e:
        logger.error(f"Import job {jobId} in project {body.projectId} could not be enqueued ({type(e).__name__}).")
        await asyncio.to_thread(importJobs.finish, body.projectId, jobId, "failed")  # frees the project's lock
        _refuse(RuntimeError("enqueue failed"), 503, "The import could not be started. Please try again.")
    return ORJSONResponse(status_code=202, content={"jobId": jobId, "tables": stored})


@router.get("/importStatus/{projectId}")
async def importStatus(projectId: str, user: UserContext = Depends(requireProductAccess)):
    """Progress record of the project's latest database import."""
    await asyncio.to_thread(requireProjectOwner, projectId, user)
    record = await asyncio.to_thread(importJobs.get, projectId)
    if record is None:
        _refuse(LookupError("No import found."), 404, "No import found.")
    return ORJSONResponse(status_code=200, content=record)
