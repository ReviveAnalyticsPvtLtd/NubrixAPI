"""
transformationService.py

Service layer for AI-powered data transformations.
"""

__version__ = "1.0.0"
__author__ = "Platform Engineering"
__all__ = ["TransformationService", "transformationService"]


from api.services.credits.creditTrackingCallback import CreditTrackingCallback
from utils.exceptionHandler import CustomException
from utils.initMethods import forget_table
from api.models import TransformationAgentResponse
from api.commons import client, tableFileState, updateProjectModifiedAt
from utils.projectStorage import authHeaders, objectUrl, readJson
from utils.logger import logger
from typing import TYPE_CHECKING
import redis
import httpx
import orjson
import json
import os

if TYPE_CHECKING:
    from nubrix.components.transformationAgent import TransformationAgent
    from nubrix.components.transformationExecutor import TransformationExecutor





class TransformationService:
    """
    Encapsulates transformation persistence, agent calls, preview, and apply.
    """
    def __init__(self):
        """Initialize service dependencies."""
        self.supabase = client
        self._agent: "TransformationAgent | None" = None
        self._executor: "TransformationExecutor | None" = None

    @property
    def agent(self) -> "TransformationAgent":
        """Create the LLM agent lazily to avoid import-time model initialization."""
        if self._agent is None:
            from nubrix.components.transformationAgent import TransformationAgent
            self._agent = TransformationAgent()
        return self._agent

    @property
    def executor(self) -> "TransformationExecutor":
        """Create the transformation executor lazily."""
        if self._executor is None:
            from nubrix.components.transformationExecutor import TransformationExecutor
            self._executor = TransformationExecutor()
        return self._executor

    def _redis_client(self) -> redis.Redis:
        """Create a Redis client using environment credentials."""
        return redis.Redis(
            host=os.environ["REDIS_HOST"],
            port=int(os.environ["REDIS_PORT"]),
            password=os.environ["REDIS_PASSWORD"],
        )

    def _sse(self, event: str, payload: dict) -> str:
        """Format an SSE event."""
        return f"event: {event}\ndata: {orjson.dumps(payload).decode('utf-8')}\n\n"

    def _metadata_url(self, projectId: str) -> str:
        """Build the project metadata URL."""
        return objectUrl(projectId, "metadata.json")

    def _preview_cache_key(self, projectId: str, transformationId: str, messageId: str, tableName: str) -> str:
        """Return the Redis preview cache key."""
        return f"{projectId}::transformation_preview::{transformationId}::{messageId}::{tableName}"

    def _sanitizeTableName(self, name: str) -> str:
        """Sanitize a string into a valid table name."""
        import re
        name = re.sub(r"[^\w-]", "_", name)
        name = re.sub(r"_+", "_", name)
        name = re.sub(r"-+", "-", name)
        name = name.strip("_-")
        if not name:
            return "table"
        if not name[0].isalpha():
            return "table_" + name
        return name

    async def _get_metadata(self, projectId: str) -> dict:
        """Fetch project metadata with a short Redis cache.
        Returns only active tables (isActive != False)."""
        cacheKey = f"{projectId}::metadata.json"
        redisClient = None
        try:
            redisClient = self._redis_client()
            cached = redisClient.get(cacheKey)
            if cached:
                fullMetadata = json.loads(cached)
                return self._filterActive(fullMetadata)
        except Exception as e:
            logger.warning(f"Metadata cache read failed for project {projectId}: {e}")

        async with httpx.AsyncClient(timeout=20) as httpClient:
            response = await httpClient.get(self._metadata_url(projectId), headers=authHeaders())
            response.raise_for_status()
            metadata = response.json()

        if redisClient is not None:
            try:
                redisClient.set(cacheKey, json.dumps(metadata), ex=300)
            except Exception as e:
                logger.warning(f"Metadata cache write failed for project {projectId}: {e}")
        return self._filterActive(metadata)

    @staticmethod
    def _filterActive(metadata: dict) -> dict:
        """Return only active table entries. Delegates to managementService."""
        from api.services.managementService import managementService
        return managementService.filterActiveTables(metadata)

    def _ensure_transformation(self, projectId: str, transformationId: str) -> dict:
        """Fetch a transformation and verify project ownership."""
        response = (
            self.supabase.table("transformations")
            .select("*")
            .eq("project_id", projectId)
            .eq("transformation_id", transformationId)
            .limit(1)
            .execute()
        )
        if not response.data:
            raise ValueError("Transformation not found.")
        return response.data[0]

    def _validate_transformation_table_name(
        self,
        projectId: str,
        targetTableName: str,
        currentTransformationId: str | None = None,
        ignoreOwnClaim: bool = False,
    ) -> None:
        """
        Validate that targetTableName can be created or updated by currentTransformationId.
        
        Rules:
        - Cannot overwrite a table owned by ANOTHER transformation in this project.
        - Cannot overwrite a base dataset table in metadata.json (uploaded/imported tables).
        - Cannot overwrite a table file with no key yet, such as an upload still being registered.
        - CAN overwrite its own output table (rewriting versions within the same transformation chat).

        ignoreOwnClaim: the caller has just claimed the name, so its own artifact proves nothing.
        """
        sanitizedTarget = self._sanitizeTableName(targetTableName)

        # 1. Fetch all transformations in the project excluding current
        query = self.supabase.table("transformations").select("*").eq("project_id", projectId)
        if currentTransformationId:
            query = query.neq("transformation_id", currentTransformationId)
        otherTransformations = query.execute().data or []

        # Build set of table names owned by OTHER transformations
        otherTransformationTables = set()
        for t in otherTransformations:
            tname = t.get("transformation_name")
            if tname:
                otherTransformationTables.add(self._sanitizeTableName(tname))
            latest = t.get("latest_approved_artifact")
            if isinstance(latest, dict) and latest.get("table_name"):
                otherTransformationTables.add(self._sanitizeTableName(latest.get("table_name")))

        if sanitizedTarget in otherTransformationTables:
            raise CustomException(
                ValueError(f"A transformation named '{sanitizedTarget}' already exists in this project."),
                statusCode=409,
                uiMessage=f"A transformation named '{sanitizedTarget}' already exists in this project. Please use a different name."
            )

        # 2. Find tables written by ALL transformations in this project (including current). Only the
        # artifact proves that: a draft's own name can belong to an upload. Other rows' names were refused above.
        allTransformations = self.supabase.table("transformations").select("*").eq("project_id", projectId).execute().data or []
        allTransformationTables = set()
        for t in allTransformations:
            latest = t.get("latest_approved_artifact")
            if isinstance(latest, dict) and latest.get("table_name"):
                allTransformationTables.add(self._sanitizeTableName(latest.get("table_name")))
        if ignoreOwnClaim:
            # Other rows' names were refused above, so this drops only the caller's own claim.
            allTransformationTables.discard(sanitizedTarget)

        # 3. Check metadata.json to block overwriting base dataset tables
        try:
            files = self.supabase.storage.from_("AnalyticsHub").list(projectId, {"search": "metadata.json"})
            filenames = [x.get("name") for x in files]
            if "metadata.json" in filenames:
                metadata = readJson(projectId, "metadata.json")
                # If target table exists in metadata.json AND is not owned by ANY transformation workspace, it's a base dataset table!
                if sanitizedTarget in metadata and sanitizedTarget not in allTransformationTables:
                    # A key alone proves nothing: earlier transformation deletes left theirs behind.
                    if self._table_file_exists(projectId, sanitizedTarget):
                        raise CustomException(
                            ValueError(f"Table '{sanitizedTarget}' is a base dataset and cannot be overwritten."),
                            statusCode=409,
                            uiMessage=f"A base dataset named '{sanitizedTarget}' already exists in this project and cannot be overwritten by a transformation."
                        )
            # A load writes its file before its key, and keeps no key if registering it fails.
            if sanitizedTarget not in allTransformationTables and self._table_file_exists(projectId, sanitizedTarget):
                raise CustomException(
                    ValueError(f"Table '{sanitizedTarget}' is already stored in this project."),
                    statusCode=409,
                    uiMessage=f"A table named '{sanitizedTarget}' is already stored in this project. Please use a different name."
                )
        except CustomException:
            raise
        except Exception as e:
            # Refuse rather than skip: a base table this check misses is overwritten on apply.
            exception = CustomException(e, statusCode=503, uiMessage="We could not check this table name right now. Please try again.")
            logger.error(exception)
            raise exception

    def _table_file_exists(self, projectId: str, tableName: str) -> bool:
        """Check for the exact table file. A search also returns longer names and other casings."""
        tableFile = f"{tableName}.parquet"
        tableFiles = self.supabase.storage.from_("AnalyticsHub").list(projectId, {"search": tableFile})
        return tableFile in [x.get("name") for x in tableFiles]

    def _remove_table_from_metadata(self, projectId: str, tableName: str) -> None:
        """Drop a deleted table's key from metadata.json. generateMetadata only ever adds keys."""
        bucket = self.supabase.storage.from_("AnalyticsHub")
        files = bucket.list(projectId, {"search": "metadata.json"})
        if "metadata.json" not in [x.get("name") for x in files]:
            return
        metadataPath = f"{projectId}/metadata.json"
        metadata = json.loads(bucket.download(metadataPath))
        if tableName not in metadata:
            return
        metadata.pop(tableName)
        bucket.upload(
            path=metadataPath,
            file=json.dumps(metadata, indent=4).encode("utf-8"),
            file_options={"upsert": "true"},
        )

    def _remove_owned_table(self, projectId: str, tableName: str) -> None:
        """Remove a table this transformation produced: the file, then its key, then the caches. Each step is safe to repeat."""
        self.supabase.storage.from_("AnalyticsHub").remove([f"{projectId}/{tableName}.parquet"])
        self._remove_table_from_metadata(projectId, tableName)
        forget_table(projectId, tableName)

    def _claim_table_name(self, projectId: str, transformationId: str, tableName: str) -> tuple[bool, dict | None]:
        """Check the name and claim it for this row, straight before its table is written.

        A load releases every claim on a name it accepts, so a claim gone by the second check means
        a load is writing that table. The second check also catches a file written since the first.
        The claim is written only over the artifact read here, so a claim a load released since is not put back.

        Returns whether the row already owned the name, and its artifact before the claim.
        """
        row = self._ensure_transformation(projectId=projectId, transformationId=transformationId)
        priorArtifact = row.get("latest_approved_artifact") if isinstance(row.get("latest_approved_artifact"), dict) else None
        priorTable = (priorArtifact or {}).get("table_name")
        ownedBefore = priorTable == tableName
        self._validate_transformation_table_name(projectId=projectId, targetTableName=tableName, currentTransformationId=transformationId)

        claimed = stillClaimed = False
        try:
            claim = self.supabase.table("transformations").update({
                "latest_approved_artifact": {
                    **{key: value for key, value in (priorArtifact or {}).items() if key != "table_file"},
                    "table_name": tableName,
                },
            }).eq("transformation_id", transformationId)
            if priorTable:
                claim = claim.eq("latest_approved_artifact->>table_name", priorTable)
            else:
                claim = claim.is_("latest_approved_artifact->>table_name", "null")
            claimed = bool(claim.execute().data)
            stillClaimed = claimed and bool(
                self.supabase.table("transformations").select("transformation_id")
                .eq("transformation_id", transformationId)
                .eq("latest_approved_artifact->>table_name", tableName)
                .execute().data
            )
            if stillClaimed:
                self._validate_transformation_table_name(
                    projectId=projectId,
                    targetTableName=tableName,
                    currentTransformationId=transformationId,
                    ignoreOwnClaim=not ownedBefore,
                )
        except Exception as e:
            if claimed:
                self._restore_artifact(transformationId, tableName, priorArtifact)
            if isinstance(e, CustomException):
                raise
            exception = CustomException(e, statusCode=503, uiMessage="We could not check this table name right now. Please try again.")
            logger.error(exception)
            raise exception
        if not stillClaimed:
            raise CustomException(
                ValueError(f"Table '{tableName}' was taken by a load before it was written."),
                statusCode=409,
                uiMessage=f"A table named '{tableName}' was just added to this project. Please use a different name."
            )
        return ownedBefore, priorArtifact

    def _write_claimed_table(self, projectId: str, transformationId: str, tableName: str, parquetBytes: bytes) -> None:
        """Claim the name, then write the table. Only a row that already owned the name may replace a file,
        so an upload that lands between the last check and a first write is kept, and this answers 409.
        """
        ownedBefore, priorArtifact = self._claim_table_name(projectId, transformationId, tableName)
        try:
            self.executor.apply(projectId=projectId, parquetBytes=parquetBytes, tableName=tableName, overwrite=ownedBefore)
        except CustomException as e:
            if e.statusCode == 409:
                self._restore_artifact(transformationId, tableName, priorArtifact)
            raise

    def _restore_artifact(self, transformationId: str, tableName: str, priorArtifact: dict | None) -> None:
        """Undo a claim this row made, unless something has replaced it since."""
        try:
            self.supabase.table("transformations").update({"latest_approved_artifact": priorArtifact}).eq("transformation_id", transformationId).eq("latest_approved_artifact->>table_name", tableName).execute()
        except Exception as e:
            logger.error(f"Could not undo the claim on '{tableName}' for transformation {transformationId}: {e}")

    def _record_applied_artifact(self, projectId: str, transformationId: str, latestArtifact: dict) -> None:
        """Point the row at the table it just wrote, unless a load has taken the name meanwhile.

        It also records the file storage now holds, so a failed load gives the name back only while that file is
        unchanged. A load that wrote first releases the name after its write, which clears this record with it.
        """
        tableName = latestArtifact["table_name"]
        try:
            fileState = tableFileState(self.supabase, projectId, tableName)
        except Exception as e:
            fileState = None
            logger.warning(f"Could not record the file for '{tableName}'; a failed load will not give the name back: {e}")
        if fileState:
            latestArtifact = {**latestArtifact, "table_file": fileState}
        try:
            updated = self.supabase.table("transformations").update({
                "latest_approved_artifact": latestArtifact,
            }).eq("transformation_id", transformationId).eq("latest_approved_artifact->>table_name", tableName).execute().data
        except Exception as e:
            # The table is written and still claimed by this row, so applying again is safe.
            exception = CustomException(e, statusCode=503, uiMessage="We could not finish saving this table. Please try again.")
            logger.error(exception)
            raise exception
        if not updated:
            raise CustomException(
                ValueError(f"Table '{tableName}' was taken by a load while it was written."),
                statusCode=409,
                uiMessage=f"A table named '{tableName}' was just added to this project. Please use a different name."
            )

    def _get_messages_from_db(self, projectId: str, transformationId: str) -> list[dict]:
        """Load messages list directly from transformations table."""
        row = self._ensure_transformation(projectId=projectId, transformationId=transformationId)
        messages = row.get("messages")
        if messages is None:
            return []
        return messages

    def _save_messages_to_db(self, projectId: str, transformationId: str, messages: list[dict]) -> None:
        """Save messages list back to transformations table."""
        self.supabase.table("transformations").update({
            "messages": messages
        }).eq("project_id", projectId).eq("transformation_id", transformationId).execute()

    def _get_message(self, projectId: str, transformationId: str, messageId: str) -> dict:
        """Fetch a transformation message from the messages array."""
        messages = self._get_messages_from_db(projectId=projectId, transformationId=transformationId)
        for msg in messages:
            if msg.get("message_id") == messageId:
                return msg
        raise ValueError("Transformation message not found.")

    def _buildMessagePayload(
        self,
        projectId: str,
        transformationId: str,
        messageId: str | None,
        userMessageId: str | None = None,
    ) -> dict:
        """
        Build the full persisted message payload (matching TransformationMessage)
        plus python_code, for emission in the SSE 'done' event.
        """
        payload = {
            "message_id": None,
            "transformation_id": transformationId,
            "role": "assistant",
            "content": None,
            "artifact": None,
            "python_code": None,
            "created_at": None,
            "user_message": None,
        }
        if messageId:
            try:
                row = self._get_message(projectId=projectId, transformationId=transformationId, messageId=messageId)
                payload.update({
                    "message_id": row.get("message_id"),
                    "role": row.get("role"),
                    "content": row.get("content"),
                    "artifact": row.get("artifact"),
                    "python_code": row.get("python_code"),
                    "created_at": row.get("created_at"),
                })
            except Exception as e:
                logger.error(f"Failed to load message {messageId} for SSE payload: {e}")
                payload["message_id"] = messageId

        if userMessageId:
            try:
                userRow = self._get_message(projectId=projectId, transformationId=transformationId, messageId=userMessageId)
                payload["user_message"] = {
                    "message_id": userRow.get("message_id"),
                    "transformation_id": userRow.get("transformation_id"),
                    "role": userRow.get("role"),
                    "content": userRow.get("content"),
                    "created_at": userRow.get("created_at"),
                }
            except Exception as e:
                logger.error(f"Failed to load user message {userMessageId} for SSE payload: {e}")

        return payload

    async def createTransformation(self, projectId: str, name: str, description: str | None) -> dict:
        """Insert into transformations table and return the row."""
        import re
        try:
            if not name or not name.strip():
                raise ValueError("Transformation name cannot be empty.")
            if len(name) > 63:
                raise ValueError("Transformation name cannot exceed 63 characters.")
            if not any(c.isalpha() for c in name):
                raise ValueError("Transformation name must contain at least one letter.")
            if not re.match(r"^[A-Za-z0-9\s._-]+$", name):
                raise ValueError("Transformation name can only contain letters, numbers, spaces, periods, hyphens, and underscores.")

            sanitizedName = self._sanitizeTableName(name)
            self._validate_transformation_table_name(projectId=projectId, targetTableName=sanitizedName, currentTransformationId=None)

            response = self.supabase.table("transformations").insert({
                "project_id": projectId,
                "transformation_name": name,
                "description": description,
                "messages": []
            }).execute()
            updateProjectModifiedAt(projectId)
            return response.data[0]
        except CustomException:
            raise
        except Exception as e:
            exception = CustomException(e, statusCode=400, uiMessage=str(e))
            logger.error(exception)
            raise exception

    async def listTransformations(self, projectId: str) -> list[dict]:
        """Return all transformations for a project."""
        try:
            response = (
                self.supabase.table("transformations")
                .select("*")
                .eq("project_id", projectId)
                .order("created_at", desc=True)
                .execute()
            )
            return response.data or []
        except Exception as e:
            exception = CustomException(e, statusCode=400, uiMessage="Failed to list transformations.")
            logger.error(exception)
            raise exception

    async def getMessages(self, projectId: str, transformationId: str) -> list[dict]:
        """Fetch messages ordered by creation time."""
        try:
            messages = self._get_messages_from_db(projectId=projectId, transformationId=transformationId)
            result = []
            for msg in messages:
                result.append({
                    "message_id": msg.get("message_id"),
                    "transformation_id": msg.get("transformation_id"),
                    "role": msg.get("role"),
                    "content": msg.get("content"),
                    "artifact": msg.get("artifact"),
                    "python_code": msg.get("python_code"),
                    "created_at": msg.get("created_at"),
                })
            return result
        except Exception as e:
            exception = CustomException(e, statusCode=400, uiMessage=str(e))
            logger.error(exception)
            raise exception

    async def sendMessageStream(self, projectId: str, transformationId: str, content: str, userId: str | None = None):
        """
        Persist a user message, stream the agent response, and persist the assistant artifact.
        """
        import uuid
        from datetime import datetime, timezone
        userMessageId = str(uuid.uuid4())
        try:
            self._ensure_transformation(projectId=projectId, transformationId=transformationId)
            messages = self._get_messages_from_db(projectId=projectId, transformationId=transformationId)
            user_msg = {
                "message_id": userMessageId,
                "transformation_id": transformationId,
                "role": "user",
                "content": content,
                "artifact": None,
                "python_code": None,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            messages.append(user_msg)
            self._save_messages_to_db(projectId=projectId, transformationId=transformationId, messages=messages)

            metadata = await self._get_metadata(projectId=projectId)
            structuredResponse = None

            agentCallbacks = []
            if userId:
                agentCallbacks.append(CreditTrackingCallback(userId=userId, operationType="transformation_message"))

            async for event in self.agent.astream(
                projectId=projectId,
                transformationId=transformationId,
                userMessage=content,
                metadata=metadata,
                chatHistory=messages[:-1],
                callbacks=agentCallbacks if agentCallbacks else None,
                userId=userId,
            ):
                if event.get("type") == "status":
                    yield self._sse("status", {"message": event.get("message", "")})
                elif event.get("type") == "token":
                    yield self._sse("token", {"delta": event.get("delta", "")})
                elif event.get("type") == "done":
                    structuredResponse = event.get("structured")

            assistantMessageId = str(uuid.uuid4())
            if not isinstance(structuredResponse, TransformationAgentResponse):
                fallback = "I could not generate a structured transformation. Please refine the request."
                assistant_msg = {
                    "message_id": assistantMessageId,
                    "transformation_id": transformationId,
                    "role": "assistant",
                    "content": fallback,
                    "artifact": None,
                    "python_code": None,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
                messages.append(assistant_msg)
                self._save_messages_to_db(projectId=projectId, transformationId=transformationId, messages=messages)
                yield self._sse("done", self._buildMessagePayload(projectId, transformationId, assistantMessageId, userMessageId))
                return

            if not structuredResponse.pythonCode or not structuredResponse.mermaidCode:
                artifact = None
                python_code = None
            else:
                artifact = {
                    "type": "mermaid",
                    "code": structuredResponse.mermaidCode,
                    "is_approved": False,
                }
                python_code = structuredResponse.pythonCode

            assistant_msg = {
                "message_id": assistantMessageId,
                "transformation_id": transformationId,
                "role": "assistant",
                "content": structuredResponse.userFacingResponse,
                "artifact": artifact,
                "python_code": python_code,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            messages.append(assistant_msg)
            self._save_messages_to_db(projectId=projectId, transformationId=transformationId, messages=messages)
            yield self._sse("done", self._buildMessagePayload(projectId, transformationId, assistantMessageId, userMessageId))
        except Exception as e:
            logger.error(f"Transformation stream failed after user message {userMessageId}: {e}")
            fallback = "I could not generate a transformation because the request failed. Please try again."
            assistantMessageId = str(uuid.uuid4())
            try:
                messages = self._get_messages_from_db(projectId=projectId, transformationId=transformationId)
                assistant_msg = {
                    "message_id": assistantMessageId,
                    "transformation_id": transformationId,
                    "role": "assistant",
                    "content": fallback,
                    "artifact": None,
                    "python_code": None,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
                messages.append(assistant_msg)
                self._save_messages_to_db(projectId=projectId, transformationId=transformationId, messages=messages)
            except Exception:
                assistantMessageId = None
            yield self._sse("token", {"delta": ""})
            yield self._sse("done", self._buildMessagePayload(projectId, transformationId, assistantMessageId, userMessageId))

    async def approveMessage(
        self,
        projectId: str,
        transformationId: str,
        messageId: str,
    ) -> dict:
        """Execute an assistant message artifact and return a 100-row preview."""
        try:
            row = self._ensure_transformation(projectId=projectId, transformationId=transformationId)
            transformationName = row.get("transformation_name")
            newTransformedTableName = self._sanitizeTableName(transformationName if transformationName else "table")

            self._validate_transformation_table_name(
                projectId=projectId,
                targetTableName=newTransformedTableName,
                currentTransformationId=transformationId,
            )

            messages = row.get("messages") or []
            target_msg = None
            for msg in messages:
                if msg.get("message_id") == messageId:
                    target_msg = msg
                    break
            if not target_msg:
                raise ValueError("Transformation message not found.")

            pythonCode = target_msg.get("python_code")
            artifact = target_msg.get("artifact")
            if not pythonCode or not artifact:
                raise ValueError("Only assistant messages with transformation artifacts can be approved.")

            previewRows, parquetBytes = self.executor.executeAndPreview(
                projectId=projectId,
                pythonCode=pythonCode,
                tableName=newTransformedTableName,
            )
            self._redis_client().set(
                name=self._preview_cache_key(projectId, transformationId, messageId, newTransformedTableName),
                value=parquetBytes,
                ex=900,
            )
            artifact["is_approved"] = False
            target_msg["artifact"] = artifact
            self._save_messages_to_db(projectId=projectId, transformationId=transformationId, messages=messages)
            return {"data": previewRows}
        except CustomException:
            raise
        except Exception as e:
            exception = CustomException(e, statusCode=400, uiMessage=str(e))
            logger.error(exception)
            raise exception

    async def applyTransformation(
        self,
        projectId: str,
        transformationId: str,
        messageId: str,
        userId: str | None = None,
    ) -> dict:
        """Persist an approved transformation output and update metadata."""
        try:
            row = self._ensure_transformation(projectId=projectId, transformationId=transformationId)
            transformationName = row.get("transformation_name")
            newTransformedTableName = self._sanitizeTableName(transformationName if transformationName else "table")

            messages = row.get("messages") or []
            target_msg = None
            for msg in messages:
                if msg.get("message_id") == messageId:
                    target_msg = msg
                    break
            if not target_msg:
                raise ValueError("Transformation message not found.")

            artifact = target_msg.get("artifact")
            if not artifact:
                raise ValueError("Only messages with transformation artifacts can be applied.")

            redisClient = self._redis_client()
            cacheKey = self._preview_cache_key(projectId, transformationId, messageId, newTransformedTableName)
            parquetBytes = redisClient.get(cacheKey)
            if parquetBytes is None:
                pythonCode = target_msg.get("python_code")
                if not pythonCode:
                    raise ValueError("No python code found in message to execute.")
                logger.info(f"Preview cache expired for message {messageId}. Re-executing code on the fly.")
                _, parquetBytes = self.executor.executeAndPreview(
                    projectId=projectId,
                    pythonCode=pythonCode,
                    tableName=newTransformedTableName,
                )

            # Checked here, not at approve: an upload can take the name while the code runs.
            self._write_claimed_table(projectId, transformationId, newTransformedTableName, parquetBytes)
            self._record_applied_artifact(projectId, transformationId, {
                "mermaid_code": artifact.get("code"),
                "message_id": messageId,
                "table_name": newTransformedTableName,
            })

            artifact["is_approved"] = True
            target_msg["artifact"] = artifact
            
            self._save_messages_to_db(projectId=projectId, transformationId=transformationId, messages=messages)
            
            try:
                redisClient.delete(cacheKey)
            except Exception:
                pass

            # Regenerate project metadata so the new table is registered in metadata.json
            try:
                from api.services.managementService import managementService
                logger.info(f"Regenerating metadata for project {projectId} after applying transformation...")
                managementService.generateMetadata(projectId=projectId, userId=userId)
            except Exception as e:
                logger.warning(f"Failed to generate metadata for project {projectId} after apply: {e}")

            updateProjectModifiedAt(projectId)
            return {
                "status": "200",
                "message": f"Transformation applied successfully. Table '{newTransformedTableName}' is now available.",
            }
        except CustomException:
            raise
        except Exception as e:
            exception = CustomException(e, statusCode=400, uiMessage=str(e))
            logger.error(exception)
            raise exception

    async def rollbackTransformation(
        self,
        projectId: str,
        transformationId: str,
        messageId: str,
        userId: str | None = None,
    ) -> dict:
        """Rollback workspace state and message history to a specific message ID (time-travel)."""
        try:
            row = self._ensure_transformation(projectId=projectId, transformationId=transformationId)
            messages = self._get_messages_from_db(projectId=projectId, transformationId=transformationId)
            
            # Find the target message in history
            targetIdx = -1
            for idx, msg in enumerate(messages):
                if msg.get("message_id") == messageId:
                    targetIdx = idx
                    break
            if targetIdx == -1:
                raise ValueError("Transformation message not found.")
            
            # Save the target user message content for the text field
            targetMsg = messages[targetIdx]
            restoredInput = targetMsg.get("content") or ""
            
            # Truncate messages BEFORE the target (exclusive — target goes to the text field)
            truncatedMessages = messages[:targetIdx]
            
            # Determine table name from transformation workspace name
            transformationName = row.get("transformation_name")
            newTransformedTableName = self._sanitizeTableName(transformationName if transformationName else "table")
            
            # Time-travel: find the last approved checkpoint in truncated history
            # Use artifact.is_approved (persists across applies) instead of is_applied (gets cleared)
            lastApprovedMsg = None
            for m in reversed(truncatedMessages):
                if m.get("role") == "assistant" and (m.get("artifact") or {}).get("is_approved") and m.get("python_code"):
                    lastApprovedMsg = m
                    break
            
            if lastApprovedMsg:
                # Re-execute the last approved transformation to restore storage state
                pythonCode = lastApprovedMsg.get("python_code")
                logger.info(f"Rolling back storage to last approved checkpoint: message {lastApprovedMsg.get('message_id')}")
                _, parquetBytes = self.executor.executeAndPreview(
                    projectId=projectId,
                    pythonCode=pythonCode,
                    tableName=newTransformedTableName,
                )
                # The table may have been deleted and its name taken since that checkpoint was applied.
                self._write_claimed_table(projectId, transformationId, newTransformedTableName, parquetBytes)
                
                # Update latest approved artifact to point to this checkpoint
                self._record_applied_artifact(projectId, transformationId, {
                    "mermaid_code": (lastApprovedMsg.get("artifact") or {}).get("code"),
                    "message_id": lastApprovedMsg.get("message_id"),
                    "table_name": newTransformedTableName,
                })
                
                # Keep the approved checkpoint's status intact
                approvedArtifact = lastApprovedMsg.get("artifact") or {}
                approvedArtifact["is_approved"] = True
                lastApprovedMsg["artifact"] = approvedArtifact
            else:
                # No approved transformation exists in truncated history — clean slate.
                # Only the table the artifact names is this row's; a file under its name without one is an upload.
                latestApproved = row.get("latest_approved_artifact")
                appliedTable = latestApproved.get("table_name") if isinstance(latestApproved, dict) else None
                if appliedTable:
                    logger.info(f"Rolling back to pre-transformation state. Deleting table '{appliedTable}'.")
                    try:
                        self._remove_owned_table(projectId, appliedTable)
                    except Exception as e:
                        exception = CustomException(e, statusCode=503, uiMessage="We could not roll back this transformation right now. Please try again.")
                        logger.error(exception)
                        raise exception

                self.supabase.table("transformations").update({
                    "latest_approved_artifact": None,
                }).eq("transformation_id", transformationId).execute()
            
            # Save truncated list to DB
            self._save_messages_to_db(projectId=projectId, transformationId=transformationId, messages=truncatedMessages)

            # Invalidate agent's cached summary for this thread so it regenerates on next request
            try:
                self.agent.invalidateThreadCache(transformationId)
            except Exception:
                pass
            
            # Clear Redis preview caches for the target message and all discarded messages
            try:
                redisClient = self._redis_client()
                cacheKey = self._preview_cache_key(projectId, transformationId, messageId, newTransformedTableName)
                redisClient.delete(cacheKey)
                
                discardedMessages = messages[targetIdx:]
                for dmsg in discardedMessages:
                    dmsgId = dmsg.get("message_id")
                    if dmsgId:
                        dcacheKey = self._preview_cache_key(projectId, transformationId, dmsgId, newTransformedTableName)
                        redisClient.delete(dcacheKey)
            except Exception as e:
                logger.warning(f"Could not clear preview caches during rollback: {e}")
                
            # Regenerate metadata.json
            try:
                from api.services.managementService import managementService
                managementService.generateMetadata(projectId=projectId, userId=userId)
            except Exception as e:
                logger.warning(f"Failed to generate metadata after rollback: {e}")
                
            updateProjectModifiedAt(projectId)
            
            # Return updated messages + the restored input for the text field
            updatedMessages = await self.getMessages(projectId=projectId, transformationId=transformationId)
            return {
                "messages": updatedMessages,
                "restoredInput": restoredInput,
            }
        except CustomException:
            raise
        except Exception as e:
            exception = CustomException(e, statusCode=400, uiMessage=str(e))
            logger.error(exception)
            raise exception



    async def renameTransformation(
        self,
        projectId: str,
        transformationId: str,
        newName: str,
    ) -> dict:
        """Rename an existing transformation workspace."""
        import re
        try:
            if not newName or not newName.strip():
                raise ValueError("Transformation name cannot be empty.")
            if len(newName) > 63:
                raise ValueError("Transformation name cannot exceed 63 characters.")
            if not any(c.isalpha() for c in newName):
                raise ValueError("Transformation name must contain at least one letter.")
            if not re.match(r"^[A-Za-z0-9\s._-]+$", newName):
                raise ValueError("Transformation name can only contain letters, numbers, spaces, periods, hyphens, and underscores.")

            # Check if transformation exists
            row = self._ensure_transformation(projectId=projectId, transformationId=transformationId)
            sanitizedNewName = self._sanitizeTableName(newName)

            self._validate_transformation_table_name(
                projectId=projectId,
                targetTableName=sanitizedNewName,
                currentTransformationId=transformationId,
            )

            # Update transformation name
            self.supabase.table("transformations").update({
                "transformation_name": newName
            }).eq("project_id", projectId).eq("transformation_id", transformationId).execute()

            updateProjectModifiedAt(projectId)
            return {"status": "200", "message": "Transformation renamed successfully."}
        except CustomException:
            raise
        except Exception as e:
            exception = CustomException(e, statusCode=400, uiMessage=str(e))
            logger.error(exception)
            raise exception

    async def deleteTransformation(self, projectId: str, transformationId: str, userId: str | None = None) -> dict:
        """Delete a transformation workspace and its associated parquet file if it exists."""
        try:
            # 1. Fetch transformation to find the associated table name
            row = self._ensure_transformation(projectId=projectId, transformationId=transformationId)
            appliedTable = None
            latest_approved = row.get("latest_approved_artifact")
            if latest_approved and isinstance(latest_approved, dict):
                appliedTable = latest_approved.get("table_name")
            transformationName = row.get("transformation_name")
            tableName = self._sanitizeTableName(transformationName if transformationName else "table")

            # 2. Remove the table from storage, metadata.json and the caches. Each step is safe to
            # repeat, and the row goes last: a failure here keeps it, so the delete can be retried.
            try:
                if appliedTable:
                    self._remove_owned_table(projectId, appliedTable)
                # With no artifact the row owns no file: one under its name is an upload, so leave its key.
                elif not self._table_file_exists(projectId, tableName):
                    self._remove_table_from_metadata(projectId, tableName)
                    forget_table(projectId, tableName)
            except Exception as e:
                exception = CustomException(e, statusCode=503, uiMessage="We could not delete this transformation right now. Please try again.")
                logger.error(exception)
                raise exception

            # 3. Delete the transformation row from database
            try:
                self.supabase.table("transformations").delete().eq("project_id", projectId).eq("transformation_id", transformationId).execute()
            except Exception as e:
                exception = CustomException(e, statusCode=503, uiMessage="We could not delete this transformation right now. Please try again.")
                logger.error(exception)
                raise exception

            updateProjectModifiedAt(projectId)
            return {"status": "200", "message": "Transformation deleted successfully."}
        except CustomException:
            raise
        except Exception as e:
            exception = CustomException(e, statusCode=400, uiMessage=str(e))
            logger.error(exception)
            raise exception


transformationService = TransformationService()
