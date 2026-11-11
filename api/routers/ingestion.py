"""Project-owner control API for the opt-in REST ingestion pilot."""

import json
import os
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from psycopg2 import Error as DatabaseError

from api.commons import UserContext, client, requireProductAccess, requireProjectOwner, tableFileState
from api.services.ingestion import repository
from api.services.ingestion.credentials import protect
from api.services.ingestion.restPull import IngestionError, extract, validateConfig


def _enabled():
    if os.environ.get("API_INGESTION_ENABLED") != "1":
        raise HTTPException(404, detail="API ingestion is unavailable")


router = APIRouter(dependencies=[Depends(_enabled)])
_CADENCES = (0, 15, 60, 1440)
_CONFIG_FIELDS = ("endpoint", "authHeader", "recordsPath", "nextCursorPath", "cursorQueryParam", "columns", "allowEmpty")


async def _body(request: Request) -> dict:
    # Do not let the shared validation handler echo rejected credential input.
    content = bytearray()
    async for chunk in request.stream():
        if len(content) + len(chunk) > 64 * 1024:
            raise HTTPException(413, detail="API ingestion configuration is too large")
        content.extend(chunk)
    try:
        data = json.loads(content)
    except (ValueError, RecursionError):
        data = None
    if not isinstance(data, dict):
        raise HTTPException(422, detail="Expected a JSON object")
    return data


def _config(body, *, preview=False):
    config = {key: body[key] for key in _CONFIG_FIELDS if key in body}
    secret = _secret(body.get("secret"))
    name = body.get("displayName", "")
    cadence = body.get("cadenceMinutes", 60)
    if type(cadence) is not int or cadence not in _CADENCES:
        raise HTTPException(422, detail="Invalid refresh cadence")
    if not preview and (not isinstance(name, str) or not name.strip() or len(name) > 120 or not config.get("columns")):
        raise HTTPException(422, detail="A dataset name and at least one mapped column are required")
    if "columns" in config and isinstance(config["columns"], list):
        config["columns"] = [
            {key: column[key] for key in ("name", "path", "type", "nullable") if key in column}
            if isinstance(column, dict) else column for column in config["columns"]
        ]
    try:
        validateConfig(config)
    except IngestionError as error:
        raise HTTPException(422, detail=error.code) from None
    config.update(displayName=name.strip() if isinstance(name, str) else "", cadenceMinutes=cadence)
    return config, secret


def _secret(value):
    if (not isinstance(value, str) or not value or len(value) > 4096
            or any(not 33 <= ord(char) <= 126 for char in value)):
        raise HTTPException(422, detail="A valid API credential is required")
    return value


def _encrypt(sourceId, secret):
    try:
        return protect(sourceId, secret)
    except ValueError:
        raise HTTPException(503, detail="API credential storage is unavailable") from None


def _query(operation, *args, **kwargs):
    try:
        return operation(*args, **kwargs)
    except repository.Conflict as error:
        raise HTTPException(409, detail=str(error)) from None
    except (DatabaseError, KeyError):
        raise HTTPException(503, detail="API ingestion is unavailable") from None


def _sourceView(source):
    if source["needs_attention"]:
        health = "needs_attention"
    elif not source["enabled"]:
        health = "paused"
    elif source["has_active_run"]:
        health = "syncing"
    else:
        health = "ready"
    return {
        **{key: source["config"][key] for key in _CONFIG_FIELDS if key in source["config"]},
        "id": source["id"], "datasetId": source["dataset_id"], "tableName": source["table_name"],
        "displayName": source["display_name"], "revision": source["revision"], "enabled": source["enabled"],
        "cadenceMinutes": source["cadence_minutes"], "health": health, "nextDueAt": source["next_due_at"],
        "lastSuccessAt": source["last_success_at"], "currentVersionId": source["current_version_id"],
        "lastRunStatus": source.get("last_run_status"), "lastErrorCode": source.get("last_error_code"),
    }


def _runView(run):
    return {
        "id": run["id"], "status": run["status"], "receivedRows": run["received_rows"],
        "errorCode": run["error_code"], "createdAt": run["created_at"],
        "finishedAt": run["finished_at"], "versionId": run["version_id"],
    }


def _owned(row, user):
    if not row:
        raise HTTPException(404, detail="API ingestion resource not found")
    requireProjectOwner(row["project_id"], user)
    return row


@router.post("/projects/{projectId}/sources/test")
def testSource(projectId: str, body: dict = Depends(_body), user: UserContext = Depends(requireProductAccess)):
    requireProjectOwner(projectId, user)
    config, secret = _config(body, preview=True)
    try:
        return {"sample": extract(config, secret, preview=True)}
    except IngestionError as error:
        raise HTTPException(422, detail=error.code) from None


@router.get("/projects/{projectId}/sources")
def listSources(projectId: str, user: UserContext = Depends(requireProductAccess)):
    requireProjectOwner(projectId, user)
    return {"sources": [_sourceView(source) for source in _query(repository.listSources, projectId)]}


@router.post("/projects/{projectId}/sources", status_code=201)
def createSource(projectId: str, body: dict = Depends(_body), user: UserContext = Depends(requireProductAccess)):
    requireProjectOwner(projectId, user)
    config, secret = _config(body)
    sourceId, datasetId = uuid4(), uuid4()
    try:
        if tableFileState(client, projectId, f"api__{datasetId.hex}"):
            raise HTTPException(409, detail="Dataset alias is already in use")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(503, detail="Dataset storage is unavailable") from None
    credential = _encrypt(str(sourceId), secret)
    return _sourceView(_query(repository.createSource, projectId, config, credential,
                              sourceId=str(sourceId), datasetId=str(datasetId)))


@router.patch("/sources/{sourceId}")
def updateSource(sourceId: UUID, body: dict = Depends(_body), user: UserContext = Depends(requireProductAccess)):
    sourceId = str(sourceId)
    _owned(_query(repository.getSource, sourceId), user)
    revision, enabled, cadence = body.get("revision"), body.get("enabled"), body.get("cadenceMinutes")
    if (type(revision) is not int or revision < 1 or (enabled is not None and type(enabled) is not bool)
            or (cadence is not None and (type(cadence) is not int or cadence not in _CADENCES))):
        raise HTTPException(422, detail="Invalid API source update")
    credential = _encrypt(sourceId, _secret(body["secret"])) if "secret" in body else None
    return _sourceView(_query(repository.updateSource, sourceId, revision, enabled=enabled,
                              cadenceMinutes=cadence, credential=credential))


@router.post("/sources/{sourceId}/runs", status_code=202)
def requestRun(sourceId: UUID, user: UserContext = Depends(requireProductAccess)):
    sourceId = str(sourceId)
    _owned(_query(repository.getSource, sourceId), user)
    return _runView(_query(repository.requestRun, sourceId))


@router.get("/sources/{sourceId}/runs")
def listRuns(sourceId: UUID, user: UserContext = Depends(requireProductAccess)):
    sourceId = str(sourceId)
    _owned(_query(repository.getSource, sourceId), user)
    return {"runs": [_runView(run) for run in _query(repository.listRuns, sourceId)]}


@router.get("/runs/{runId}")
def getRun(runId: UUID, user: UserContext = Depends(requireProductAccess)):
    return _runView(_owned(_query(repository.getRun, str(runId)), user))
