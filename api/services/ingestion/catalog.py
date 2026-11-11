"""Resolve published API snapshots without changing legacy project files."""

from contextlib import closing, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import os
import re
from uuid import UUID


_MANAGED_NAME = re.compile(r"api__[0-9a-f]{32}\Z")
_TABLE_PINS: ContextVar[tuple | None] = ContextVar("api_ingestion_table_pins", default=None)
_METADATA_TYPES = {"integer": "int64", "number": "float64", "boolean": "bool",
                   "date": "datetime64[ns]", "timestamp": "datetime64[ns, tz]"}


@dataclass(frozen=True)
class TableReadRef:
    versionId: str
    objectName: str
    logicalName: str
    rowCount: int
    columnCount: int


def catalogEnabled() -> bool:
    return os.environ.get("API_INGESTION_ENABLED") == "1"


def isManagedAlias(tableName: str) -> bool:
    return isinstance(tableName, str) and bool(_MANAGED_NAME.fullmatch(tableName))


def _readManagedRows(projectId: str, tableName: str | None = None) -> list[dict]:
    from psycopg2.extras import RealDictCursor
    from api.services.ingestion.repository import connect

    query = """
        SELECT d.table_name, d.display_name, d.description, d.columns, d.active,
               v.id AS version_id, v.object_name, v.row_count
        FROM api_ingestion.datasets d
        LEFT JOIN api_ingestion.versions v
            ON v.id = d.current_version_id AND v.dataset_id = d.id
        WHERE d.project_id = %s
    """
    args = (projectId,)
    if tableName is not None:
        query += " AND d.table_name = %s"
        args += (tableName,)
    with closing(connect()) as conn, conn, conn.cursor(cursor_factory=RealDictCursor) as cursor:
        cursor.execute(query, args)
        return cursor.fetchall()


def _readRef(row: dict) -> TableReadRef:
    from utils.projectStorage import StorageReadError

    alias = row["table_name"]
    version = UUID(str(row["version_id"]))
    expectedName = f"{alias}--{version.hex}.snapshot"
    if not isManagedAlias(alias) or row["object_name"] != expectedName:
        raise StorageReadError("Invalid API dataset storage reference.")
    return TableReadRef(str(version), expectedName, alias, row["row_count"], len(row["columns"]))


def currentTableVersions(projectId: str) -> dict[str, TableReadRef]:
    if not catalogEnabled():
        return {}
    return {row["table_name"]: _readRef(row) for row in _readManagedRows(projectId) if row["version_id"]}


@contextmanager
def pinTableVersions(projectId: str, references: dict[str, TableReadRef] | None = None):
    """Keep each table version stable across the reads for one analysis."""
    existing = _TABLE_PINS.get()
    if references is None and existing is not None and existing[0] == projectId:
        yield existing[2]
        return
    pinned = dict(references or {})
    used: dict[str, str] = {}
    token = _TABLE_PINS.set((projectId, pinned, used))
    try:
        yield used
    finally:
        _TABLE_PINS.reset(token)


def resolveTableVersion(projectId: str, logicalName: str) -> TableReadRef | None:
    if not catalogEnabled() or not isManagedAlias(logicalName):
        return None
    pin = _TABLE_PINS.get()
    if pin is not None and pin[0] != projectId:
        pin = None
    reference = pin[1].get(logicalName) if pin is not None else None
    if reference is None:
        rows = _readManagedRows(projectId, logicalName)
        if not rows:
            return None
        if rows[0]["version_id"] is None:
            from utils.projectStorage import StorageReadError
            raise StorageReadError("This API dataset has no published version.")
        reference = _readRef(rows[0])
    if pin is not None:
        pin[1][logicalName] = reference
        pin[2][logicalName] = reference.versionId
    return reference


def managedTableEntries(projectId: str) -> dict:
    if not catalogEnabled():
        return {}
    entries = {}
    for row in _readManagedRows(projectId):
        if not row["version_id"]:
            continue
        reference = _readRef(row)
        entries[reference.logicalName] = {
            "displayName": row["display_name"],
            "description": row.get("description") or row["display_name"],
            "shape": [reference.rowCount, reference.columnCount],
            "columns": [
                {"name": column["name"], "type": _METADATA_TYPES.get(column["type"], column["type"]),
                 "description": column.get("description", "")}
                for column in row["columns"]
            ],
            "isActive": row["active"],
            "sourceKind": "rest_api",
            "versionId": reference.versionId,
        }
    return entries


def readProjectCatalog(projectId: str, *, readLegacy=None) -> dict:
    from utils.projectStorage import StorageReadError, readJson

    managed = managedTableEntries(projectId)
    try:
        legacy = (readLegacy or readJson)(projectId, "metadata.json")
    except StorageReadError as error:
        if not managed or getattr(error, "statusCode", None) != 404:
            raise
        legacy = {}
    return {**legacy, **managed}


def isRegisteredTable(projectId: str, tableName: str) -> bool:
    return catalogEnabled() and isManagedAlias(tableName) and bool(_readManagedRows(projectId, tableName))


def toggleManagedActive(projectId: str, tableName: str) -> bool | None:
    if not catalogEnabled() or not isManagedAlias(tableName):
        return None
    from api.services.ingestion.repository import connect

    with closing(connect()) as conn, conn, conn.cursor() as cursor:
        cursor.execute(
            """UPDATE api_ingestion.datasets SET active = NOT active
               WHERE project_id = %s AND table_name = %s RETURNING active""",
            (projectId, tableName),
        )
        row = cursor.fetchone()
        return row[0] if row else None


def editManagedMetadata(projectId: str, tableName: str, tableDescription: str | None,
                        columnName: str | None, columnDescription: str | None) -> bool:
    if not catalogEnabled() or not isManagedAlias(tableName):
        return False
    from psycopg2.extras import Json
    from api.services.ingestion.repository import connect

    with closing(connect()) as conn, conn, conn.cursor() as cursor:
        cursor.execute(
            """SELECT id, columns FROM api_ingestion.datasets
               WHERE project_id = %s AND table_name = %s FOR UPDATE""",
            (projectId, tableName),
        )
        row = cursor.fetchone()
        if row is None:
            return False
        datasetId, columns = row
        if tableDescription and not (columnName and columnDescription):
            cursor.execute("UPDATE api_ingestion.datasets SET description = %s WHERE id = %s", (tableDescription, datasetId))
        elif columnName and columnDescription and not tableDescription:
            column = next((entry for entry in columns if entry["name"] == columnName), None)
            if column is None:
                raise ValueError("Column not found.")
            column["description"] = columnDescription
            cursor.execute("UPDATE api_ingestion.datasets SET columns = %s WHERE id = %s", (Json(columns), datasetId))
        else:
            raise ValueError("Invalid combination of parameters provided.")
        return True
