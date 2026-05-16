"""Tests for the sync orchestrator. Uses a real DuckDB on a temp path
and a fake DatabaseSource so no live DocumentDB is needed."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any, Iterator

import pytest

from ctb_copilot.db import init_db, ro_connection, rw_connection
from ctb_copilot.ingest import IngestError, ingest_file
from ctb_copilot.ports.source import DatabaseSource, SyncProgress
from ctb_copilot.sync import SyncError, run_sync


# ---------- helpers ----------


def _good_row(idx: int) -> dict[str, Any]:
    """A canonical-shape row that satisfies the reconciliation invariant."""
    reporting = float(idx * 100)
    intercompany = float(-idx * 10)
    consolidated = reporting + intercompany
    return {
        "consol_gl_code": f"acct-{idx}",
        "consol_gl_description": f"Account {idx}",
        "entity_name": "GCC1",
        "entity_code": "GCC1",
        "gl_nature": "Balance Sheet",
        "fs_category": "Assets",
        "bs_classification": "Current assets",
        "fsli": "Cash",
        "grouping": "g",
        "sub_grouping": "sg",
        "functional_currency": "INR",
        "amount_functional_ccy": reporting,
        "amount_reporting_ccy": reporting,
        "adj_other_consolidated": 0.0,
        "adj_nci": 0.0,
        "adj_goodwill": 0.0,
        "adj_ppa": 0.0,
        "adj_intercompany": intercompany,
        "adj_investment_capital": 0.0,
        "adj_retained_earnings": 0.0,
        "adj_fctr": 0.0,
        "amount_consolidated": consolidated,
    }


def _broken_row() -> dict[str, Any]:
    """A row where amount_consolidated doesn't reconcile with its components."""
    r = _good_row(99)
    r["amount_consolidated"] = 999_999.99  # not what reporting + adjustments sum to
    return r


class FakeSource(DatabaseSource):
    """Yields pre-built batches synchronously. Optionally raises mid-stream."""

    def __init__(self, batches: list[list[dict[str, Any]]], raise_at_batch: int | None = None) -> None:
        self.batches = batches
        self.raise_at_batch = raise_at_batch

    def stream_rows(
        self,
        *,
        filter_doc: dict[str, Any],
        batch_size: int = 10000,
        progress: SyncProgress | None = None,
    ) -> Iterator[list[dict[str, Any]]]:
        for i, batch in enumerate(self.batches):
            if self.raise_at_batch is not None and i == self.raise_at_batch:
                raise ConnectionError(f"simulated source failure at batch {i}")
            if progress is not None:
                progress.report(len(batch))
            yield batch


@pytest.fixture
def db_path() -> Path:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "test.duckdb"
        init_db(path)
        yield path


def _register_pending(db_path: Path, sync_id: str, period: str) -> None:
    with rw_connection(db_path) as conn:
        conn.execute(
            "INSERT INTO ingestions (id, filename, period, status, source_type) VALUES (?, ?, ?, 'pending', 'docdb')",
            [sync_id, "<sync>", period],
        )


# ---------- happy path ----------


def test_sync_happy_path_inserts_rows_and_marks_done(db_path: Path) -> None:
    _register_pending(db_path, sync_id="s1", period="FY 2024-25")
    rows = [_good_row(i) for i in range(25)]
    src = FakeSource(batches=[rows[:10], rows[10:20], rows[20:]])

    n = run_sync(sync_id="s1", source=src, filter_doc={}, period="FY 2024-25", db_path=db_path)
    assert n == 25

    with ro_connection(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM ctb_data WHERE period='FY 2024-25'").fetchone()[0] == 25
        # Staging should be empty after the swap
        assert conn.execute("SELECT COUNT(*) FROM ctb_data_staging WHERE upload_id='s1'").fetchone()[0] == 0
        status, row_count = conn.execute("SELECT status, row_count FROM ingestions WHERE id='s1'").fetchone()
        assert status == "done"
        assert row_count == 25


def test_sync_progress_callback_updates_row_count_during_streaming(db_path: Path) -> None:
    _register_pending(db_path, sync_id="s2", period="FY 2024-25")
    rows = [_good_row(i) for i in range(30)]
    src = FakeSource(batches=[rows[:10], rows[10:20], rows[20:]])

    run_sync(sync_id="s2", source=src, filter_doc={}, period="FY 2024-25", db_path=db_path)

    # Final row_count after success should equal the total streamed
    with ro_connection(db_path) as conn:
        assert conn.execute("SELECT row_count FROM ingestions WHERE id='s2'").fetchone()[0] == 30


# ---------- atomic swap semantics ----------


def test_sync_replaces_previous_period_data_atomically(db_path: Path) -> None:
    # Pre-seed an earlier Excel-style upload for the same period
    sample = Path("data/sample-ctb.xlsx")
    if sample.exists():
        ingest_file(db_path, sample, period="FY 2024-25", original_filename="sample-ctb.xlsx")
    else:
        # Fallback: pre-seed with synthetic rows via the sync path itself
        _register_pending(db_path, sync_id="prev", period="FY 2024-25")
        old_rows = [_good_row(i) for i in range(5)]
        run_sync(sync_id="prev", source=FakeSource([old_rows]), filter_doc={}, period="FY 2024-25", db_path=db_path)

    with ro_connection(db_path) as conn:
        old_count = conn.execute("SELECT COUNT(*) FROM ctb_data WHERE period='FY 2024-25'").fetchone()[0]
    assert old_count > 0

    # Now sync fresh data for the same period
    _register_pending(db_path, sync_id="new", period="FY 2024-25")
    new_rows = [_good_row(i) for i in range(7)]
    n = run_sync(sync_id="new", source=FakeSource([new_rows]), filter_doc={}, period="FY 2024-25", db_path=db_path)
    assert n == 7

    with ro_connection(db_path) as conn:
        # ctb_data now reflects ONLY the new sync
        new_count = conn.execute(
            "SELECT COUNT(*) FROM ctb_data WHERE period='FY 2024-25' AND upload_id='new'"
        ).fetchone()[0]
        assert new_count == 7
        total = conn.execute("SELECT COUNT(*) FROM ctb_data WHERE period='FY 2024-25'").fetchone()[0]
        assert total == 7
        # Previous ingestions for the period are flagged 'replaced'
        replaced = conn.execute(
            "SELECT COUNT(*) FROM ingestions WHERE period='FY 2024-25' AND status='replaced'"
        ).fetchone()[0]
        assert replaced >= 1


# ---------- failure / rollback ----------


def test_sync_rollback_preserves_old_data_when_source_fails_mid_stream(db_path: Path) -> None:
    # Seed an existing period via Excel ingest, then attempt a sync that fails partway
    _register_pending(db_path, sync_id="seed", period="FY 2024-25")
    seed_rows = [_good_row(i) for i in range(3)]
    run_sync(sync_id="seed", source=FakeSource([seed_rows]), filter_doc={}, period="FY 2024-25", db_path=db_path)

    with ro_connection(db_path) as conn:
        before = conn.execute("SELECT COUNT(*) FROM ctb_data WHERE period='FY 2024-25'").fetchone()[0]
    assert before == 3

    # Source fails on the 2nd batch
    _register_pending(db_path, sync_id="bad", period="FY 2024-25")
    src = FakeSource(
        batches=[[_good_row(10), _good_row(11)], [_good_row(12)]],
        raise_at_batch=1,
    )

    with pytest.raises(SyncError) as exc_info:
        run_sync(sync_id="bad", source=src, filter_doc={}, period="FY 2024-25", db_path=db_path)
    assert "batch 1" in str(exc_info.value)

    with ro_connection(db_path) as conn:
        # Old period data untouched
        after = conn.execute(
            "SELECT COUNT(*) FROM ctb_data WHERE period='FY 2024-25' AND upload_id='seed'"
        ).fetchone()[0]
        assert after == 3
        # Failed sync left no orphan rows in staging
        leftover = conn.execute("SELECT COUNT(*) FROM ctb_data_staging WHERE upload_id='bad'").fetchone()[0]
        assert leftover == 0
        # Failed sync row reflects the failure
        status, error = conn.execute("SELECT status, error FROM ingestions WHERE id='bad'").fetchone()
        assert status == "failed"
        assert error is not None and "batch 1" in error


def test_sync_rollback_when_reconciliation_fails(db_path: Path) -> None:
    # Existing period has 3 rows from a prior good sync
    _register_pending(db_path, sync_id="seed", period="FY 2024-25")
    run_sync(sync_id="seed", source=FakeSource([[_good_row(i) for i in range(3)]]),
             filter_doc={}, period="FY 2024-25", db_path=db_path)

    # New sync contains a broken row in batch 1 — reconciliation will reject
    _register_pending(db_path, sync_id="bad-recon", period="FY 2024-25")
    src = FakeSource(batches=[[_good_row(1), _good_row(2)], [_broken_row()]])

    with pytest.raises(SyncError):
        run_sync(sync_id="bad-recon", source=src, filter_doc={}, period="FY 2024-25", db_path=db_path)

    with ro_connection(db_path) as conn:
        # Old data still there
        assert conn.execute(
            "SELECT COUNT(*) FROM ctb_data WHERE period='FY 2024-25' AND upload_id='seed'"
        ).fetchone()[0] == 3
        # Even the good first batch was rolled out of staging
        assert conn.execute("SELECT COUNT(*) FROM ctb_data_staging WHERE upload_id='bad-recon'").fetchone()[0] == 0
        status = conn.execute("SELECT status FROM ingestions WHERE id='bad-recon'").fetchone()[0]
        assert status == "failed"


# ---------- the build_record helper ----------


def test_build_record_drops_unknown_fields_and_coerces() -> None:
    from ctb_copilot.sync import _build_record

    raw = {
        "consol_gl_code": "1001",
        "fs_category": " Assets ",         # whitespace gets trimmed
        "amount_consolidated": "1234.56",   # string number gets coerced to float
        "_id": "mongo-id-shouldnt-leak",
        "extra_field": "ignored",
    }
    record = _build_record(raw, upload_id="u1", row_number=42, period="FY 2024-25")

    from ctb_copilot.ingest import _COL_IDX
    assert record[_COL_IDX["upload_id"]] == "u1"
    assert record[_COL_IDX["row_number"]] == 42
    assert record[_COL_IDX["period"]] == "FY 2024-25"
    assert record[_COL_IDX["consol_gl_code"]] == "1001"
    assert record[_COL_IDX["fs_category"]] == "Assets"  # trimmed
    assert record[_COL_IDX["amount_consolidated"]] == 1234.56
    # Missing canonical fields → None
    assert record[_COL_IDX["adj_nci"]] is None
    # length matches the canonical layout
    assert len(record) == len(_COL_IDX)
