"""Durable control state for REST pulls. Storage and source I/O occur outside transactions."""

import os
from contextlib import closing, contextmanager
from uuid import UUID, uuid4

import psycopg2
from psycopg2.extras import Json, RealDictCursor


ACTIVE = ("queued", "running", "retrying")
MAX_ATTEMPTS = 3
MAX_ACTIVE_RUNS = 2


class Conflict(Exception):
    pass


def connect():
    return psycopg2.connect(os.environ["DATABASE_URL"], connect_timeout=5,
                            application_name="nubrix-api-ingestion")


@contextmanager
def transaction():
    with closing(connect()) as connection, connection, connection.cursor(cursor_factory=RealDictCursor) as cursor:
        yield cursor


_SOURCE = """
select s.*, d.id as dataset_id, d.display_name, d.table_name, d.current_version_id,
       exists(select 1 from api_ingestion.runs r where r.dataset_id=d.id
              and r.status in ('queued','running','retrying')) as has_active_run,
       latest.status as last_run_status, latest.error_code as last_error_code
from api_ingestion.sources s join api_ingestion.datasets d on d.source_id=s.id
left join lateral (select status,error_code from api_ingestion.runs
                   where dataset_id=d.id order by created_at desc,id limit 1) latest on true
"""

_RUN = """
select r.*, d.project_id, d.source_id, d.table_name, d.current_version_id,
       v.id as version_id
from api_ingestion.runs r join api_ingestion.datasets d on d.id=r.dataset_id
left join api_ingestion.versions v on v.run_id=r.id
"""


def _source(cursor, sourceId, *, lock=False):
    query = _SOURCE + " where s.id=%s"
    if lock:
        query += " for update of s"
    cursor.execute(query, (sourceId,))
    return cursor.fetchone()


def getSource(sourceId):
    with transaction() as cursor:
        return _source(cursor, sourceId)


def listSources(projectId):
    with transaction() as cursor:
        cursor.execute(_SOURCE + " where s.project_id=%s order by d.display_name, d.id", (projectId,))
        return cursor.fetchall()


def createSource(projectId, config, credential, *, sourceId=None, datasetId=None):
    sourceId = sourceId or str(uuid4())
    datasetId = datasetId or str(uuid4())
    tableName = f"api__{datasetId.replace('-', '')}"
    cadence = config["cadenceMinutes"]
    with transaction() as cursor:
        cursor.execute("""
            insert into api_ingestion.sources (id,project_id,config,credential,cadence_minutes)
            values (%s,%s,%s,%s,%s)
        """, (sourceId, projectId, Json(config), credential, cadence))
        cursor.execute("""
            insert into api_ingestion.datasets (id,source_id,project_id,table_name,display_name,columns)
            values (%s,%s,%s,%s,%s,%s)
        """, (datasetId, sourceId, projectId, tableName, config["displayName"], Json(config["columns"])))
        return _source(cursor, sourceId)


def updateSource(sourceId, revision, *, enabled=None, cadenceMinutes=None, credential=None):
    with transaction() as cursor:
        source = _source(cursor, sourceId, lock=True)
        if not source or source["revision"] != revision:
            raise Conflict("SOURCE_REVISION_CONFLICT")
        cadence = source["cadence_minutes"] if cadenceMinutes is None else cadenceMinutes
        active = source["enabled"] if enabled is None else enabled
        needsAttention = source["needs_attention"] and credential is None
        cursor.execute("""
            update api_ingestion.sources
            set enabled=%s, cadence_minutes=%s, credential=coalesce(%s,credential),
                needs_attention=%s, revision=revision+1,
                next_due_at=case when %s and not %s and %s>0 and last_success_at is not null
                                 then now()+make_interval(mins=>%s) end
            where id=%s
        """, (active, cadence, credential, needsAttention, active, needsAttention, cadence, cadence, sourceId))
        cursor.execute("""
            update api_ingestion.runs set status='cancelled', finished_at=now(), lease_expires_at=null
            where dataset_id=%s and status in ('queued','running','retrying')
        """, (source["dataset_id"],))
        return _source(cursor, sourceId)


def getRun(runId):
    with transaction() as cursor:
        cursor.execute(_RUN + " where r.id=%s", (runId,))
        return cursor.fetchone()


def listRuns(sourceId):
    with transaction() as cursor:
        cursor.execute(_RUN + " where d.source_id=%s order by r.created_at desc limit 50", (sourceId,))
        return cursor.fetchall()


def _requestRun(cursor, source, scheduledAt=None):
    cursor.execute(_RUN + " where d.id=%s and r.status in ('queued','running','retrying')", (source["dataset_id"],))
    existing = cursor.fetchone()
    if existing:
        return existing
    runId = str(uuid4())
    cursor.execute("""
        insert into api_ingestion.runs (id,dataset_id,source_revision,scheduled_at,status)
        values (%s,%s,%s,%s,'queued')
    """, (runId, source["dataset_id"], source["revision"], scheduledAt))
    cursor.execute(_RUN + " where r.id=%s", (runId,))
    return cursor.fetchone()


def requestRun(sourceId):
    with transaction() as cursor:
        source = _source(cursor, sourceId, lock=True)
        if not source or not source["enabled"] or source["needs_attention"]:
            raise Conflict("SOURCE_PAUSED")
        return _requestRun(cursor, source)


def dueRunIds(limit=2):
    """Beat persists due slots before dispatching recoverable run IDs."""
    with transaction() as cursor:
        cursor.execute(_SOURCE + """
            where s.enabled and not s.needs_attention and s.next_due_at<=now()
            order by s.next_due_at limit %s for update of s skip locked
        """, (limit,))
        for source in cursor.fetchall():
            _requestRun(cursor, source, source["next_due_at"])
            cursor.execute("""update api_ingestion.sources
                set next_due_at=now()+make_interval(mins=>cadence_minutes) where id=%s""", (source["id"],))
        cursor.execute("""
            update api_ingestion.runs set status='failed', finished_at=now(), error_code='WORKER_TIMEOUT'
            where status='running' and attempt >= %s and lease_expires_at<=now()
        """, (MAX_ATTEMPTS,))
        cursor.execute("""
            with eligible as (
                select id from api_ingestion.runs
                where dispatch_after<=now() and (
                    (status in ('queued','retrying') and retry_at<=now())
                    or (status='running' and lease_expires_at<=now())
                ) order by created_at limit %s for update skip locked
            )
            update api_ingestion.runs r set dispatch_after=now()+interval '3 minutes'
            from eligible where r.id=eligible.id returning r.id
        """, (limit,))
        return [row["id"] for row in cursor.fetchall()]


def claimRun(runId):
    with transaction() as cursor:
        cursor.execute("select pg_try_advisory_xact_lock(hashtextextended('nubrix-api-ingestion',0)) as locked")
        if not cursor.fetchone()["locked"]:
            return None
        cursor.execute("""select count(*) as active from api_ingestion.runs
            where status='running' and lease_expires_at>now()""")
        if cursor.fetchone()["active"] >= MAX_ACTIVE_RUNS:
            return None
        cursor.execute(_RUN + " where r.id=%s", (runId,))
        run = cursor.fetchone()
        if not run:
            return None
        source = _source(cursor, run["source_id"], lock=True)
        if not source or not source["enabled"] or source["revision"] != run["source_revision"]:
            return None
        cursor.execute("""
            update api_ingestion.runs set status='running', attempt=attempt+1,
                lease_expires_at=now()+interval '6 minutes', error_code=null
            where id=%s and attempt<%s and ((status in ('queued','retrying') and retry_at<=now())
                or (status='running' and lease_expires_at<=now())) returning attempt
        """, (runId, MAX_ATTEMPTS))
        attempt = cursor.fetchone()
        if not attempt:
            return None
        return {**run, **attempt, "config": source["config"], "credential": source["credential"]}


def _lockLease(cursor, lease):
    source = _source(cursor, lease["source_id"], lock=True)
    if not source or not source["enabled"] or source["revision"] != lease["source_revision"]:
        return None
    cursor.execute("""
        select id from api_ingestion.runs where id=%s and status='running' and attempt=%s
            and lease_expires_at>clock_timestamp() for update
    """, (lease["id"], lease["attempt"]))
    return source if cursor.fetchone() else None


def finishAttempt(lease, errorCode, *, retryable=False, retryAfter=None, needsAttention=False):
    with transaction() as cursor:
        source = _lockLease(cursor, lease)
        if not source:
            return
        retry = retryable and lease["attempt"] < MAX_ATTEMPTS
        seconds = max(60 * (2 ** (lease["attempt"] - 1)), retryAfter or 0)
        cursor.execute("""
            update api_ingestion.runs set status=%s, error_code=%s, lease_expires_at=null,
                retry_at=now()+make_interval(secs=>%s), dispatch_after=now(),
                finished_at=case when not %s then now() end
            where id=%s
        """, ("retrying" if retry else "failed", errorCode, seconds, retry, lease["id"]))
        if needsAttention:
            cursor.execute("""update api_ingestion.sources set needs_attention=true,next_due_at=null
                where id=%s""", (lease["source_id"],))


def snapshotName(lease, versionId):
    return f"{lease['table_name']}--{UUID(versionId).hex}.snapshot"


@contextmanager
def projectStorageLock(projectId):
    """Serialize snapshot writes with a project's storage removal."""
    if os.environ.get("API_INGESTION_ENABLED") != "1":
        yield
        return
    with closing(connect()) as connection:
        connection.autocommit = True
        with connection.cursor() as cursor:
            cursor.execute("select pg_advisory_lock(hashtextextended(%s,0))",
                           (f"nubrix:ingestion:project:{projectId}",))
        yield


def fenceProjectSources(projectId):
    """Cancel current and future work before account erasure removes its objects."""
    if os.environ.get("API_INGESTION_ENABLED") != "1":
        return
    with transaction() as cursor:
        cursor.execute("""
            update api_ingestion.sources set enabled=false,next_due_at=null,revision=revision+1
            where project_id=%s and enabled
        """, (projectId,))
        cursor.execute("""
            update api_ingestion.runs r set status='cancelled',finished_at=now(),lease_expires_at=null
            from api_ingestion.datasets d where r.dataset_id=d.id and d.project_id=%s
              and r.status in ('queued','running','retrying')
        """, (projectId,))


def recordStaging(lease, versionId):
    with transaction() as cursor:
        if not _lockLease(cursor, lease):
            return False
        cursor.execute("""
            insert into api_ingestion.staged_objects (version_id,project_id,object_name)
            values (%s,%s,%s)
        """, (versionId, lease["project_id"], snapshotName(lease, versionId)))
        return True


def publishSnapshot(lease, versionId, rowCount, digest):
    objectName = snapshotName(lease, versionId)
    with transaction() as cursor:
        source = _lockLease(cursor, lease)
        if not source:
            cursor.execute("select object_name from api_ingestion.versions where run_id=%s", (lease["id"],))
            existing = cursor.fetchone()
            return bool(existing and existing["object_name"] == objectName)
        if source["current_version_id"] != lease["current_version_id"]:
            return False
        cursor.execute("""
            insert into api_ingestion.versions (id,dataset_id,run_id,object_name,row_count,content_digest)
            values (%s,%s,%s,%s,%s,%s)
        """, (versionId, lease["dataset_id"], lease["id"], objectName, rowCount, digest))
        cursor.execute("update api_ingestion.datasets set current_version_id=%s where id=%s",
                       (versionId, lease["dataset_id"]))
        cursor.execute("""
            update api_ingestion.sources set last_success_at=now(),
                next_due_at=case when cadence_minutes>0 then now()+make_interval(mins=>cadence_minutes) end
            where id=%s
        """, (lease["source_id"],))
        cursor.execute("""update api_ingestion.runs set status='succeeded',received_rows=%s,
            finished_at=now(),lease_expires_at=null where id=%s""", (rowCount, lease["id"]))
        return True


def cleanupCandidates(limit=20):
    """Keep current and previous versions and allow seven days for pinned readers."""
    with transaction() as cursor:
        cursor.execute("""
            select s.version_id,s.project_id,s.object_name,'staged' as kind,s.created_at
            from api_ingestion.staged_objects s
            where s.created_at<now()-interval '1 day'
              and not exists(select 1 from api_ingestion.versions v where v.id=s.version_id)
            union all
            select v.id,d.project_id,v.object_name,'retired',v.created_at
            from api_ingestion.versions v join api_ingestion.datasets d on d.id=v.dataset_id
            where v.created_at<now()-interval '7 days' and v.id<>d.current_version_id
              and 2 <= (select count(*) from api_ingestion.versions newer
                        where newer.dataset_id=v.dataset_id and newer.created_at>v.created_at)
            order by created_at limit %s
        """, (limit,))
        return cursor.fetchall()


def recordCleanup(candidate):
    with transaction() as cursor:
        if candidate["kind"] == "retired":
            cursor.execute("delete from api_ingestion.versions where id=%s", (candidate["version_id"],))
        cursor.execute("delete from api_ingestion.staged_objects where version_id=%s",
                       (candidate["version_id"],))
