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
    HTTPException,
    WebSocket,
    WebSocketDisconnect,
)
from api.models import SpeechToTextModel, ImageToInsightsModel, UpdateDashboardInsightStatus
from fastapi.security import HTTPAuthorizationCredentials
from api.services.utilityService import utilityService
from nubrix.triggers.celery import celeryApp
from api.commons import issueTaskId, sessionOwnsTask
from fastapi.responses import ORJSONResponse
from api.commons import (
    verifyToken,
    requireProductAccess,
    requireCredits,
    requireProjectOwner,
    requireTrialOrAbove,
    requireTriggerOwner,
    UserContext,
)
import asyncio
import time
from utils.logger import logger

router = APIRouter()
"""
Router for utility-related endpoints.
"""

FORECAST_TRIGGER = "forecast"


def _websocketTokenIsValid(credentials: HTTPAuthorizationCredentials) -> bool | None:
    """
    Check a socket's session. Returns None when the check itself failed, which
    is neither a valid session nor a refused one.
    """
    try:
        return bool(verifyToken(credentials))
    except HTTPException:
        return False
    except Exception as e:
        logger.warning("Task status session check failed: {}", e)
        return None


TASK_STATUS_POLL_SECONDS = 5
TASK_STATUS_MAX_WATCH_SECONDS = 30 * 60


async def _waitForClientToLeave(websocket: WebSocket) -> None:
    while (await websocket.receive())["type"] != "websocket.disconnect":
        pass


def _clientHasLeft(clientLeft: asyncio.Task) -> bool:
    """
    A watcher that failed says nothing about the client, so a result is still sent.
    """
    return clientLeft.done() and clientLeft.exception() is None


def _finalTaskStatus(task) -> dict:
    if task.status != "SUCCESS":
        return {"taskId": task.task_id, "status": "FAILURE", "taskResponseCode": None}
    responseCode = task.get()
    taskFailed = isinstance(responseCode, int) and responseCode >= 400
    return {
        "taskId": task.task_id,
        "status": "FAILURE" if taskFailed else "SUCCESS",
        "taskResponseCode": responseCode,
    }

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
async def getDashboardInsights(projectId: str, user: UserContext = Depends(requireProductAccess)):
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
async def updateDashboardInsightStatus(body: UpdateDashboardInsightStatus, user: UserContext = Depends(requireProductAccess)):
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
async def sendForecasts(user: UserContext = Depends(requireTrialOrAbove)):
    """
    Send forecasts using the utility service.

    Args:
        user: UserContext injected after the subscription check.

    Returns:
        ORJSONResponse: Task ID, trigger name, and task status or error message.
    """
    await asyncio.to_thread(requireTriggerOwner, FORECAST_TRIGGER, user)
    try:
        r = utilityService.sendForecasts(taskId=issueTaskId(user.userId))
        return ORJSONResponse(status_code = 200, content = {"taskId": r.task_id, "triggerName": FORECAST_TRIGGER, "taskStatus": r.status})
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
    if not await asyncio.to_thread(_websocketTokenIsValid, credentials):
        await websocket.close(code = status.WS_1008_POLICY_VIOLATION)
        return
    taskId = websocket.query_params.get("taskId")
    if not sessionOwnsTask(token, taskId):
        await websocket.close(code = status.WS_1008_POLICY_VIOLATION)
        return
    await websocket.send_json({"status": "RUNNING"})
    clientLeft = asyncio.create_task(_waitForClientToLeave(websocket))
    try:
        r = celeryApp.AsyncResult(taskId)
        watchEndsAt = time.monotonic() + TASK_STATUS_MAX_WATCH_SECONDS
        sessionValid = True
        taskReady = await asyncio.to_thread(r.ready)
        while not (taskReady and sessionValid):
            if time.monotonic() >= watchEndsAt:
                await websocket.send_json({"taskId": r.task_id, "status": "UNKNOWN", "taskResponseCode": None})
                await websocket.close()
                return
            await asyncio.wait({clientLeft}, timeout=TASK_STATUS_POLL_SECONDS)
            if clientLeft.done():
                return
            taskReady = await asyncio.to_thread(r.ready)
            sessionValid = await asyncio.to_thread(_websocketTokenIsValid, credentials)
            if _clientHasLeft(clientLeft):
                return
            if sessionValid is False:
                await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
                return
        finalStatus = await asyncio.to_thread(_finalTaskStatus, r)
        if _clientHasLeft(clientLeft):
            return
        await websocket.send_json(finalStatus)
        await websocket.close()
    except WebSocketDisconnect:
        pass
    except Exception as e:
        if _clientHasLeft(clientLeft):
            return
        logger.error("Task status watch failed: {}", e)
        await websocket.send_json({"status": "ERROR", "errorDetail": "Task status is unavailable."})
        await websocket.close()
    finally:
        clientLeft.cancel()
