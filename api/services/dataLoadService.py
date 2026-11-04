"""
dataLoadService.py

dataLoadService module provides services for loading and managing data from various sources (CSV, Excel, PDF, MySQL, PostgreSQL, MongoDB) and deleting tables for NubrixAI projects.
"""

__version__ = "1.0.0"
__author__ = "Rauhan Ahmed Siddiqui"
__all__ = ["dataLoadService"] 


from nubrix.components.pdfTableExtractor import PdfTableExtractor
from api.services.credits.creditTrackingCallback import CreditTrackingCallback
from api.commons import giveTableNameBack, tableFileState, updateProjectModifiedAt
from utils.exceptionHandler import CustomException
from utils.initMethods import forget_table
from pymongo.mongo_client import MongoClient
from pymongo.server_api import ServerApi
from fastapi import Form, UploadFile
from sqlalchemy import URL, create_engine
from utils.projectStorage import readJson
from utils.logger import logger
from api.commons import client
from typing import Annotated
from time import sleep
from api.models import (
    LoadMySQLorPostgreSQL,
    LoadMongoDB,
    DeleteTable
)
import pandas as pd
import tempfile
import json
import fitz
import io
import os
import gc
import re
import csv

# Seconds to wait before each retry of the release after a load's write.
RELEASE_RETRY_PAUSES = (0.2, 0.5)


class DataLoadService:
    """
    Service class for loading data from different sources and managing project tables.
    """
    def __init__(self) -> None:
        """
        Initializes the DataLoadService and sets up the client for database operations.
        """
        logger.info("Initializing Data Load Service.")
        self.client = client

    def _sanitizeFileName(self, fileName: str) -> str:
        """
        Sanitize a file name by removing the extension and replacing spaces and special characters with underscores.

        Args:
            fileName (str): The original file name (with or without extension).

        Returns:
            str: A sanitized, storage-safe file name without the extension.
        """
        baseName = os.path.splitext(fileName)[0]
        sanitized = re.sub(r"[^\w-]", "_", baseName)
        sanitized = re.sub(r"_+", "_", sanitized)
        sanitized = sanitized.strip("_")
        return sanitized

    def _releaseTableName(self, projectId: str, tableName: str) -> list[dict]:
        """Take the name from any transformation that still claims it, so none can remove or overwrite this table.

        Returns the claims it took, each with the transformation's artifact before the release where it was read.
        """
        try:
            # Read first to keep what each row had; a claim made between the two is released without it.
            claims = self.client.table("transformations").select("transformation_id, latest_approved_artifact").eq("project_id", projectId).eq("latest_approved_artifact->>table_name", tableName).execute().data or []
            priorArtifacts = {claim.get("transformation_id"): claim.get("latest_approved_artifact") for claim in claims}
            released = self.client.table("transformations").update({"latest_approved_artifact": None}).eq("project_id", projectId).eq("latest_approved_artifact->>table_name", tableName).execute().data or []
        except Exception as e:
            exception = CustomException(e, statusCode=503, uiMessage="We could not check this table name right now. Please try again.")
            logger.error(exception)
            raise exception
        return [
            {"transformation_id": row.get("transformation_id"), "latest_approved_artifact": priorArtifacts.get(row.get("transformation_id"))}
            for row in released
        ]

    def _restoreClaims(self, projectId: str, tableName: str, claims: list[dict]) -> None:
        """Give a name back to the transformations a load took it from, when the load wrote nothing over their table.

        Only while the file is still the one the transformation recorded when it wrote it, so another load's
        upload is never handed to it. A claim with no recorded `table_file` is compared with the file as this load
        read it before it read the claims. No file then and none now also counts as unchanged, as for an apply
        whose write failed. The file is checked again after the name is given back, and the name is released
        again if the file changed or could not be checked. A load writing later releases it after its own write.
        A row that has claimed anything since keeps it.
        """
        for claim in claims:
            prior = claim["latest_approved_artifact"]
            if not prior:
                continue
            ownFile = prior.get("table_file") or claim.get("file_before_release")
            giveTableNameBack(self.client, projectId, claim["transformation_id"], tableName, prior, (ownFile,))

    def _acceptTableName(self, projectId: str, tableName: str) -> list[dict]:
        """Refuse a name already in the project, then release it. Loads release it again once the file is written.

        Returns the claims it took, for _writeTable to give back if the load writes nothing. Each carries the
        file as it was read before the claims were, for a claim with no recorded `table_file` (applied before
        files were recorded, or the record failed). A load that writes releases the name after its write, so a
        claim still there when the claims are read means no load finished a write since that read: the file read
        is the transformation's. Read after the claims, it could be another load's upload.
        """
        from api.services.managementService import managementService
        managementService.validateTableNameAvailable(projectId=projectId, tableName=tableName)
        try:
            fileBeforeRelease = tableFileState(self.client, projectId, tableName)
        except Exception as e:
            # Then only a claim whose table has no file is given back, as before files were read here.
            fileBeforeRelease = None
            logger.warning(f"Could not read '{tableName}' before taking its name; a failed load gives it back only to a transformation that recorded its file or has none: {e}")
        claims = self._releaseTableName(projectId, tableName)
        return [{**claim, "file_before_release": fileBeforeRelease} for claim in claims]

    def _writeTable(self, projectId: str, tableName: str, claims: list[dict], writeParquet) -> None:
        """Write a load's table under a name it accepted, then release the name again.

        writeParquet(path) produces the file. If it fails, nothing was written, so the claims are given back
        while the file is unchanged (see _restoreClaims, which also covers no file and no record) and a
        transformation's table keeps its owner. A failed upload may still have landed, so after one the
        claims stay released. After the write the name is released again and the table's caches are cleared,
        whether or not this load saw a claim: another load may have taken it first. That release is retried,
        the caches are cleared even if it keeps failing, and the load then answers 503.
        """
        uploading = False
        try:
            with tempfile.NamedTemporaryFile(delete=True, suffix=".parquet") as temp:
                writeParquet(temp.name)
                uploading = True
                self.client.storage.from_("AnalyticsHub").upload(
                    file=temp.name,
                    path=f"{projectId}/{tableName}.parquet",
                    file_options={"upsert": "true"}
                )
        except Exception:
            if not uploading:
                self._restoreClaims(projectId, tableName, claims)
            raise
        releaseError = None
        for pause in (*RELEASE_RETRY_PAUSES, None):
            try:
                self._releaseTableName(projectId, tableName)
                releaseError = None
                break
            except CustomException as e:
                releaseError = e
                if pause is not None:
                    sleep(pause)
        try:
            forget_table(projectId, tableName)
        except Exception as e:
            logger.warning(f"Could not clear caches for {tableName} after a load replaced it: {e}")
        if releaseError:
            # A transformation may still claim this upload. Deleting it then keeps the upload only if it recorded its file.
            logger.error(f"Load wrote '{tableName}' in project {projectId} but could not release the name from its transformations.")
            raise releaseError

    @staticmethod
    def _sanitizeDfForParquet(df: pd.DataFrame) -> pd.DataFrame:
        for col in df.columns:
            if df[col].dtype == object:
                originalNulls = df[col].isna()
                numeric = pd.to_numeric(df[col], errors="coerce")
                newNulls = numeric.isna() & ~originalNulls
                if not newNulls.any():
                    df[col] = numeric
                    continue

                dt = pd.to_datetime(df[col], errors="coerce", format="mixed")
                dtNewNulls = dt.isna() & ~originalNulls
                if not dtNewNulls.any():
                    df[col] = dt
                    continue

                df[col] = (
                    df[col]
                    .astype(str)
                    .replace({"None": pd.NA, "nan": pd.NA, "<NA>": pd.NA})
                )
        return df

    @staticmethod
    def _detectCsvDelimiter(fileBytes: bytes) -> str:
        sample = fileBytes[:8192].decode("utf-8-sig", errors="ignore")
        try:
            return csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
        except csv.Error:
            return ","

    @classmethod
    def _readCsvUpload(cls, fileBytes: bytes) -> pd.DataFrame:
        delimiter = cls._detectCsvDelimiter(fileBytes)
        return pd.read_csv(io.BytesIO(fileBytes), sep=delimiter, parse_dates=True)

    async def loadCsvData(self, projectId: Annotated[str, Form()], files: list[UploadFile], userId: str | None = None) -> dict:
        """
        Load CSV files into project storage and generate metadata for the newly
        added tables only.

        Raises:
            CustomException:
                400 - Missing projectId or files
                415 - Unsupported file type
                422 - Invalid CSV upload details
                500 - CSV upload failed
        """
        try:
            if not projectId or not files:
                raise CustomException(
                    ValueError("Missing projectId or files"),
                    statusCode=400,
                    uiMessage="Missing projectId or files."
                )
            from api.services.managementService import managementService
            loadedTableNames = []
            for file in files:
                if not file.filename.lower().endswith(".csv"):
                    raise CustomException(
                        ValueError("Invalid file type"),
                        statusCode=415,
                        uiMessage="Unsupported file type. Upload CSV files only."
                    )
                sanitizedName = self._sanitizeFileName(file.filename)
                fileBytes = await file.read()
                claims = self._acceptTableName(projectId, sanitizedName)
                self._writeTable(
                    projectId, sanitizedName, claims,
                    lambda path: self._sanitizeDfForParquet(self._readCsvUpload(fileBytes)).to_parquet(path, compression="snappy"),
                )
                loadedTableNames.append(sanitizedName)
            updateProjectModifiedAt(projectId)
            # Generate metadata ONLY for the newly loaded tables
            newMetadata = {}
            if loadedTableNames:
                try:
                    newMetadata = managementService.generateMetadataForTables(
                        projectId=projectId, tableNames=loadedTableNames, userId=userId
                    )
                except Exception as metaErr:
                    logger.error(f"Metadata generation failed for CSV load: {metaErr}")
            return newMetadata
        except CustomException:
            raise
        except Exception as e:
            exception = CustomException(
                e,
                uiMessage="CSV upload failed. Try again later."
            )
            logger.error(exception)
            raise exception
        
    async def loadExcelData(self, projectId: Annotated[str, Form()], files: list[UploadFile], userId: str | None = None) -> dict:
        """
        Load Excel files into project storage and generate metadata for the newly
        added tables only.

        Raises:
            CustomException:
                400 - Missing projectId or files
                415 - Unsupported file type
                422 - Invalid Excel upload details
                500 - Excel upload failed
        """
        try:
            if not projectId or not files:
                raise CustomException(
                    ValueError("Missing projectId or files"),
                    statusCode=400,
                    uiMessage="Missing projectId or files."
                )
            from api.services.managementService import managementService
            loadedTableNames = []
            for file in files:
                if not file.filename.lower().endswith((".xls", ".xlsx")):
                    raise CustomException(
                        ValueError("Invalid file type"),
                        statusCode=415,
                        uiMessage="Unsupported file type. Upload Excel files only."
                    )
                allSheetData = pd.read_excel(io.BytesIO(await file.read()), sheet_name=None, parse_dates=True)
                sanitizedBase = self._sanitizeFileName(file.filename)
                for sheetName, sheetData in allSheetData.items():
                    sanitizedSheet = re.sub(r"[^\w-]", "_", str(sheetName)).strip("_")
                    tableName = f"{sanitizedBase}_{sanitizedSheet}"
                    claims = self._acceptTableName(projectId, tableName)
                    self._writeTable(
                        projectId, tableName, claims,
                        lambda path: self._sanitizeDfForParquet(sheetData).to_parquet(path, compression="snappy"),
                    )
                    loadedTableNames.append(tableName)
            updateProjectModifiedAt(projectId)
            newMetadata = {}
            if loadedTableNames:
                try:
                    newMetadata = managementService.generateMetadataForTables(
                        projectId=projectId, tableNames=loadedTableNames, userId=userId
                    )
                except Exception as metaErr:
                    logger.error(f"Metadata generation failed for Excel load: {metaErr}")
            return newMetadata
        except CustomException:
            raise
        except Exception as e:
            exception = CustomException(
                e,
                uiMessage="Excel upload failed. Try again later."
            )
            logger.error(exception)
            raise exception
        
    async def loadPdfData(self, projectId: Annotated[str, Form()], files: list[UploadFile], userId: str | None = None) -> dict:
        """
        Load PDF files into project storage using a two-pass approach:
        Pass 1 rasterizes each page and extracts table fragments via a VLM.
        Pass 2 sends a compact summary of all fragments to the LLM which
        returns a merge plan grouping them into logical tables.
        Tables are stored as individual parquet files ({filename}_table{N}.parquet).
        Raises:
            CustomException:
                400 - Missing projectId or files
                415 - Unsupported file type
                422 - No extractable tables found
                500 - PDF upload failed
        """
        try:
            if not projectId or not files:
                raise CustomException(
                    ValueError("Missing projectId or files"),
                    statusCode=400,
                    uiMessage="Missing projectId or files."
                )
            loadedTableNames = []
            for file in files:
                if not file.filename.lower().endswith(".pdf"):
                    raise CustomException(
                        ValueError("Invalid file type"),
                        statusCode=415,
                        uiMessage="Unsupported file type. Upload PDF files only."
                    )
                fileBytes = await file.read()
                baseName = self._sanitizeFileName(file.filename)
                extractor = PdfTableExtractor(projectId=projectId, userId=userId)
                pdfCallbacks = []
                if userId:
                    pdfCallbacks.append(CreditTrackingCallback(userId=userId, operationType="pdf_extraction_per_page"))

                pageImages: list[tuple[int, str]] = []
                with fitz.open(stream=fileBytes, filetype="pdf") as doc:
                    totalPages = len(doc)
                    logger.info(f"Processing {file.filename}: {totalPages} pages")

                    for pageNum, page in enumerate(doc, start=1):
                        b64 = PdfTableExtractor.renderPageToBase64(
                            page, dpi=extractor.dpi
                        )
                        pageImages.append((pageNum, b64))

                del fileBytes
                gc.collect()

                fragments = await extractor.extractPagesParallel(
                    pageImages, totalPages, callbacks=pdfCallbacks if pdfCallbacks else None
                )
                del pageImages
                gc.collect()

                if not fragments:
                    raise CustomException(
                        ValueError("No extractable tables in PDF"),
                        statusCode=422,
                        uiMessage="No extractable tables found in the PDF. Ensure the PDF contains tables."
                    )

                mergePlan = extractor.planMerge(fragments, callbacks=pdfCallbacks if pdfCallbacks else None)
                pendingTables: list[dict] = []

                for group in mergePlan.groups:
                    canonicalHeaders = group.canonicalHeaders
                    numCols = len(canonicalHeaders)
                    dfs = []
                    for idx in group.tableIndices:
                        if idx < 0 or idx >= len(fragments):
                            continue
                        rows = fragments[idx]["table"].rows
                        normalized = []
                        for r in rows:
                            if len(r) == numCols:
                                normalized.append(r)
                            elif len(r) > numCols:
                                normalized.append(r[:numCols])
                            else:
                                normalized.append(
                                    r + [None] * (numCols - len(r))
                                )
                        dfs.append(
                            pd.DataFrame(normalized, columns=canonicalHeaders)
                        )

                    if not dfs:
                        continue
                    combined = pd.concat(dfs, ignore_index=True)
                    combined.dropna(how="all", inplace=True)
                    if not combined.empty:
                        pendingTables.append({
                            "headers": canonicalHeaders,
                            "df": combined,
                        })

                if not pendingTables:
                    raise CustomException(
                        ValueError("No extractable tables in PDF"),
                        statusCode=422,
                        uiMessage="No extractable tables found in the PDF. Ensure the PDF contains tables."
                    )

                logger.info(
                    f"{file.filename}: {len(fragments)} fragments merged "
                    f"into {len(pendingTables)} table(s)"
                )

                # Every name is accepted before any is written. A load that stops gives back the names it has not written.
                acceptedClaims: dict[str, list[dict]] = {}
                try:
                    for idx in range(1, len(pendingTables) + 1):
                        tableName = f"{baseName}_table{idx}"
                        acceptedClaims[tableName] = self._acceptTableName(projectId, tableName)
                    for idx, entry in enumerate(pendingTables, start=1):
                        tableName = f"{baseName}_table{idx}"
                        self._writeTable(
                            projectId, tableName, acceptedClaims.pop(tableName),
                            lambda path: self._sanitizeDfForParquet(entry["df"]).to_parquet(path, compression="snappy"),
                        )
                        loadedTableNames.append(tableName)
                except Exception:
                    for unwrittenName, claims in acceptedClaims.items():
                        self._restoreClaims(projectId, unwrittenName, claims)
                    raise

            updateProjectModifiedAt(projectId)
            # Generate metadata ONLY for the newly loaded PDF tables
            newMetadata = {}
            if loadedTableNames:
                try:
                    from api.services.managementService import managementService
                    newMetadata = managementService.generateMetadataForTables(
                        projectId=projectId, tableNames=loadedTableNames, userId=userId
                    )
                except Exception as metaErr:
                    logger.error(f"Metadata generation failed for PDF load: {metaErr}")
            return newMetadata
        except CustomException:
            raise
        except Exception as e:
            exception = CustomException(
                e,
                uiMessage="PDF upload failed. Try again later."
            )
            logger.error(exception)
            raise exception

    def loadMySql(self, connection: LoadMySQLorPostgreSQL, userId: str | None = None) -> dict:
        """
        Load data from MySQL database and generate metadata for the loaded table.
        """
        try:
            required = [connection.host, connection.port, connection.user,
                        connection.password, connection.db, connection.table, connection.projectId]
            if not all(required):
                raise CustomException(
                    ValueError("Missing required DB fields"),
                    statusCode=400,
                    uiMessage="Missing required DB connection fields."
                )
            connStr = URL.create(
                "mysql+pymysql",
                username=connection.user,
                password=connection.password,
                host=connection.host,
                port=connection.port,
                database=connection.db,
            )
            engine = create_engine(connStr)
            sanitizedTable = self._sanitizeFileName(connection.table)
            from api.services.managementService import managementService
            claims = self._acceptTableName(connection.projectId, sanitizedTable)
            self._writeTable(
                connection.projectId, sanitizedTable, claims,
                lambda path: pd.read_sql(f"SELECT * FROM {connection.table}", engine).to_parquet(path, compression="snappy"),
            )
            updateProjectModifiedAt(connection.projectId)
            newMetadata = {}
            try:
                newMetadata = managementService.generateMetadataForTables(
                    projectId=connection.projectId, tableNames=[sanitizedTable], userId=userId
                )
            except Exception as metaErr:
                logger.error(f"Metadata generation failed for MySQL load: {metaErr}")
            return newMetadata
        except CustomException:
            raise
        except Exception as e:
            exception = CustomException(
                e,
                statusCode=400,
                uiMessage="Unable to connect to MySQL : check host/port/credentials."
            )
            logger.error(exception)
            raise exception

            
    def loadPostgreSQL(self, connection: LoadMySQLorPostgreSQL, userId: str | None = None) -> dict:
        """
        Load data from PostgreSQL database and generate metadata for the loaded table.
        """
        try:
            required = [connection.host, connection.port, connection.user,
                        connection.password, connection.db, connection.table, connection.projectId]
            if not all(required):
                raise CustomException(
                    ValueError("Missing required DB fields"),
                    statusCode=400,
                    uiMessage="Missing required DB connection fields."
                )
            connStr = URL.create(
                "postgresql+psycopg2",
                username=connection.user,
                password=connection.password,
                host=connection.host,
                port=connection.port,
                database=connection.db,
            )
            engine = create_engine(connStr)
            sanitizedTable = self._sanitizeFileName(connection.table)
            from api.services.managementService import managementService
            claims = self._acceptTableName(connection.projectId, sanitizedTable)
            self._writeTable(
                connection.projectId, sanitizedTable, claims,
                lambda path: pd.read_sql(f"SELECT * FROM {connection.table}", engine).to_parquet(path, compression="snappy"),
            )
            updateProjectModifiedAt(connection.projectId)
            newMetadata = {}
            try:
                newMetadata = managementService.generateMetadataForTables(
                    projectId=connection.projectId, tableNames=[sanitizedTable], userId=userId
                )
            except Exception as metaErr:
                logger.error(f"Metadata generation failed for PostgreSQL load: {metaErr}")
            return newMetadata
        except CustomException:
            raise
        except Exception as e:
            exception = CustomException(
                e,
                statusCode=400,
                uiMessage="Unable to connect to PostgreSQL: check host/port/credentials."
            )
            logger.error(exception)
            raise exception
        
    def loadMongoDB(self, connection: LoadMongoDB, userId: str | None = None) -> dict:
        """
        Load data from MongoDB and generate metadata for the loaded collection.
        """
        try:
            if not connection.connectionString or not connection.db or not connection.collection:
                raise CustomException(
                    ValueError("Missing MongoDB connection fields"),
                    statusCode=400,
                    uiMessage="Missing required DB connection fields."
                )
            sanitizedCollection = self._sanitizeFileName(connection.collection)
            from api.services.managementService import managementService

            def writeCollection(path: str) -> None:
                mongoClient = MongoClient(connection.connectionString, server_api=ServerApi('1'))
                records = list(mongoClient[connection.db][connection.collection].find())
                for record in records:
                    record.pop("_id", None)
                pd.DataFrame(records).to_parquet(path, compression="snappy")

            claims = self._acceptTableName(connection.projectId, sanitizedCollection)
            self._writeTable(connection.projectId, sanitizedCollection, claims, writeCollection)
            updateProjectModifiedAt(connection.projectId)
            newMetadata = {}
            try:
                newMetadata = managementService.generateMetadataForTables(
                    projectId=connection.projectId, tableNames=[sanitizedCollection], userId=userId
                )
            except Exception as metaErr:
                logger.error(f"Metadata generation failed for MongoDB load: {metaErr}")
            return newMetadata
        except CustomException:
            raise
        except Exception as e:
            exception = CustomException(
                e,
                statusCode=400,
                uiMessage="Unable to connect to MongoDB: check connection string/credentials."
            )
            logger.error(exception)
            raise exception
        
    def deleteTable(self, tableDetails: DeleteTable) -> None:
        """
        Deletes a table from the project storage and updates project metadata accordingly.

        Args:
            tableDetails (DeleteTable): Details of the table to delete.
        Returns:
            None
        Raises:
            CustomException: If deletion or metadata update fails.
        """
        try:
            _ = self.client.storage.from_("AnalyticsHub").remove(f"{tableDetails.projectId}/{tableDetails.tableName}" + ".parquet")
            jsonData = readJson(tableDetails.projectId, "metadata.json")
            jsonData.pop(tableDetails.tableName)
            with io.BytesIO() as buffer:
                buffer.write(json.dumps(jsonData, indent=4).encode("utf-8"))
                buffer.seek(0)
                self.client.storage.from_("AnalyticsHub").upload(path = f"{tableDetails.projectId}/metadata.json", file = buffer.getvalue(), file_options = {"upsert": "true"})
            # The transformation no longer owns this name. If this fails, the next load of the name releases it.
            try:
                self._releaseTableName(tableDetails.projectId, tableDetails.tableName)
            except Exception as e:
                logger.warning(f"Could not release deleted table {tableDetails.tableName} from its transformation: {e}")
            try:
                forget_table(tableDetails.projectId, tableDetails.tableName)
            except Exception as e:
                logger.warning(f"Could not clear caches for deleted table {tableDetails.tableName}: {e}")
            updateProjectModifiedAt(tableDetails.projectId)
            return
        except Exception as e:
            exception  = CustomException(e)
            logger.error(exception)
            raise exception

dataLoadService = DataLoadService()
