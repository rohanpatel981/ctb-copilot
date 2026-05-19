"""FastAPI service: upload endpoint, status, periods, schema, query.

Adapters are wired once at import time. Swap LocalDiskStorage / AnthropicLLM
here when you ship S3 / Bedrock adapters — every call site in `query.py` and
`ingest.py` depends on the Protocol, not the concrete class.
"""

import json
import uuid
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from ctb_copilot.adapters.llm_anthropic import AnthropicLLM
from ctb_copilot.adapters.source_docdb import DocumentDBSource
from ctb_copilot.adapters.storage_local import LocalDiskStorage
from ctb_copilot.config import settings
from ctb_copilot.db import LLM_SCHEMA_DDL, init_db, ro_connection, rw_connection
from ctb_copilot.ingest import ingest_file
from ctb_copilot.query import QueryResult, UnsafeSQLError, run_query
from ctb_copilot.sync import SyncError, run_sync
from ctb_copilot.tenants import TenantSync

storage = LocalDiskStorage(settings.storage_dir)
llm = AnthropicLLM(api_key=settings.anthropic_api_key, model=settings.anthropic_model)
init_db(settings.duckdb_path)

app = FastAPI(title="ctb-copilot", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # v1 local-dev. Tighten in v2 when auth lands.
    allow_methods=["*"],
    allow_headers=["*"],
)


class UploadResponse(BaseModel):
    upload_id: str
    status: str


class IngestionStatus(BaseModel):
    id: str
    filename: str | None
    status: str
    row_count: int | None
    error: str | None
    client_id: str | None = None
    gaap_id: str | None = None
    reporting_parent_company_id: str | None = None
    fin_year_id: str | None = None
    reporting_period_id: str | None = None
    currency_id: str | None = None


class QueryRequest(BaseModel):
    question: str


class SyncRequest(BaseModel):
    """FE sends camelCase keys inside `filter`. status='ACTIVE' is pinned
    server-side; FE doesn't need to send it."""
    filter: TenantSync


class SyncResponse(BaseModel):
    sync_id: str
    status: str
    filter_applied: dict[str, Any]


class DocDBConfigResponse(BaseModel):
    configured: bool
    database: str | None = None
    collection: str | None = None


def _run_ingestion_bg(db_path: Path, source_path: Path, upload_id: str, tenant: TenantSync, original_filename: str) -> None:
    """Background task wrapper that swallows exceptions. ingest_file already
    persists failures to the ingestions table; we don't want the background
    task crash to take down the worker."""
    try:
        ingest_file(
            db_path=db_path,
            source_path=source_path,
            tenant=tenant,
            upload_id=upload_id,
            original_filename=original_filename,
        )
    except Exception:
        pass


def _tenant_where_sql() -> str:
    return (
        "client_id=? AND gaap_id=? AND reporting_parent_company_id=? "
        "AND fin_year_id=? AND reporting_period_id=? AND currency_id=?"
    )


@app.post("/upload", response_model=UploadResponse)
async def upload(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    clientId: str = Form(...),
    gaapId: str = Form(...),
    reportingParentCompanyId: str = Form(...),
    finYearId: str = Form(...),
    reportingPeriodId: str = Form(...),
    currencyId: str = Form(...),
) -> UploadResponse:
    if not file.filename or not file.filename.lower().endswith((".xlsx", ".xlsb", ".xls")):
        raise HTTPException(400, "Upload must be an Excel file (.xlsx / .xlsb / .xls).")

    try:
        tenant = TenantSync(
            clientId=clientId, gaapId=gaapId,
            reportingParentCompanyId=reportingParentCompanyId,
            finYearId=finYearId, reportingPeriodId=reportingPeriodId,
            currencyId=currencyId,
        )
    except Exception as e:
        raise HTTPException(400, f"Invalid tenant identity: {e}") from e

    upload_id = str(uuid.uuid4())
    storage_key = f"{upload_id}{Path(file.filename).suffix.lower()}"
    storage.put(storage_key, file.file)

    td = tenant.as_dict()
    where_args = list(td.values())  # ordered by TENANT_ID_COLUMNS

    with rw_connection(settings.duckdb_path) as conn:
        # Override semantics: uploading a new file with the same tenant
        # tuple replaces the previous rows for that tuple. Old ingestion
        # rows are marked 'replaced' so the audit trail is preserved.
        # Crucially, OTHER tenants' rows are untouched.
        conn.execute(
            f"UPDATE ingestions SET status='replaced' WHERE status='done' AND {_tenant_where_sql()}",
            where_args,
        )
        conn.execute(
            f"DELETE FROM ctb_data WHERE {_tenant_where_sql()}",
            where_args,
        )
        conn.execute(
            "INSERT INTO ingestions (id, filename, status, source_type, "
            "client_id, gaap_id, reporting_parent_company_id, fin_year_id, "
            "reporting_period_id, currency_id) "
            "VALUES (?, ?, 'pending', 'excel', ?, ?, ?, ?, ?, ?)",
            [upload_id, file.filename, *where_args],
        )

    background_tasks.add_task(
        _run_ingestion_bg,
        settings.duckdb_path,
        storage.local_path(storage_key),
        upload_id,
        tenant,
        file.filename,
    )
    return UploadResponse(upload_id=upload_id, status="pending")


_INGESTIONS_SELECT = (
    "SELECT id, filename, status, row_count, error, "
    "client_id, gaap_id, reporting_parent_company_id, "
    "fin_year_id, reporting_period_id, currency_id FROM ingestions"
)


def _row_to_status(r: tuple) -> IngestionStatus:
    return IngestionStatus(
        id=r[0], filename=r[1], status=r[2], row_count=r[3], error=r[4],
        client_id=r[5], gaap_id=r[6], reporting_parent_company_id=r[7],
        fin_year_id=r[8], reporting_period_id=r[9], currency_id=r[10],
    )


@app.get("/uploads", response_model=list[IngestionStatus])
def list_uploads() -> list[IngestionStatus]:
    with ro_connection(settings.duckdb_path) as conn:
        rows = conn.execute(_INGESTIONS_SELECT + " ORDER BY uploaded_at DESC").fetchall()
    return [_row_to_status(r) for r in rows]


@app.get("/uploads/{upload_id}", response_model=IngestionStatus)
def get_upload(upload_id: str) -> IngestionStatus:
    with ro_connection(settings.duckdb_path) as conn:
        row = conn.execute(_INGESTIONS_SELECT + " WHERE id=?", [upload_id]).fetchone()
    if row is None:
        raise HTTPException(404, "Upload not found.")
    return _row_to_status(row)


@app.get("/periods")
def list_periods() -> list[dict]:
    """List loaded (client, fin_year_id, reporting_period_id) tuples with
    row counts. Multi-tenant aware — caller is expected to filter on
    client_id in the UI to see only their engagement's periods."""
    with ro_connection(settings.duckdb_path) as conn:
        rows = conn.execute(
            "SELECT client_id, fin_year_id, reporting_period_id, currency_id, "
            "COUNT(*) AS row_count, COUNT(DISTINCT entity_code) AS entity_count "
            "FROM ctb_data "
            "GROUP BY client_id, fin_year_id, reporting_period_id, currency_id "
            "ORDER BY client_id, fin_year_id, reporting_period_id"
        ).fetchall()
    return [
        {
            "client_id": r[0],
            "fin_year_id": r[1],
            "reporting_period_id": r[2],
            "currency_id": r[3],
            "row_count": r[4],
            "entity_count": r[5],
        }
        for r in rows
    ]


@app.get("/entities")
def list_entities() -> list[dict]:
    with ro_connection(settings.duckdb_path) as conn:
        rows = conn.execute(
            "SELECT entity_code, entity_name, COUNT(*) AS row_count "
            "FROM ctb_data WHERE entity_code IS NOT NULL "
            "GROUP BY entity_code, entity_name ORDER BY entity_code"
        ).fetchall()
    return [{"entity_code": r[0], "entity_name": r[1], "row_count": r[2]} for r in rows]


@app.get("/schema")
def get_schema() -> dict:
    return {"ddl": LLM_SCHEMA_DDL}


@app.post("/query", response_model=QueryResult)
async def query(req: QueryRequest) -> QueryResult:
    if not req.question.strip():
        raise HTTPException(400, "question is required.")
    try:
        return await run_query(question=req.question, llm=llm, db_path=settings.duckdb_path)
    except UnsafeSQLError as e:
        raise HTTPException(400, f"Generated SQL rejected by safety check: {e}") from e


@app.get("/sync/config", response_model=DocDBConfigResponse)
def sync_config() -> DocDBConfigResponse:
    """Whether DocumentDB sync is wired up. The UI uses this to decide
    whether to show the sync section. No filter values here — FE owns
    the (clientId, gaapId, …) it wants to sync."""
    configured = bool(settings.docdb_uri and settings.docdb_database and settings.docdb_collection)
    return DocDBConfigResponse(
        configured=configured,
        database=settings.docdb_database,
        collection=settings.docdb_collection,
    )


def _build_docdb_filter(tenant: TenantSync) -> dict[str, Any]:
    """Tenant identity becomes the Mongo filter. status='ACTIVE' is
    server-pinned so the FE can't sync stale rows by mistake."""
    return {
        "clientId": tenant.client_id,
        "gaapId": tenant.gaap_id,
        "reportingParentCompanyId": tenant.reporting_parent_company_id,
        "finYearId": tenant.fin_year_id,
        "reportingPeriodId": tenant.reporting_period_id,
        "currencyId": tenant.currency_id,
        "status": "ACTIVE",
    }


def _run_sync_bg(sync_id: str, tenant: TenantSync, filter_doc: dict) -> None:
    """BackgroundTask wrapper. Swallows SyncError because run_sync has
    already written the failure to the ingestions table."""
    source = DocumentDBSource(
        uri=settings.docdb_uri,
        database=settings.docdb_database,
        collection=settings.docdb_collection,
    )
    try:
        run_sync(
            sync_id=sync_id,
            source=source,
            filter_doc=filter_doc,
            tenant=tenant,
            db_path=settings.duckdb_path,
        )
    except SyncError:
        pass


@app.post("/sync", response_model=SyncResponse)
def sync_from_docdb(req: SyncRequest, background_tasks: BackgroundTasks) -> SyncResponse:
    if not (settings.docdb_uri and settings.docdb_database and settings.docdb_collection):
        raise HTTPException(
            400,
            "DocumentDB sync is not configured. Set DOCDB_URI, DOCDB_DATABASE, "
            "and DOCDB_COLLECTION in your .env to enable.",
        )

    tenant = req.filter
    filter_doc = _build_docdb_filter(tenant)
    sync_id = str(uuid.uuid4())
    td = tenant.as_dict()

    source_metadata = {
        "source_type": "docdb",
        "database": settings.docdb_database,
        "collection": settings.docdb_collection,
        "filter": filter_doc,
    }
    with rw_connection(settings.duckdb_path) as conn:
        conn.execute(
            "INSERT INTO ingestions (id, filename, status, source_type, source_metadata, "
            "client_id, gaap_id, reporting_parent_company_id, fin_year_id, "
            "reporting_period_id, currency_id) "
            "VALUES (?, ?, 'pending', 'docdb', ?, ?, ?, ?, ?, ?, ?)",
            [
                sync_id, f"<docdb:{settings.docdb_collection}>",
                json.dumps(source_metadata),
                td["client_id"], td["gaap_id"], td["reporting_parent_company_id"],
                td["fin_year_id"], td["reporting_period_id"], td["currency_id"],
            ],
        )

    background_tasks.add_task(_run_sync_bg, sync_id, tenant, filter_doc)

    return SyncResponse(sync_id=sync_id, status="pending", filter_applied=filter_doc)


@app.get("/sync/{sync_id}", response_model=IngestionStatus)
def get_sync_status(sync_id: str) -> IngestionStatus:
    """Alias for /uploads/{id} — same data, different conceptual surface."""
    return get_upload(sync_id)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


def run() -> None:
    """Entry point for `uv run ctb-api`."""
    uvicorn.run(
        "ctb_copilot.api:app",
        host=settings.api_host,
        port=settings.api_port,
        reload=True,
    )
