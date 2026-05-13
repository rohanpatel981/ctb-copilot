"""FastAPI service: upload endpoint, status, periods, schema, query.

Adapters are wired once at import time. Swap LocalDiskStorage / AnthropicLLM
here when you ship S3 / Bedrock adapters — every call site in `query.py` and
`ingest.py` depends on the Protocol, not the concrete class.
"""

import uuid
from pathlib import Path

import uvicorn
from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from ctb_copilot.adapters.llm_anthropic import AnthropicLLM
from ctb_copilot.adapters.storage_local import LocalDiskStorage
from ctb_copilot.config import settings
from ctb_copilot.db import LLM_SCHEMA_DDL, init_db, ro_connection, rw_connection
from ctb_copilot.ingest import ingest_file
from ctb_copilot.query import QueryResult, UnsafeSQLError, run_query

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
    period: str | None
    status: str
    row_count: int | None
    error: str | None


class QueryRequest(BaseModel):
    question: str


def _run_ingestion_bg(db_path: Path, source_path: Path, upload_id: str, period: str, original_filename: str) -> None:
    """Background task wrapper that swallows exceptions. ingest_file already
    persists failures to the ingestions table; we don't want the background
    task crash to take down the worker."""
    try:
        ingest_file(
            db_path=db_path,
            source_path=source_path,
            period=period,
            upload_id=upload_id,
            original_filename=original_filename,
        )
    except Exception:
        pass


@app.post("/upload", response_model=UploadResponse)
async def upload(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    period: str = Form(...),
) -> UploadResponse:
    if not file.filename or not file.filename.lower().endswith((".xlsx", ".xlsb", ".xls")):
        raise HTTPException(400, "Upload must be an Excel file (.xlsx / .xlsb / .xls).")
    if not period.strip():
        raise HTTPException(400, "period is required, e.g. 'FY 2024-25'.")

    upload_id = str(uuid.uuid4())
    storage_key = f"{upload_id}{Path(file.filename).suffix.lower()}"
    storage.put(storage_key, file.file)

    with rw_connection(settings.duckdb_path) as conn:
        # Override semantics: uploading a new file with an existing FY tag
        # replaces the previous data for that period. Old ingestion rows are
        # marked 'replaced' so the audit trail is preserved.
        conn.execute(
            "UPDATE ingestions SET status='replaced' WHERE period=? AND status='done'",
            [period],
        )
        conn.execute("DELETE FROM ctb_data WHERE period=?", [period])
        conn.execute(
            "INSERT INTO ingestions (id, filename, period, status) VALUES (?, ?, ?, 'pending')",
            [upload_id, file.filename, period],
        )

    background_tasks.add_task(
        _run_ingestion_bg,
        settings.duckdb_path,
        storage.local_path(storage_key),
        upload_id,
        period,
        file.filename,
    )
    return UploadResponse(upload_id=upload_id, status="pending")


@app.get("/uploads", response_model=list[IngestionStatus])
def list_uploads() -> list[IngestionStatus]:
    with ro_connection(settings.duckdb_path) as conn:
        rows = conn.execute(
            "SELECT id, filename, period, status, row_count, error "
            "FROM ingestions ORDER BY uploaded_at DESC"
        ).fetchall()
    return [
        IngestionStatus(id=r[0], filename=r[1], period=r[2], status=r[3], row_count=r[4], error=r[5])
        for r in rows
    ]


@app.get("/uploads/{upload_id}", response_model=IngestionStatus)
def get_upload(upload_id: str) -> IngestionStatus:
    with ro_connection(settings.duckdb_path) as conn:
        row = conn.execute(
            "SELECT id, filename, period, status, row_count, error FROM ingestions WHERE id=?",
            [upload_id],
        ).fetchone()
    if row is None:
        raise HTTPException(404, "Upload not found.")
    return IngestionStatus(id=row[0], filename=row[1], period=row[2], status=row[3], row_count=row[4], error=row[5])


@app.get("/periods")
def list_periods() -> list[dict]:
    with ro_connection(settings.duckdb_path) as conn:
        rows = conn.execute(
            "SELECT period, COUNT(*) AS row_count, COUNT(DISTINCT entity_code) AS entity_count "
            "FROM ctb_data GROUP BY period ORDER BY period"
        ).fetchall()
    return [{"period": r[0], "row_count": r[1], "entity_count": r[2]} for r in rows]


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
