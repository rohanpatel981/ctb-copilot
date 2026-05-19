"""Tests for the sync orchestrator. Uses a real DuckDB on a temp path
and a fake DatabaseSource so no live DocumentDB is needed."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any, Iterator

import pytest

from ctb_copilot.db import init_db, ro_connection, rw_connection
from ctb_copilot.ports.source import DatabaseSource, SyncProgress
from ctb_copilot.sync import SyncError, run_sync
from ctb_copilot.tenants import TenantSync


# ---------- helpers ----------


def _tenant(client: str = "client-A", fy: str = "FY 2024-25") -> TenantSync:
    return TenantSync(
        client_id=client,
        gaap_id="ind_as",
        reporting_parent_company_id=f"rpc-{client}",
        fin_year_id=fy,
        reporting_period_id="Annual",
        currency_id="INR",
    )


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


def _register_pending(db_path: Path, sync_id: str, tenant: TenantSync) -> None:
    with rw_connection(db_path) as conn:
        td = tenant.as_dict()
        conn.execute(
            "INSERT INTO ingestions (id, filename, status, source_type, "
            "client_id, gaap_id, reporting_parent_company_id, fin_year_id, "
            "reporting_period_id, currency_id) "
            "VALUES (?, '<sync>', 'pending', 'docdb', ?, ?, ?, ?, ?, ?)",
            [sync_id, td["client_id"], td["gaap_id"], td["reporting_parent_company_id"],
             td["fin_year_id"], td["reporting_period_id"], td["currency_id"]],
        )


# ---------- happy path ----------


def test_sync_happy_path_inserts_rows_and_marks_done(db_path: Path) -> None:
    t = _tenant()
    _register_pending(db_path, "s1", t)
    rows = [_good_row(i) for i in range(25)]
    src = FakeSource(batches=[rows[:10], rows[10:20], rows[20:]])

    n = run_sync(sync_id="s1", source=src, filter_doc={}, tenant=t, db_path=db_path)
    assert n == 25

    with ro_connection(db_path) as conn:
        # Tenant filter is the source of truth now (no `period` column)
        assert conn.execute(
            "SELECT COUNT(*) FROM ctb_data WHERE client_id=? AND fin_year_id=?",
            [t.client_id, t.fin_year_id],
        ).fetchone()[0] == 25
        assert conn.execute("SELECT COUNT(*) FROM ctb_data_staging WHERE upload_id='s1'").fetchone()[0] == 0
        status, row_count = conn.execute("SELECT status, row_count FROM ingestions WHERE id='s1'").fetchone()
        assert status == "done"
        assert row_count == 25


def test_sync_progress_callback_updates_row_count_during_streaming(db_path: Path) -> None:
    t = _tenant()
    _register_pending(db_path, "s2", t)
    rows = [_good_row(i) for i in range(30)]
    src = FakeSource(batches=[rows[:10], rows[10:20], rows[20:]])

    run_sync(sync_id="s2", source=src, filter_doc={}, tenant=t, db_path=db_path)

    with ro_connection(db_path) as conn:
        assert conn.execute("SELECT row_count FROM ingestions WHERE id='s2'").fetchone()[0] == 30


# ---------- atomic swap, scoped to the 6-tuple ----------


def test_sync_replaces_only_same_tenant_tuple(db_path: Path) -> None:
    """Re-syncing tenant A must NOT touch tenant B's rows, even when both
    live in the same ctb_data table. This is the core multi-tenant
    guarantee."""
    t_a = _tenant(client="client-A", fy="FY 2024-25")
    t_b = _tenant(client="client-B", fy="FY 2024-25")

    _register_pending(db_path, "a-first", t_a)
    run_sync(sync_id="a-first", source=FakeSource([[_good_row(i) for i in range(5)]]),
             filter_doc={}, tenant=t_a, db_path=db_path)
    _register_pending(db_path, "b-first", t_b)
    run_sync(sync_id="b-first", source=FakeSource([[_good_row(i) for i in range(7)]]),
             filter_doc={}, tenant=t_b, db_path=db_path)

    with ro_connection(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM ctb_data WHERE client_id='client-A'").fetchone()[0] == 5
        assert conn.execute("SELECT COUNT(*) FROM ctb_data WHERE client_id='client-B'").fetchone()[0] == 7

    # Re-sync tenant A with different row count
    _register_pending(db_path, "a-second", t_a)
    run_sync(sync_id="a-second", source=FakeSource([[_good_row(i) for i in range(9)]]),
             filter_doc={}, tenant=t_a, db_path=db_path)

    with ro_connection(db_path) as conn:
        # Tenant A replaced from 5 → 9
        assert conn.execute("SELECT COUNT(*) FROM ctb_data WHERE client_id='client-A'").fetchone()[0] == 9
        # Tenant B untouched
        assert conn.execute("SELECT COUNT(*) FROM ctb_data WHERE client_id='client-B'").fetchone()[0] == 7
        # Tenant A's earlier ingestion is now 'replaced'
        replaced = conn.execute(
            "SELECT COUNT(*) FROM ingestions WHERE status='replaced' AND client_id='client-A'"
        ).fetchone()[0]
        assert replaced == 1


def test_sync_different_fin_years_coexist_under_same_client(db_path: Path) -> None:
    t1 = _tenant(client="client-A", fy="FY 2024-25")
    t2 = _tenant(client="client-A", fy="FY 2025-26")

    _register_pending(db_path, "y24", t1)
    run_sync(sync_id="y24", source=FakeSource([[_good_row(i) for i in range(3)]]),
             filter_doc={}, tenant=t1, db_path=db_path)
    _register_pending(db_path, "y25", t2)
    run_sync(sync_id="y25", source=FakeSource([[_good_row(i) for i in range(4)]]),
             filter_doc={}, tenant=t2, db_path=db_path)

    with ro_connection(db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM ctb_data WHERE client_id='client-A' AND fin_year_id='FY 2024-25'"
        ).fetchone()[0] == 3
        assert conn.execute(
            "SELECT COUNT(*) FROM ctb_data WHERE client_id='client-A' AND fin_year_id='FY 2025-26'"
        ).fetchone()[0] == 4
        # Combined across years for cross-period queries
        assert conn.execute(
            "SELECT COUNT(*) FROM ctb_data WHERE client_id='client-A'"
        ).fetchone()[0] == 7


# ---------- failure / rollback ----------


def test_sync_rollback_preserves_old_data_when_source_fails_mid_stream(db_path: Path) -> None:
    t = _tenant()
    _register_pending(db_path, "seed", t)
    run_sync(sync_id="seed", source=FakeSource([[_good_row(i) for i in range(3)]]),
             filter_doc={}, tenant=t, db_path=db_path)

    with ro_connection(db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM ctb_data WHERE client_id=? AND fin_year_id=?",
            [t.client_id, t.fin_year_id],
        ).fetchone()[0] == 3

    _register_pending(db_path, "bad", t)
    src = FakeSource(
        batches=[[_good_row(10), _good_row(11)], [_good_row(12)]],
        raise_at_batch=1,
    )

    with pytest.raises(SyncError) as exc_info:
        run_sync(sync_id="bad", source=src, filter_doc={}, tenant=t, db_path=db_path)
    assert "batch 1" in str(exc_info.value)

    with ro_connection(db_path) as conn:
        # Old tenant data untouched
        assert conn.execute(
            "SELECT COUNT(*) FROM ctb_data WHERE client_id=? AND fin_year_id=? AND upload_id='seed'",
            [t.client_id, t.fin_year_id],
        ).fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM ctb_data_staging WHERE upload_id='bad'").fetchone()[0] == 0
        status, error = conn.execute("SELECT status, error FROM ingestions WHERE id='bad'").fetchone()
        assert status == "failed"
        assert error is not None and "batch 1" in error


def test_sync_rollback_when_reconciliation_fails(db_path: Path) -> None:
    t = _tenant()
    _register_pending(db_path, "seed", t)
    run_sync(sync_id="seed", source=FakeSource([[_good_row(i) for i in range(3)]]),
             filter_doc={}, tenant=t, db_path=db_path)

    _register_pending(db_path, "bad-recon", t)
    src = FakeSource(batches=[[_good_row(1), _good_row(2)], [_broken_row()]])

    with pytest.raises(SyncError):
        run_sync(sync_id="bad-recon", source=src, filter_doc={}, tenant=t, db_path=db_path)

    with ro_connection(db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM ctb_data WHERE upload_id='seed'"
        ).fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM ctb_data_staging WHERE upload_id='bad-recon'").fetchone()[0] == 0
        assert conn.execute("SELECT status FROM ingestions WHERE id='bad-recon'").fetchone()[0] == "failed"


# ---------- the build_record helper ----------


def test_build_record_tags_with_all_six_tenant_ids() -> None:
    from ctb_copilot.ingest import _COL_IDX
    from ctb_copilot.sync import _build_record

    t = _tenant(client="client-XYZ", fy="FY 2024-25")
    raw = {"consol_gl_code": "1001", "fs_category": " Assets ", "amount_consolidated": "1234.56"}
    record = _build_record(raw, upload_id="u1", row_number=42, tenant=t)

    assert record[_COL_IDX["upload_id"]] == "u1"
    assert record[_COL_IDX["row_number"]] == 42
    assert record[_COL_IDX["client_id"]] == "client-XYZ"
    assert record[_COL_IDX["gaap_id"]] == "ind_as"
    assert record[_COL_IDX["reporting_parent_company_id"]] == "rpc-client-XYZ"
    assert record[_COL_IDX["fin_year_id"]] == "FY 2024-25"
    assert record[_COL_IDX["reporting_period_id"]] == "Annual"
    assert record[_COL_IDX["currency_id"]] == "INR"
    assert record[_COL_IDX["consol_gl_code"]] == "1001"
    assert record[_COL_IDX["fs_category"]] == "Assets"
    assert record[_COL_IDX["amount_consolidated"]] == 1234.56
    assert len(record) == len(_COL_IDX)
