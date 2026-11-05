"""
API router for project and management operations.

This module provides endpoints for project creation, listing, state updates, metadata management, trigger management, and report generation.
"""

__version__ = "1.0.0"
__author__ = "Rauhan Ahmed Siddiqui"
__all__ = ["router"]


from utils.exceptionHandler import CustomException, raiseHttpException
from utils.projectStorage import readTablePage
import asyncio
from fastapi import APIRouter, Depends, Form, Request, Query
from api.services.managementService import managementService
from fastapi.responses import ORJSONResponse, HTMLResponse
from starlette.datastructures import UploadFile as StarletteUploadFile
from api.commons import requireWorkspaceOwner
from api.models import (
    UpdateProjectState,
    CreateProject,
    EditMetadata,
    RenameProject
)
from api.commons import verifyToken, requireProductAccess, requireProductAccessToken, requireCredits, requireProjectOwner, requireTrialOrAbove, UserContext

router = APIRouter()
"""
Router for project and management-related endpoints.
"""

@router.post("/createProject")
async def createProject(projectDetails: CreateProject, user: UserContext = Depends(requireProductAccess)):
    """
    Create a new project.

    Status Codes:
        200 - Success
        401 - Please login to create a project.
        404 - Workspace not found.
        409 - A project with this name already exists in the workspace.
        422 - Invalid project details.
        500 - Failed to create project. Try again later.
        503 - Workspace access could not be verified.
    """
    await asyncio.to_thread(requireWorkspaceOwner, projectDetails.workspaceId, user)
    try:
        projectId = managementService.createProject(projectDetails=projectDetails, token=user.token)
        return ORJSONResponse(
            status_code=200,
            content={
                "status": "SUCCESS",
                "projectId": projectId,
                "message": "Project created successfully"
            }
        )
    except CustomException as e:
        raiseHttpException(e)
    
@router.post("/createWorkspace/{workspaceName}")
async def createWorkspace(workspaceName: str, token = Depends(requireProductAccessToken)):
    """
    Create a new workspace.

    Status Codes:
        200 - Success
        401 - Please login to create a workspace.
        409 - Workspace with this name already exists.
        422 - Invalid workspace name.
        500 - Unable to create workspace. Try again later.
    """
    try:
        workspaceId = managementService.createWorkspace(workspaceName=workspaceName, token=token)
        return ORJSONResponse(
            status_code=200,
            content={
                "status": "SUCCESS",
                "workspaceId": workspaceId,
                "message": "Workspace created successfully"
            }
        )
    except CustomException as e:
        raiseHttpException(e)
    
@router.get("/listProjects/{workspaceId}")
async def listProjects(workspaceId: str, token = Depends(requireProductAccessToken)):
    """
    List all projects accessible to the user.

    Args:
        token: Authorization token dependency.

    Returns:
        ORJSONResponse: List of projects or error message.
    """
    try:
        data = managementService.listProjects(workspaceId = workspaceId, token = token)
        return ORJSONResponse(status_code = 200, content = {"projects": data.to_dict(orient = "records")})
    except CustomException as e:
        raiseHttpException(e)
    
@router.get("/listWorkspaces")
async def listWorkspaces(token = Depends(requireProductAccessToken)):
    """
    List all workspaces accessible to the user.

    Args:
        token: Authorization token dependency.

    Returns:
        ORJSONResponse: List of workspaces or error message.
    """
    try:
        response = managementService.listWokspaces(token = token)
        return ORJSONResponse(status_code = 200, content = response)
    except CustomException as e:
        raiseHttpException(e)

@router.patch("/updateCurrentWorkspace/{updatedWorkspaceId}")
async def updateCurrentWorkspace(updatedWorkspaceId: str, token = Depends(requireProductAccessToken)):
    """
    Update the workspace id of a user.

    Args:
        updatedWorkspaceId (str): Details for updating current workspace of a user.
        token: Authorization token dependency.

    Returns:
        ORJSONResponse: Success or error message.
    """
    try:
        managementService.updateCurrentWorkspace(updatedWorkspaceId = updatedWorkspaceId, token = token)
        return ORJSONResponse(status_code = 200, content = {"status": "SUCCESS", "message": "Current workspace updated successfully"})
    except CustomException as e:
        raiseHttpException(e)

@router.patch("/updateWorkspaceName/{workspaceId}/{newWorkspaceName}")
async def updateWorkspaceName(workspaceId: str, newWorkspaceName: str, token = Depends(requireProductAccessToken)):
    """
    Update the name of a workspace.

    Status Codes:
        200 - Success
        401 - Please login to update workspace.
        404 - Workspace not found.
        409 - A workspace with this name already exists.
        422 - Invalid workspace name.
        500 - Failed to update workspace name. Try again later.
    """
    try:
        managementService.updateWorkspaceName(workspaceId=workspaceId, newWorkspaceName=newWorkspaceName, token=token)
        return ORJSONResponse(
            status_code=200,
            content={
                "status": "SUCCESS",
                "message": "Workspace name updated successfully"
            }
        )
    except CustomException as e:
        raiseHttpException(e)

@router.delete("/deleteWorkspace/{workspaceId}")
async def deleteWorkspace(workspaceId: str, token = Depends(requireProductAccessToken)):
    """
    Delete a workspace and all its projects.

    Status Codes:
        200 - Success
        401 - Please login to delete workspace.
        404 - Workspace not found.
        500 - Failed to delete workspace. Try again later.
    """
    try:
        managementService.deleteWorkspace(workspaceId=workspaceId, token=token)
        return ORJSONResponse(
            status_code=200,
            content={
                "status": "SUCCESS",
                "message": "Workspace and all associated projects deleted successfully"
            }
        )
    except CustomException as e:
        raiseHttpException(e)

@router.patch("/updateBookmark")
async def updateBookmark(updateBookmarkDetails: UpdateProjectState, user: UserContext = Depends(requireProductAccess)):
    """
    Update the bookmark status of a project.

    Args:
        updateBookmarkDetails (UpdateProjectState): Details for updating bookmark status.
        user: UserContext injected after session verification.

    Returns:
        ORJSONResponse: Success or error message.
    """
    await asyncio.to_thread(requireProjectOwner, updateBookmarkDetails.projectId, user)
    try:
        managementService.updateBookmark(updateBookmarkDetails = updateBookmarkDetails)
        return ORJSONResponse(status_code = 200, content = {"status": "SUCCESS", "message": "Project bookmark status updated successfully"})
    except CustomException as e:
        raiseHttpException(e)
    
@router.patch("/updateArchive")
async def updateArchive(updateArchiveDetails: UpdateProjectState, user: UserContext = Depends(requireProductAccess)):
    """
    Update the archive status of a project.

    Args:
        updateArchiveDetails (UpdateProjectState): Details for updating archive status.
        user: UserContext injected after session verification.

    Returns:
        ORJSONResponse: Success or error message.
    """
    await asyncio.to_thread(requireProjectOwner, updateArchiveDetails.projectId, user)
    try:
        managementService.updateArchive(updateArchiveDetails = updateArchiveDetails)
        return ORJSONResponse(status_code = 200, content = {"status": "SUCCESS", "message": "Project archive status updated successfully"})
    except CustomException as e:
        raiseHttpException(e)
    
@router.patch("/updateTrash")
async def updateTrash(updateTrashDetails: UpdateProjectState, user: UserContext = Depends(requireProductAccess)):
    """
    Update the trash status of a project.

    Args:
        updateTrashDetails (UpdateProjectState): Details for updating trash status.
        user: UserContext injected after session verification.

    Returns:
        ORJSONResponse: Success or error message.
    """
    await asyncio.to_thread(requireProjectOwner, updateTrashDetails.projectId, user)
    try:
        managementService.updateTrash(updateTrashDetails = updateTrashDetails)
        return ORJSONResponse(status_code = 200, content = {"status": "SUCCESS", "message": "Project trash status updated successfully"})
    except CustomException as e:
        raiseHttpException(e)
    
@router.post("/generateMetadata/{projectId}")
async def generateMetadata(projectId: str, user: UserContext = Depends(requireCredits("metadata_generation"))):
    """
    Generate metadata for a given project.

    Args:
        projectId (str): The ID of the project.
        user: UserContext injected after credit validation.

    Returns:
        ORJSONResponse: Generated metadata and insights or error message.
    """
    await asyncio.to_thread(requireProjectOwner, projectId, user)
    try:
        jsonData = managementService.generateMetadata(projectId=projectId, userId=user.userId)
        return ORJSONResponse(status_code = 200, content = {"status": "SUCCESS", "metadata": jsonData})
    except CustomException as e:
        raiseHttpException(e)
    
@router.post("/generateKpis/{projectId}")
async def generateKpis(projectId: str, preserveCharted: bool = False, user: UserContext = Depends(requireCredits("insight_generation"))):
    """
    Generate important KPIs for a given project.

    Args:
        projectId (str): The ID of the project.
        preserveCharted (bool): When true, existing charted KPIs are retained
            and only non-charted KPIs are regenerated. Defaults to false.
        user: UserContext injected after credit validation.

    Returns:
        ORJSONResponse: Generated metadata and insights or error message.
    """
    await asyncio.to_thread(requireProjectOwner, projectId, user)
    try:
        jsonData = managementService.generateInsightsForProject(projectId=projectId, preserveCharted=preserveCharted, userId=user.userId)
        response = {"status": "SUCCESS"}
        response.update(jsonData)
        return ORJSONResponse(status_code = 200, content = response)
    except CustomException as e:
        raiseHttpException(e)
    
@router.get("/getInsights/{projectId}")
async def getInsights(projectId: str, user: UserContext = Depends(requireProductAccess)):
    """
    Retrieve insights and their status for a given project.

    Args:
        projectId (str): The ID of the project.
        user: UserContext injected after session verification.

    Returns:
        ORJSONResponse: Project metadata or error message.
    """
    await asyncio.to_thread(requireProjectOwner, projectId, user)
    try:
        newJson = managementService.getInsights(projectId = projectId)
        return ORJSONResponse(status_code = 200, content = newJson) 
    except CustomException as e:
        raiseHttpException(e)

@router.get("/getMetadata/{projectId}")
async def getMetadata(projectId: str, user: UserContext = Depends(requireProductAccess)):
    """
    Retrieve metadata for a given project.

    Args:
        projectId (str): The ID of the project.
        user: UserContext injected after session verification.

    Returns:
        ORJSONResponse: Project metadata or error message.
    """
    await asyncio.to_thread(requireProjectOwner, projectId, user)
    try:
        newJson = managementService.getMetadata(projectId = projectId)
        return ORJSONResponse(status_code = 200, content = newJson) 
    except CustomException as e:
        raiseHttpException(e)

@router.put("/editMetadata")
async def editMetadata(modifiedMetadata: EditMetadata, user: UserContext = Depends(requireProductAccess)):
    """
    Edit the metadata for a project.

    Args:
        modifiedMetadata (EditMetadata): The modified metadata details.
        user: UserContext injected after session verification.

    Returns:
        ORJSONResponse: Updated metadata or error message.
    """
    await asyncio.to_thread(requireProjectOwner, modifiedMetadata.projectId, user)
    try:
        jsonData = managementService.editMetadata(modifiedMetadata = modifiedMetadata)
        return ORJSONResponse(status_code = 200, content = {"status": "SUCCESS", "metadata": jsonData})
    except CustomException as e:
        raiseHttpException(e)

@router.patch("/toggleTableActive/{projectId}/{tableName}")
async def toggleTableActive(projectId: str, tableName: str, user: UserContext = Depends(requireProductAccess)):
    """
    Toggle the isActive flag for a table in a project's metadata.

    Status Codes:
        200 - Success. Returns the table name and its new isActive state.
        401 - Please login to toggle table active state.
        404 - Table not found in metadata.
        500 - Failed to update table active state.
    """
    await asyncio.to_thread(requireProjectOwner, projectId, user)
    try:
        result = managementService.toggleTableActive(projectId=projectId, tableName=tableName)
        return ORJSONResponse(status_code=200, content={"status": "SUCCESS", **result})
    except CustomException as e:
        raiseHttpException(e)

@router.delete("/deleteProject")
async def deleteProject(projectId: str, user: UserContext = Depends(requireProductAccess)):
    """
    Delete a project by its ID.

    Args:
        projectId (str): The ID of the project to delete.
        user: UserContext injected after session verification.

    Returns:
        ORJSONResponse: Success or error message.
    """
    await asyncio.to_thread(requireProjectOwner, projectId, user)
    try:
        managementService.deleteProject(projectId = projectId)
        return ORJSONResponse(status_code = 200, content = {"status": "SUCCESS", "message": "Project deleted successfully"})
    except CustomException as e:
        raiseHttpException(e)

@router.patch("/renameProject")
async def renameProject(renameDetails: RenameProject, user: UserContext = Depends(requireProductAccess)):
    """
    Rename an existing project.

    Status Codes:
        200 - Success
        401 - Please login to rename the project.
        404 - Project not found.
        409 - A project with this name already exists in the workspace.
        422 - Invalid project name.
        500 - Failed to rename project. Try again later.
    """
    await asyncio.to_thread(requireProjectOwner, renameDetails.projectId, user)
    try:
        managementService.renameProject(renameDetails=renameDetails, token=user.token)
        return ORJSONResponse(
            status_code=200,
            content={
                "status": "SUCCESS",
                "message": "Project renamed successfully"
            }
        )
    except CustomException as e:
        raiseHttpException(e)
    
@router.get("/listTriggers/{projectId}")
async def listTriggers(projectId: str, user: UserContext = Depends(requireProductAccess)):
    """
    List all triggers for a given project.

    Args:
        projectId (str): The ID of the project.
        user: UserContext injected after session verification.

    Returns:
        ORJSONResponse: List of triggers or error message.
    """
    await asyncio.to_thread(requireProjectOwner, projectId, user)
    try:
        triggers = managementService.listTriggers(projectId = projectId)
        return ORJSONResponse(status_code = 200, content = {"status": "SUCCESS", "triggers": triggers})  
    except CustomException as e:
        raiseHttpException(e)
    
@router.get("/listTriggersUnderUserId")
async def listTriggers(token = Depends(requireProductAccessToken)):
    """
    List all triggers assigned to the current user.

    Args:
        token: Authorization token dependency.

    Returns:
        ORJSONResponse: List of triggers or error message.
    """
    try:
        allTriggers = managementService.listTriggersUnderUserId(token = token)
        return ORJSONResponse(status_code = 200, content = {"status": "SUCCESS", "triggersAssignedToUser": allTriggers})   
    except CustomException as e:
        raiseHttpException(e)
    
@router.post("/generateReport/{projectId}")
async def generateReport(projectId: str, user: UserContext = Depends(requireTrialOrAbove)):
    """
    Generate a report for a given project.

    Args:
        projectId (str): The ID of the project.
        user: UserContext injected after the subscription check.

    Returns:
        ORJSONResponse: Generated report HTML content or error message.
    """
    await asyncio.to_thread(requireProjectOwner, projectId, user)
    try:
        reports = managementService.generateReport(projectId = projectId)
        return ORJSONResponse(status_code = 200, content = {"status": "SUCCESS", "reportHtmlContent": reports})
    except CustomException as e:
        raiseHttpException(e)
    
@router.get("/getReport/{projectId}/{tableName}")
async def getReport(projectId: str, tableName: str, user: UserContext = Depends(requireProductAccess)):
    """
    Retrieve a report for a specific table in a project.

    Args:
        projectId (str): The ID of the project.
        tableName (str): The name of the table.
        user: UserContext injected after session verification.

    Returns:
        HTMLResponse: HTML content of the report or error message.
    """
    await asyncio.to_thread(requireProjectOwner, projectId, user)
    try:
        htmlContent = managementService.getReport(projectId = projectId, tableName = tableName)
        return HTMLResponse(status_code = 200, content = htmlContent)  
    except CustomException as e:
        raiseHttpException(e)
    
@router.get("/getUserProfile")
def getUserProfile(token = Depends(verifyToken)):
    """
    Retrieve user profile details.

    Status Codes:
        200 - Success
        401 - Please login to view profile.
        404 - User profile not found.
        500 - Failed to retrieve user profile. Try again later.
    """
    try:
        userProfile = managementService.getUserProfile(token=token)
        return ORJSONResponse(
            status_code=200,
            content={
                "status": "SUCCESS",
                "profile": userProfile
            }
        )
    except CustomException as e:
        raiseHttpException(e)

@router.put("/editUserProfile")
async def editUserProfile(
    request: Request,
    userName: str | None = Form(default=None),
    company: str | None = Form(default=None),
    position: str | None = Form(default=None),
    bio: str | None = Form(default=None),
    token = Depends(verifyToken)
):
    """
    Update user profile details.

    Accepts form data with optional file upload for profile image.
    Profile image is stored in Supabase 'userProfileImages' bucket.

    Status Codes:
        200 - Success
        401 - Please login to edit profile.
        404 - User profile not found.
        500 - Failed to update user profile. Try again later.
    """
    try:
        form = await request.form()
        profileImageForm = form.get("profileImage")
        revertToDefault = False
        imageBytes = None
        imageFilename = None

        if profileImageForm == "":
            revertToDefault = True
        elif isinstance(profileImageForm, StarletteUploadFile):
            imageBytes = await profileImageForm.read()
            imageFilename = profileImageForm.filename
            if not imageBytes or not imageFilename:
                revertToDefault = True
        elif profileImageForm is not None:
            raise CustomException(
                ValueError("Invalid profileImage form value"),
                statusCode=422,
                uiMessage="profileImage must be a file upload or an empty string."
            )

        updatedProfile = managementService.editUserProfile(
            userName=userName,
            company=company,
            position=position,
            bio=bio,
            profileImage=imageBytes,
            profileImageFilename=imageFilename,
            token=token,
            revertToDefault=revertToDefault
        )
        return ORJSONResponse(
            status_code=200,
            content={
                "status": "SUCCESS",
                "message": "Profile updated successfully",
                "profile": updatedProfile
            }
        )
    except CustomException as e:
        raiseHttpException(e)

@router.get("/viewTable/{projectId}/{tableName}")
async def viewTable(
    projectId: str,
    tableName: str,
    page: int = Query(default=1, ge=1),
    pageSize: int = Query(default=100, ge=1, le=500),
    user: UserContext = Depends(requireProductAccess),
):
    """
    Read a parquet table from Supabase storage and return paginated rows.

    Only the footer and the row groups holding the page are fetched, so a
    10M-row table costs one row group, not the whole file.
    """
    await asyncio.to_thread(requireProjectOwner, projectId, user)
    try:
        def _read_page() -> dict:
            frame, totalRows = readTablePage(projectId, tableName, offset=(page - 1) * pageSize, limit=pageSize)
            rows = frame.to_dicts()
            from utils.initMethods import serializer
            import json as _json
            rows = _json.loads(_json.dumps(rows, default=serializer))
            return {
                "rows": rows,
                "pagination": {
                    "page": page,
                    "pageSize": pageSize,
                    "totalRows": totalRows,
                    "totalPages": (totalRows + pageSize - 1) // pageSize,
                },
            }

        result = await asyncio.to_thread(_read_page)
        return ORJSONResponse(status_code=200, content={"status": "SUCCESS", **result})
    except CustomException as e:
        raiseHttpException(e)
    except Exception as e:
        exc = CustomException(e, statusCode=500, uiMessage=f"Failed to read table: {e}")
        raiseHttpException(exc)
