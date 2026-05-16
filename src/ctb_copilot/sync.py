"""Sync orchestrator — stream rows from a DatabaseSource into DuckDB with
an atomic swap on success.

Flow:
  1. Update ingestions row → status='running'
  2. Stream batches from source; for each batch:
       a. Build canonical 25-col records (add upload_id, row_number, period)
       b. Run reconciliation check (same as Excel ingest)
       c. INSERT batch into ctb_data_staging
       d. Update ingestions.row_count so the UI can show progress
  3. Atomic swap (single transaction):
       a. Mark old ingestion rows for this period as 'replaced'
       b. DELETE old period rows from ctb_data
       c. INSERT into ctb_data SELECT FROM ctb_data_staging (this sync's rows)
       d. DELETE FROM ctb_data_staging (cleanup)
       e. UPDATE ingestions → status='done', completed_at=now
  4. On failure at any step:
       a. ROLLBACK if a transaction is open
       b. DELETE FROM ctb_data_staging WHERE upload_id = this sync
       c. UPDATE ingestions → status='failed', error=<message>
       d. ctb_data is untouched — old period data is preserved

Queries during a long-running sync read from ctb_data only, which still
holds the previous period's data until the swap commits. No torn reads.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import duckdb

from ctb_copilot.ingest import (
    _COL_IDX,
    _INSERT_COLUMNS,
    IngestError,
    _coerce_numeric,
    _coerce_text,
    validate_reconciliation,
)
from ctb_copilot.ingest import TEXT_COLS
from ctb_copilot.ports.source import DatabaseSource, SyncProgress

_STAGING_INSERT_SQL = (
    f"INSERT INTO ctb_data_staging ({', '.join(_INSERT_COLUMNS)}) "
    f"VALUES ({', '.join(['?'] * len(_INSERT_COLUMNS))})"
)

# Canonical fields the source must yield (everything except the three
# provenance columns prepended by the orchestrator).
_CANONICAL_DATA_FIELDS = tuple(name for name in _INSERT_COLUMNS if name not in ("upload_id", "row_number", "period"))


def _build_record(
    row_dict: dict[str, Any],
    *,
    upload_id: str,
    row_number: int,
    period: str,
) -> list[Any]:
    """Convert a canonical-shape source-row dict into the 25-element list
    that matches `_INSERT_COLUMNS` order, applying the same text/number
    coercion the Excel ingest path uses."""
    record: list[Any] = [None] * len(_INSERT_COLUMNS)
    record[_COL_IDX["upload_id"]] = upload_id
    record[_COL_IDX["row_number"]] = row_number
    record[_COL_IDX["period"]] = period
    for field in _CANONICAL_DATA_FIELDS:
        raw = row_dict.get(field)
        if field in TEXT_COLS:
            record[_COL_IDX[field]] = _coerce_text(raw)
        else:
            record[_COL_IDX[field]] = _coerce_numeric(raw)
    return record


class SyncError(Exception):
    """Raised by the orchestrator after the failure path has been written
    back to the ingestions table. Includes how many rows had been staged
    when the failure happened so the API can surface a useful number."""

    def __init__(self, message: str, rows_inserted: int = 0) -> None:
        super().__init__(message)
        self.rows_inserted = rows_inserted


def run_sync(
    *,
    sync_id: str,
    source: DatabaseSource,
    filter_doc: dict[str, Any],
    period: str,
    db_path: Path,
) -> int:
    """Run a sync against the configured source. Returns the number of
    rows inserted. Raises SyncError on failure (after writing the failure
    state to the ingestions table)."""
    conn = duckdb.connect(str(db_path))
    try:
        conn.execute("UPDATE ingestions SET status='running' WHERE id=?", [sync_id])

        rows_seen = {"n": 0}

        def _on_batch(total: int) -> None:
            rows_seen["n"] = total
            conn.execute("UPDATE ingestions SET row_count=? WHERE id=?", [total, sync_id])

        progress = SyncProgress(on_batch=_on_batch)
        row_number_counter = {"n": 1}  # mirror Excel's "row 1 = header" convention

        try:
            for batch_dicts in source.stream_rows(filter_doc=filter_doc, progress=progress):
                if not batch_dicts:
                    continue
                records: list[list[Any]] = []
                for d in batch_dicts:
                    row_number_counter["n"] += 1
                    records.append(_build_record(
                        d,
                        upload_id=sync_id,
                        row_number=row_number_counter["n"],
                        period=period,
                    ))
                validate_reconciliation(records)
                conn.executemany(_STAGING_INSERT_SQL, records)

            # Atomic swap — wrap in a transaction so a failure here doesn't
            # leave ctb_data with the wrong period's data half-written.
            conn.execute("BEGIN TRANSACTION")
            try:
                conn.execute(
                    "UPDATE ingestions SET status='replaced' WHERE period=? AND status='done' AND id != ?",
                    [period, sync_id],
                )
                conn.execute("DELETE FROM ctb_data WHERE period = ?", [period])
                conn.execute(
                    "INSERT INTO ctb_data SELECT * FROM ctb_data_staging WHERE upload_id = ?",
                    [sync_id],
                )
                conn.execute("DELETE FROM ctb_data_staging WHERE upload_id = ?", [sync_id])
                conn.execute(
                    "UPDATE ingestions SET status='done', row_count=?, completed_at=CURRENT_TIMESTAMP WHERE id=?",
                    [rows_seen["n"], sync_id],
                )
                conn.execute("COMMIT")
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except duckdb.TransactionException:
                    pass
                raise

            return rows_seen["n"]

        except Exception as exc:
            # Clean up staging + record the failure. ctb_data is untouched
            # because we never deleted from it outside the (rolled-back) swap.
            try:
                conn.execute("DELETE FROM ctb_data_staging WHERE upload_id = ?", [sync_id])
            except Exception:
                pass
            conn.execute(
                "UPDATE ingestions SET status='failed', error=?, completed_at=CURRENT_TIMESTAMP WHERE id=?",
                [str(exc)[:2000], sync_id],
            )
            raise SyncError(str(exc), rows_inserted=rows_seen["n"]) from exc
    finally:
        conn.close()
