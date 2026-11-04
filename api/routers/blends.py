"""
API router for data blending operations.

This module provides endpoints for creating data blends, retrieving data sources, and fetching fields from sources.
"""
__version__ = "1.0.0"
__author__ = "Rauhan Ahmed Siddiqui"
__all__ = ["router"]


from utils.exceptionHandler import CustomException, raiseHttpException
import asyncio
from api.services.blendService import blendService
from fastapi.responses import ORJSONResponse
from fastapi import APIRouter, Depends
from api.commons import verifyUser, requireProjectOwner, UserContext
from api.models import (
    GetFieldsFromSources,
    CreateDataBlend
)

router = APIRouter()
"""
Router for blend-related endpoints.
"""
    
@router.post("/createDataBlend")
async def createDataBlend(blendDetails: CreateDataBlend, user: UserContext = Depends(verifyUser)):
    """
    Create a new data blend using the provided blend details.

    Args:
        blendDetails (CreateDataBlend): The details required to create a data blend.
        user: UserContext injected after session verification.

    Returns:
        ORJSONResponse: Success or error message.
    """
    await asyncio.to_thread(requireProjectOwner, blendDetails.projectId, user)
    try:
        blendService.createDataBlend(blendDetails = blendDetails)
        return ORJSONResponse(status_code = 200, content = {"status": "SUCCESS", "message": "Blend created successfully."})
    except CustomException as e:
        raiseHttpException(e)

@router.get("/getDataSources")
async def getDataSources(projectId: str, user: UserContext = Depends(verifyUser)):
    """
    Retrieve available data sources for a given project.

    Args:
        projectId (str): The ID of the project to fetch data sources for.
        user: UserContext injected after session verification.

    Returns:
        ORJSONResponse: List of data sources or error message.
    """
    await asyncio.to_thread(requireProjectOwner, projectId, user)
    try:
        dataSources = blendService.getDataSources(projectId = projectId)
        return ORJSONResponse(status_code = 200, content = dataSources)
    except CustomException as e:
        raiseHttpException(e)

@router.post("/getFieldsFromSources")
async def getFieldsFromSources(details: GetFieldsFromSources, user: UserContext = Depends(verifyUser)):
    """
    Get fields from the specified data sources.

    Args:
        details (GetFieldsFromSources): The details specifying which sources to fetch fields from.
        user: UserContext injected after session verification.

    Returns:
        ORJSONResponse: List of fields or error message.
    """
    await asyncio.to_thread(requireProjectOwner, details.projectId, user)
    try:
        response = blendService.getFieldsFromSources(details = details)
        return ORJSONResponse(status_code = 200, content = response)
    except CustomException as e:
        raiseHttpException(e)