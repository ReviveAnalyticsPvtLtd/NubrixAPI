"""
API router for utility operations.

This module provides endpoints for speech transcription, hybrid image-to-insights generation,
dashboard insight retrieval and lifecycle management, sending forecasts, temporary functions,
and websocket task status updates.
"""

__version__ = "1.0.0"
__author__ = "Rauhan Ahmed Siddiqui"
__all__ = ["router"]


from utils.exceptionHandler import CustomException, raiseHttpException
from fastapi import (
    status,
    APIRouter,
    Depends,
    WebSocket,
    WebSocketDisconnect,
)
from api.models import SpeechToTextModel, ImageToInsightsModel, UpdateDashboardInsightStatus
from fastapi.security import HTTPAuthorizationCredentials
from api.services.utilityService import utilityService
from nubrix.triggers.celery import celeryApp
from fastapi.responses import ORJSONResponse
from api.commons import verifyToken, verifyUser, requireCredits, requireProjectOwner, UserContext
import asyncio

router = APIRouter()
"""
Router for utility-related endpoints.
"""


def _websocketTokenIsValid(credentials: HTTPAuthorizationCredentials) -> bool:
    try:
        return bool(verifyToken(token=credentials))
    except Exception:
        return False

@router.post("/getSpeechTranscript")
async def getSpeechTranscript(speechToText: SpeechToTextModel, user: UserContext = Depends(requireCredits("speech_to_text"))):
    """
    Get the transcript of a speech audio file.

    Args:
        speechToText (SpeechToTextModel): The speech-to-text model input.
        user: UserContext injected after credit validation.

    Returns:
        ORJSONResponse: Transcription text or error message.
    """
    try:
        transcriptText = utilityService.getSpeechTranscript(speechToText=speechToText, userId=user.userId)
        return ORJSONResponse(status_code = 200, content = {"transcriptionText": transcriptText})
    except CustomException as e:
        raiseHttpException(e)
    
@router.post("/getInsightsFromImage")
async def getInsightsFromImage(imageToInsights: ImageToInsightsModel, user: UserContext = Depends(requireCredits("image_to_insights"))):
    """
    Extract structured, evidence-backed business insights from a dashboard page's
    underlying data using the data + statistics + domain + LLM pipeline.

    Args:
        imageToInsights (ImageToInsightsModel): Input model containing the project,
            page, and refresh flag.
        user: UserContext injected after credit validation.

    Returns:
        ORJSONResponse: Structured JSON with diagnostic_insights, prescriptive_actions,
            and missing_data.
    """
    await asyncio.to_thread(requireProjectOwner, imageToInsights.projectId, user)
    try:
        insights = utilityService.getInsightsFromImage(imageToInsights=imageToInsights, userId=user.userId)
        return ORJSONResponse(status_code=200, content=insights)
    except CustomException as e:
        raiseHttpException(e)

@router.get("/getDashboardInsights/{projectId}")
async def getDashboardInsights(projectId: str, user: UserContext = Depends(verifyUser)):
    """
    Retrieve all persisted dashboard insights for a project.

    Args:
        projectId (str): The project identifier.
        user: UserContext injected after session verification.

    Returns:
        ORJSONResponse: List of insight records with status lifecycle.
    """
    await asyncio.to_thread(requireProjectOwner, projectId, user)
    try:
        records = utilityService.getDashboardInsights(projectId=projectId)
        return ORJSONResponse(status_code=200, content={"insights": records})
    except CustomException as e:
        raiseHttpException(e)

@router.patch("/updateDashboardInsightStatus")
async def updateDashboardInsightStatus(body: UpdateDashboardInsightStatus, user: UserContext = Depends(verifyUser)):
    """
    Update the lifecycle status of a persisted dashboard insight.

    Args:
        body (UpdateDashboardInsightStatus): Model containing projectId, insightId, and new status.
        user: UserContext injected after session verification.

    Returns:
        ORJSONResponse: The updated insight record.
    """
    await asyncio.to_thread(requireProjectOwner, body.projectId, user)
    try:
        record = utilityService.updateDashboardInsightStatus(
            projectId=body.projectId,
            insightId=body.insightId,
            status=body.status,
        )
        return ORJSONResponse(status_code=200, content={"insight": record})
    except CustomException as e:
        raiseHttpException(e)

@router.get("/sendForecasts")
async def sendForecasts(token = Depends(verifyToken)):
    """
    Send forecasts using the utility service.

    Args:
        token: Authorization token dependency.

    Returns:
        ORJSONResponse: Task ID, trigger name, and task status or error message.
    """
    try:
        r = utilityService.sendForecasts()
        return ORJSONResponse(status_code = 200, content = {"taskId": r.task_id, "triggerName": "forecast", "taskStatus": r.status})
    except CustomException as e:
        raiseHttpException(e)

@router.websocket("/ws/getTaskStatus")
async def getTaskStatus(websocket: WebSocket):
    """
    WebSocket endpoint to get the status of a background task.

    Args:
        websocket (WebSocket): The WebSocket connection instance.

    Returns:
        None. Sends task status updates over the WebSocket connection.
    """
    await websocket.accept()
    token = websocket.query_params.get("token")
    if not token:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    credentials = HTTPAuthorizationCredentials(scheme = "Bearer", credentials = token)
    if not _websocketTokenIsValid(credentials):
        await websocket.close(code = status.WS_1008_POLICY_VIOLATION)
        return
    await websocket.send_json({"status": "RUNNING"})
    try:
        taskId = websocket.query_params.get("taskId")
        r = celeryApp.AsyncResult(taskId)
        while not r.ready():
            await asyncio.sleep(5)
            if not _websocketTokenIsValid(credentials):
                await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
                return
        if not _websocketTokenIsValid(credentials):
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return
        await websocket.send_json({
            "taskId": r.task_id,
            "status": r.status,
            "taskResponseCode": r.get()
        })
        await websocket.close()
    except WebSocketDisconnect:
        pass
    except Exception as e:
        await websocket.send_json({"status": "ERROR", "errorDetail": str(e)})
        await websocket.close()
