"""
UtilityService module provides utility functions such as speech-to-text transcription,
data-driven dashboard insight generation, insight persistence, and sending forecasts
for NubrixAI.
"""

from nubrix.components.insightContextBuilder import InsightContextBuilder
from nubrix.components.dashboardPayloadBuilder import DashboardPayloadBuilder
from nubrix.components.imageToInsights import ImageToInsights
from nubrix.components.signalEngine import SignalEngine
from nubrix.components.speechToText import SpeechToText
from api.services.credits.creditTrackingCallback import CreditTrackingCallback
from api.models import SpeechToTextModel, ImageToInsightsModel
from utils.exceptionHandler import CustomException
from nubrix.triggers.celery import celeryApp
from celery.result import AsyncResult
from utils.logger import logger
from api.commons import client
from datetime import datetime, timezone
import pandas as pd
import hashlib
import json
import uuid
import time
import os
import io
from urllib.request import urlopen

VALID_INSIGHT_STATUSES = {"new", "accepted", "rejected", "implemented"}

class UtilityService:
    """
    Service class providing utility operations such as speech transcription, hybrid insight
    generation with persistence, and sending forecasts.
    """
    def __init__(self) -> None:
        """Initializes the UtilityService and sets up required modules."""
        logger.info("Initializing Utility Service.")
        self.imageToInsightsModule = ImageToInsights()
        self.insightContextBuilder = InsightContextBuilder()
        self.signalEngine = SignalEngine()
        self.payloadBuilder = DashboardPayloadBuilder(
            fullRowLimit=self.imageToInsightsModule.fullRowLimit,
            compactRowLimit=self.imageToInsightsModule.compactRowLimit,
            tokenBudget=self.imageToInsightsModule.payloadTokenBudget,
        )
        self.speechToTextModule = SpeechToText()
        self.client = client
    
    # ~50 estimated tokens per second of audio for billing purposes (covers
    # input processing overhead on top of output token generation).
    _STT_TOKENS_PER_SECOND = 50
    _STT_MIN_TOKENS = 2000

    def getSpeechTranscript(self, speechToText: SpeechToTextModel, userId: str | None = None, operationId: str | None = None) -> str:
        """
        Converts base64-encoded audio to text using the SpeechToText module.

        Args:
            speechToText (SpeechToTextModel): Model containing the base64-encoded audio string.
            userId (str | None): The user ID for credit deduction and Langfuse tracing.
        Returns:
            str: The transcribed text from the audio.
        Raises:
            CustomException: If transcription fails.
        """
        try:
            context=None
            executionId='speech:'+str(uuid.uuid4())
            if userId:
                from api.services.credits.creditService import creditService
                context=creditService.admitCreditOperation(userId,'speech_to_text',
                    (operationId+':'+executionId) if operationId else executionId)
            result = self.speechToTextModule.getTranscript(b64String = speechToText.b64String)
            transcriptText = result["text"]
            audioDuration = result.get("duration")

            if userId:
                if audioDuration is not None and audioDuration > 0:
                    estimatedTokens = max(self._STT_MIN_TOKENS, int(audioDuration * self._STT_TOKENS_PER_SECOND))
                else:
                    estimatedTokens = self._STT_MIN_TOKENS
                creditService.settleCreditOperation(context,estimatedTokens,executionId)
                try:
                    from utils.langfuseClient import logManualGeneration
                    from nubrix.utils import getConfig
                    sttModel = getConfig(os.path.join(os.getcwd(), "config.ini")).get("SPEECHTOTEXT", "model", fallback="whisper-large-v3-turbo")
                    logManualGeneration(
                        userId=userId,
                        name="speech-to-text",
                        model=sttModel,
                        inputSummary={"type": "audio/webm", "encoding": "base64", "duration_seconds": audioDuration},
                        output=transcriptText,
                        tags=["utility", "speech_to_text"],
                    )
                except Exception as e:
                    logger.warning(f"STT Langfuse trace failed: {e}")
            return transcriptText
        except Exception as e:
            exception  = CustomException(e)
            logger.error(exception)
            raise exception
        
    def getInsightsFromImage(self, imageToInsights: ImageToInsightsModel, userId: str | None = None) -> dict:
        """
        Extracts structured, evidence-backed insights from a dashboard page's own data.

        Builds a serialized data payload from the page's widgets, statistics, schema,
        and domain profile, and sends that to the LLM. Results are cached per page
        against a hash of the payload, so the cache invalidates exactly when the
        underlying data changes.

        Args:
            imageToInsights (ImageToInsightsModel): Project, page, and refresh flag.
            userId (str | None): Caller, used for credit tracking and tracing.

        Returns:
            dict: Structured insights with diagnostic_insights, prescriptive_actions,
                missing_data, plus cache metadata.

        Raises:
            CustomException: If insight generation fails.
        """
        try:
            context = self.insightContextBuilder.buildContext(
                projectId=imageToInsights.projectId,
                pageId=imageToInsights.pageId,
            )
            context["statisticalSummary"] = self.signalEngine.buildStatisticalSummary(
                chartData=context.get("chartData", [])
            )
            payloadText = self.payloadBuilder.build(context)
            cacheKey = self._computeCacheKey(imageToInsights.pageId, payloadText)

            if not imageToInsights.refresh:
                cachedRecord = self._findCachedRecord(
                    records=self._loadDashboardInsightsFile(imageToInsights.projectId),
                    pageId=imageToInsights.pageId,
                    cacheKey=cacheKey,
                )
                if cachedRecord:
                    return self._formatDashboardInsightResponse(
                        record=cachedRecord,
                        source="cache",
                        cacheHit=True,
                    )

            context["projectId"] = imageToInsights.projectId
            if userId:
                context["userId"] = userId

            insightCallbacks = []
            if userId:
                insightCallbacks.append(CreditTrackingCallback(userId=userId, operationType="image_to_insights"))

            insights = self.imageToInsightsModule.getInsights(
                payloadText=payloadText,
                context=context,
                callbacks=insightCallbacks if insightCallbacks else None,
            )

            record = self._persistDashboardInsight(
                projectId=imageToInsights.projectId,
                pageId=imageToInsights.pageId,
                insights=insights,
                cacheKey=cacheKey,
            )

            return self._formatDashboardInsightResponse(
                record=record,
                source="generated",
                cacheHit=False,
            )
        except Exception as e:
            exception = CustomException(e)
            logger.error(exception)
            raise exception

    @staticmethod
    def _computeCacheKey(pageId: str, payloadText: str) -> str:
        """
        Derives the cache key for a page's insights from its serialized payload.

        Hashing the payload rather than timestamping means the cache invalidates
        precisely when the dashboard's data, filters, or widget set change.
        """
        digest = hashlib.sha256(f"{pageId}|{payloadText}".encode("utf-8"))
        return digest.hexdigest()

    def _findCachedRecord(self, records: list, pageId: str, cacheKey: str) -> dict | None:
        """
        Returns the stored insight for this page whose payload hash still matches.

        Records written before the content-hash cache (cacheVersion 1) carry no
        cacheKey and are never served. Records whose insights do not carry the
        documented schema are also skipped, so a page cached while the payload
        was malformed regenerates instead of serving the bad shape forever.
        """
        for record in reversed(records):
            if not isinstance(record, dict) or "insights" not in record:
                continue
            if record.get("pageId") != pageId:
                continue
            if not self._isValidInsightsPayload(record.get("insights")):
                continue
            if record.get("cacheKey") == cacheKey:
                return record
        return None

    @staticmethod
    def _isValidInsightsPayload(insights: object) -> bool:
        """
        Returns whether an insights payload carries the schema the client expects:
        diagnostic_insights, prescriptive_actions, and missing_data.
        """
        return isinstance(insights, dict) and all(
            key in insights
            for key in ("diagnostic_insights", "prescriptive_actions", "missing_data")
        )

    def _formatDashboardInsightResponse(self, record: dict, source: str, cacheHit: bool) -> dict:
        """
        Formats cached and generated dashboard insight records for the API response.
        """
        return {
            "insights": record.get("insights"),
            "source": source,
            "cacheHit": cacheHit,
            "insightId": record.get("id"),
            "generatedAt": record.get("generatedAt"),
        }

    def _persistDashboardInsight(self, projectId: str, pageId: str, insights: dict, cacheKey: str) -> dict:
        """
        Persists the generated insight to dashboardInsights.json, replacing only
        the record for this page and leaving other pages' records intact.

        Args:
            projectId (str): The project identifier.
            pageId (str): The dashboard page the insight was generated for.
            insights (dict): The structured insight payload.
            cacheKey (str): Hash of the payload this insight was generated from.

        Returns:
            dict: The persisted insight record.
        """
        try:
            record = {
                "id": str(uuid.uuid4()),
                "scope": "page",
                "pageId": pageId,
                "cacheKey": cacheKey,
                "generatedAt": datetime.now(timezone.utc).isoformat(),
                "insights": insights,
                "status": "new",
                "cacheVersion": 2,
            }

            existing = [
                item for item in self._loadDashboardInsightsFile(projectId)
                if isinstance(item, dict) and item.get("pageId") != pageId
            ]
            records = existing + [record]

            with io.BytesIO() as buffer:
                buffer.write(json.dumps(records, indent=4).encode("utf-8"))
                buffer.seek(0)
                self.client.storage.from_("AnalyticsHub").upload(
                    path=f"{projectId}/dashboardInsights.json",
                    file=buffer.getvalue(),
                    file_options={"upsert": "true"},
                )
            logger.info(f"Dashboard insight persisted for project {projectId}, page {pageId}.")
            return record
        except Exception as e:
            logger.warning(f"Failed to persist dashboard insight: {e}")
            raise

    def _loadDashboardInsightsFile(self, projectId: str) -> list:
        """
        Loads dashboardInsights.json from Supabase storage.

        Returns:
            list: Existing insight records, or empty list if file does not exist.
        """
        try:
            files = self.client.storage.from_("AnalyticsHub").list(path=projectId)
            if "dashboardInsights.json" not in [x.get("name") for x in files]:
                return []
            fileUrl = os.environ["FILE_URL"].format(projectId=projectId, fileName="dashboardInsights.json").replace(".parquet", "") + f"?cb={int(time.time())}"
            data = json.loads(urlopen(fileUrl).read())
            return data if isinstance(data, list) else []
        except Exception:
            return []

    def getDashboardInsights(self, projectId: str) -> list:
        """
        Retrieves all persisted dashboard insights for a project.

        Args:
            projectId (str): The project identifier.

        Returns:
            list: List of insight records.

        Raises:
            CustomException: If retrieval fails.
        """
        try:
            return self._loadDashboardInsightsFile(projectId)
        except Exception as e:
            exception = CustomException(e)
            logger.error(exception)
            raise exception

    def updateDashboardInsightStatus(self, projectId: str, insightId: str, status: str) -> dict:
        """
        Updates the lifecycle status of a persisted dashboard insight.

        Args:
            projectId (str): The project identifier.
            insightId (str): The unique insight record ID.
            status (str): New status — one of "new", "accepted", "rejected", "implemented".

        Returns:
            dict: The updated insight record.

        Raises:
            CustomException: If the update fails or the insight is not found.
        """
        try:
            if status not in VALID_INSIGHT_STATUSES:
                raise ValueError(f"Invalid status '{status}'. Must be one of {VALID_INSIGHT_STATUSES}.")

            records = self._loadDashboardInsightsFile(projectId)
            updatedRecord = None
            for record in records:
                if record.get("id") == insightId:
                    record["status"] = status
                    updatedRecord = record
                    break

            if updatedRecord is None:
                raise ValueError(f"Insight record '{insightId}' not found in project '{projectId}'.")

            with io.BytesIO() as buffer:
                buffer.write(json.dumps(records, indent=4).encode("utf-8"))
                buffer.seek(0)
                self.client.storage.from_("AnalyticsHub").upload(
                    path=f"{projectId}/dashboardInsights.json",
                    file=buffer.getvalue(),
                    file_options={"upsert": "true"},
                )

            logger.info(f"Dashboard insight {insightId} status updated to '{status}'.")
            return updatedRecord
        except Exception as e:
            exception = CustomException(e)
            logger.error(exception)
            raise exception
        
    def sendForecasts(self) -> AsyncResult:
        """
        Sends a task to generate forecasts asynchronously using Celery.

        Returns:
            AsyncResult: The result object for the Celery task.
        Raises:
            CustomException: If the task submission fails.
        """
        try:
            return celeryApp.send_task("NubrixAI.generateForecasts")
        except Exception as e:
            exception  = CustomException(e)
            logger.error(exception)
            raise exception

utilityService = UtilityService()
