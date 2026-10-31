"""
API router for AI-powered data transformations.
"""

__version__ = "1.0.0"
__author__ = "Rauhan Ahmed Siddiqui"
__all__ = ["router"]


from api.models import (
    CreateTransformationRequest,
    SendMessageRequest,
    RollbackRequest,
    RenameTransformationRequest,
)
from api.services.transformationService import transformationService
from utils.exceptionHandler import CustomException, raiseHttpException
import asyncio
from fastapi.responses import ORJSONResponse, StreamingResponse
from fastapi import APIRouter, Depends
from api.commons import verifyUser, requireActiveSubscription, requireCredits, requireProjectOwner, UserContext


router = APIRouter()
"""
Router for transformation-related endpoints.
"""


@router.post("")
async def createTransformation(
    projectId: str,
    request: CreateTransformationRequest,
    user: UserContext = Depends(requireActiveSubscription),
):
    """
    Create a new transformation workspace for a project.
    """
    await asyncio.to_thread(requireProjectOwner, projectId, user)
    try:
        result = await transformationService.createTransformation(
            projectId=projectId,
            name=request.transformation_name,
            description=request.description,
        )
        return ORJSONResponse(status_code=200, content=result)
    except CustomException as e:
        raiseHttpException(e)


@router.get("")
async def listTransformations(projectId: str, user: UserContext = Depends(verifyUser)):
    """
    List all transformations for a project.
    """
    await asyncio.to_thread(requireProjectOwner, projectId, user)
    try:
        result = await transformationService.listTransformations(projectId=projectId)
        return ORJSONResponse(status_code=200, content={"data": result})
    except CustomException as e:
        raiseHttpException(e)


@router.get("/{transformation_id}/messages")
async def getTransformationMessages(
    transformation_id: str,
    projectId: str,
    user: UserContext = Depends(verifyUser),
):
    """
    Return persisted chat history for a transformation.
    """
    await asyncio.to_thread(requireProjectOwner, projectId, user)
    try:
        result = await transformationService.getMessages(
            projectId=projectId,
            transformationId=transformation_id,
        )
        return ORJSONResponse(status_code=200, content={"messages": result})
    except CustomException as e:
        raiseHttpException(e)


@router.post("/{transformation_id}/messages")
async def sendTransformationMessage(
    transformation_id: str,
    projectId: str,
    request: SendMessageRequest,
    user: UserContext = Depends(requireCredits("transformation_message")),
):
    """
    Send a user message and stream the assistant response over SSE.
    """
    await asyncio.to_thread(requireProjectOwner, projectId, user)
    return StreamingResponse(
        transformationService.sendMessageStream(
            projectId=projectId,
            transformationId=transformation_id,
            content=request.content,
            userId=user.userId,
        ),
        media_type="text/event-stream",
    )


@router.post("/{transformation_id}/messages/{message_id}")
async def approveTransformationMessage(
    transformation_id: str,
    message_id: str,
    projectId: str,
    user: UserContext = Depends(requireActiveSubscription),
):
    """
    Approve a Mermaid artifact and return a transformed table preview.
    """
    await asyncio.to_thread(requireProjectOwner, projectId, user)
    try:
        result = await transformationService.approveMessage(
            projectId=projectId,
            transformationId=transformation_id,
            messageId=message_id,
        )
        return ORJSONResponse(status_code=200, content=result)
    except CustomException as e:
        raiseHttpException(e)


@router.post("/{transformation_id}/messages/{message_id}/apply")
async def applyTransformationMessage(
    transformation_id: str,
    message_id: str,
    projectId: str,
    user: UserContext = Depends(requireActiveSubscription),
):
    """
    Persist an approved transformation output as a project table.
    """
    await asyncio.to_thread(requireProjectOwner, projectId, user)
    try:
        result = await transformationService.applyTransformation(
            projectId=projectId,
            transformationId=transformation_id,
            messageId=message_id,
            userId=user.userId,
        )
        return ORJSONResponse(status_code=200, content=result)
    except CustomException as e:
        raiseHttpException(e)


@router.post("/{transformation_id}/rollback")
async def rollbackTransformation(
    transformation_id: str,
    projectId: str,
    request: RollbackRequest,
    user: UserContext = Depends(requireActiveSubscription),
):
    """
    Rollback workspace state and messages to a specific message ID.
    """
    await asyncio.to_thread(requireProjectOwner, projectId, user)
    try:
        result = await transformationService.rollbackTransformation(
            projectId=projectId,
            transformationId=transformation_id,
            messageId=request.messageId,
            userId=user.userId,
        )
        return ORJSONResponse(status_code=200, content=result)
    except CustomException as e:
        raiseHttpException(e)


@router.patch("/{transformation_id}/rename")
async def renameTransformation(
    transformation_id: str,
    projectId: str,
    request: RenameTransformationRequest,
    user: UserContext = Depends(verifyUser),
):
    """
    Rename an existing transformation workspace.
    """
    await asyncio.to_thread(requireProjectOwner, projectId, user)
    try:
        result = await transformationService.renameTransformation(
            projectId=projectId,
            transformationId=transformation_id,
            newName=request.newTransformationName,
        )
        return ORJSONResponse(status_code=200, content=result)
    except CustomException as e:
        raiseHttpException(e)


@router.delete("/{transformation_id}")
async def deleteTransformation(
    transformation_id: str,
    projectId: str,
    user: UserContext = Depends(verifyUser),
):
    """
    Delete a transformation workspace and its associated parquet file if it exists.
    """
    await asyncio.to_thread(requireProjectOwner, projectId, user)
    try:
        result = await transformationService.deleteTransformation(
            projectId=projectId,
            transformationId=transformation_id,
            userId=user.userId,
        )
        return ORJSONResponse(status_code=200, content=result)
    except CustomException as e:
        raiseHttpException(e)
