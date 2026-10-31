"""
API router for reporting operations.

This module defines endpoints for generating charts and panel charts as part of the reporting functionality. It provides routes for creating visual representations of data based on user-supplied parameters, as well as managing saved favourite queries.

Endpoints:
    - POST /generateChart: Generate a single chart from provided chart details.
    - POST /generatePanelChart: Generate a panel (multi-chart) visualization from provided panel chart details.
    - POST /generateAndExportChartsInParallel: Generate and export multiple charts in parallel.
    - POST /saveQuery: Save a favourite query for a project.
    - GET /getQueries/{projectId}: Retrieve all saved favourite queries for a project.
    - DELETE /deleteQuery: Delete a saved favourite query by its ID.
"""

__version__ = "1.0.0"
__author__ = "Rauhan Ahmed Siddiqui"
__all__ = ["router"]


from utils.exceptionHandler import CustomException, raiseHttpException
import asyncio
from api.models import GenerateChartInput, PanelChartDetails, GenerateChartsInParallel, SaveQuery, DeleteQuery
from api.services.reportingService import reportingService
from fastapi.responses import ORJSONResponse
from fastapi import APIRouter, Depends
from api.commons import verifyUser, requireCredits, requireProjectOwner, UserContext

router = APIRouter()
"""
Router for reporting-related endpoints.
"""

@router.post("/generateChart")
async def generateChart(chartDetails: GenerateChartInput, user: UserContext = Depends(requireCredits("reporting_query"))):
    """
    Generate a chart based on the provided chart details.

    Args:
        chartDetails (GenerateChartInput): The details required to generate the chart, including data, chart type, and configuration.
        user: UserContext injected after credit validation.

    Returns:
        ORJSONResponse: A response containing the generated chart data in JSON format, or an error message if generation fails.

    Raises:
        HTTPException: If an error occurs during chart generation, returns a 500 status code with the error details.
    """
    await asyncio.to_thread(requireProjectOwner, chartDetails.projectId, user)
    try:
        response = await reportingService.generateChart(chartDetails=chartDetails, userId=user.userId)
        return ORJSONResponse(status_code = 200, content = response)
    except CustomException as e:
        raiseHttpException(e)
    
@router.post("/generatePanelChart")
async def generatePanelChart(panelChartDetails: PanelChartDetails, user: UserContext = Depends(verifyUser)):
    """
    Generate a panel chart (multiple charts in a single view) based on the provided panel chart details.

    Args:
        panelChartDetails (PanelChartDetails): The details required to generate the panel chart, including data sources, layout, and configuration for each sub-chart.
        user: UserContext injected after session verification.

    Returns:
        ORJSONResponse: A response containing the generated panel chart data in JSON format, or an error message if generation fails.

    Raises:
        HTTPException: If an error occurs during panel chart generation, returns a 500 status code with the error details.
    """
    await asyncio.to_thread(requireProjectOwner, panelChartDetails.projectId, user)
    try:
        response = await reportingService.generatePanelChart(panelChartDetails = panelChartDetails)
        return ORJSONResponse(status_code = 200, content = response)
    except CustomException as e:
        raiseHttpException(e)
    
@router.post("/generateAndExportChartsInParallel")
async def generateAndExportChartsInParallel(details: GenerateChartsInParallel, user: UserContext = Depends(requireCredits("reporting_query"))):
    """
    Generate and export multiple charts in parallel based on the provided details.

    Args:
        details (GenerateChartsInParallel): The details for generating charts in parallel, including project ID and input queries.
        user: UserContext injected after credit validation.

    Returns:
        ORJSONResponse: A response containing the generated chart data in JSON format, or an error message if generation fails.

    Raises:
        HTTPException: If an error occurs during chart generation, returns a 500 status code with the error details.
    """
    await asyncio.to_thread(requireProjectOwner, details.projectId, user)
    try:
        response = await reportingService.generateChartsInParallel(details=details, userId=user.userId)
        return ORJSONResponse(status_code = 200, content = {"message": "Charts generated successfully", "pageData": response})
    except CustomException as e:
        raiseHttpException(e)

@router.post("/saveQuery")
async def saveQuery(details: SaveQuery, user: UserContext = Depends(verifyUser)):
    """
    Save a user-marked favourite query to a queryConfig.json file in the project's Supabase storage folder.

    Args:
        details (SaveQuery): The details containing the project ID and the favourite query string.
        user: UserContext injected after session verification.

    Returns:
        ORJSONResponse: A success response containing the assigned query ID, or an error message if saving fails.

    Raises:
        HTTPException: If an error occurs during query saving, returns a 500 status code with the error details.
    """
    await asyncio.to_thread(requireProjectOwner, details.projectId, user)
    try:
        queryId = await reportingService.saveQuery(details = details)
        return ORJSONResponse(status_code = 200, content = {"status": "SUCCESS", "message": "Query saved successfully.", "queryId": queryId})
    except CustomException as e:
        raiseHttpException(e)

@router.get("/getQueries/{projectId}")
async def getQueries(projectId: str, user: UserContext = Depends(verifyUser)):
    """
    Retrieve all saved favourite queries for a given project.

    Args:
        projectId (str): The ID of the project.
        user: UserContext injected after session verification.

    Returns:
        ORJSONResponse: A response containing the saved queries dictionary, or an error message if retrieval fails.

    Raises:
        HTTPException: If an error occurs during retrieval, returns a 500 status code with the error details.
    """
    await asyncio.to_thread(requireProjectOwner, projectId, user)
    try:
        queries = await reportingService.getQueries(projectId = projectId)
        return ORJSONResponse(status_code = 200, content = {"status": "SUCCESS", "queries": queries})
    except CustomException as e:
        raiseHttpException(e)

@router.delete("/deleteQuery")
async def deleteQuery(details: DeleteQuery, user: UserContext = Depends(verifyUser)):
    """
    Delete a saved favourite query from the project's queryConfig.json file.

    Args:
        details (DeleteQuery): The details containing the project ID and the query ID to delete.
        user: UserContext injected after session verification.

    Returns:
        ORJSONResponse: A success response confirming deletion, or an error message if deletion fails.

    Raises:
        HTTPException: If an error occurs during deletion, returns a 500 status code with the error details.
    """
    await asyncio.to_thread(requireProjectOwner, details.projectId, user)
    try:
        await reportingService.deleteQuery(details = details)
        return ORJSONResponse(status_code = 200, content = {"status": "SUCCESS", "message": "Query deleted successfully."})
    except CustomException as e:
        raiseHttpException(e)