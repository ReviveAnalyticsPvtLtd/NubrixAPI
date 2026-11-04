"""
API router for dashboard operations.

This module provides endpoints for creating pages, exporting widgets, retrieving data, editing widget positions, deleting dashboard elements, and fetching columns for dashboards.
"""

__version__ = "1.0.0"
__author__ = "Rauhan Ahmed Siddiqui"
__all__ = ["router"]


from utils.exceptionHandler import CustomException, raiseHttpException
import asyncio
from api.services.dashboardService import dashboardService
from fastapi.responses import ORJSONResponse
from fastapi import APIRouter, Depends
from api.commons import verifyUser, requireProjectOwner, UserContext
from api.models import (
    DeleteDashboardElement,
    ExportToDashboard,
    EditWidgetPosition,
    CreatePage,
    GetData
)

router = APIRouter()
"""
Router for dashboard-related endpoints.
"""

@router.post("/createPage")
async def createPage(details: CreatePage, user: UserContext = Depends(verifyUser)):
    """
    Create a new dashboard page.

    Args:
        details (CreatePage): The details required to create a new page.
        user: UserContext injected after session verification.

    Returns:
        ORJSONResponse: Success message with the new page ID or error message.
    """
    await asyncio.to_thread(requireProjectOwner, details.projectId, user)
    try:
        pageId = dashboardService.createPage(details = details)
        return ORJSONResponse(status_code = 200, content = {"status": "SUCCESS", "pageId": pageId})
    except CustomException as e:
        raiseHttpException(e)
    
@router.get("/getAllPages")
async def getAllPages(projectId: str, user: UserContext = Depends(verifyUser)):
    """
    Retrieve all pages for a given project.

    Args:
        projectId (str): The ID of the project.
        user: UserContext injected after session verification.

    Returns:
        ORJSONResponse: List of pages or error message.
    """
    await asyncio.to_thread(requireProjectOwner, projectId, user)
    try:
        pages = dashboardService.getAllPages(projectId = projectId)
        return ORJSONResponse(status_code = 200, content = {"status": "SUCCESS", "pages": pages})
    except CustomException as e:
        raiseHttpException(e)
    
@router.post("/exportToDashboard")
async def exportToDashboard(details: ExportToDashboard, user: UserContext = Depends(verifyUser)):
    """
    Export a widget to the dashboard.

    Args:
        details (ExportToDashboard): The details required to export a widget.
        user: UserContext injected after session verification.

    Returns:
        ORJSONResponse: Success message with the widget ID or error message.
    """
    await asyncio.to_thread(requireProjectOwner, details.projectId, user)
    try:
        widgetId = dashboardService.exportToDashboard(details = details)
        return ORJSONResponse(status_code = 200, content = {"status": "SUCCESS", "widgetId": widgetId})
    except CustomException as e:
        raiseHttpException(e)
    
@router.post("/getData")
async def getData(details: GetData, user: UserContext = Depends(verifyUser)):
    """
    Retrieve data for a dashboard page.

    Args:
        details (GetData): The details specifying which data to retrieve.
        user: UserContext injected after session verification.

    Returns:
        ORJSONResponse: Page data or error message.
    """
    await asyncio.to_thread(requireProjectOwner, details.projectId, user)
    try:
        pageInfo = dashboardService.getData(details = details)
        return ORJSONResponse(status_code = 200, content = {"status": "SUCCESS", "pageData": pageInfo})
    except CustomException as e:
        raiseHttpException(e)
    
@router.put("/editWidgetPosition")
async def editWidgetPosition(details: EditWidgetPosition, user: UserContext = Depends(verifyUser)):
    """
    Edit the position of a widget on a dashboard page.

    Args:
        details (EditWidgetPosition): The details for editing widget position.
        user: UserContext injected after session verification.

    Returns:
        ORJSONResponse: Updated page data or error message.
    """
    await asyncio.to_thread(requireProjectOwner, details.projectId, user)
    try:
        pageInfo = dashboardService.editWidgetPosition(details = details)
        return ORJSONResponse(status_code = 200, content = {"status": "SUCCESS", "pageData": pageInfo})
    except CustomException as e:
        raiseHttpException(e)

@router.delete("/deleteDashboardElement")
async def deleteDashboardElement(details: DeleteDashboardElement, user: UserContext = Depends(verifyUser)):
    """
    Delete an element from the dashboard.

    Args:
        details (DeleteDashboardElement): The details of the element to delete.
        user: UserContext injected after session verification.

    Returns:
        ORJSONResponse: Success or error message.
    """
    await asyncio.to_thread(requireProjectOwner, details.projectId, user)
    try:
        dashboardService.deleteDashboardElement(details = details)
        return ORJSONResponse(status_code = 200, content = {"status": "SUCCESS", "message": "element deleted successfully."})
    except CustomException as e:
        raiseHttpException(e)

@router.get("/getAllColumns/{projectId}")
async def getAllColumns(projectId: str, user: UserContext = Depends(verifyUser)):
    """
    Retrieve all columns for a given project.

    Args:
        projectId (str): The ID of the project.
        user: UserContext injected after session verification.

    Returns:
        ORJSONResponse: Details of columns or error message.
    """
    await asyncio.to_thread(requireProjectOwner, projectId, user)
    try:
        results = dashboardService.getAllColumns(projectId = projectId)
        return ORJSONResponse(status_code = 200, content = {"status": "SUCCESS", "details": results})
    except CustomException as e:
        raiseHttpException(e)
    
@router.get("/dashboardRefresh/{projectId}")
async def dashboardRefresh(projectId: str, user: UserContext = Depends(verifyUser)):
    await asyncio.to_thread(requireProjectOwner, projectId, user)
    try:
        result = dashboardService.pullDataInParallel(projectId = projectId)
        result.update({"message": "Data refreshed successfully."})
        return ORJSONResponse(status_code = 200, content = result)
    except CustomException as e:
        raiseHttpException(e)