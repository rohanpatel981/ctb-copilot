"""Tests for the DocumentDB source adapter — focused on filter merging
and the row projector. The pymongo cursor itself is tested via mocks so
no live DocumentDB is needed."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

from ctb_copilot.adapters.source_docdb import (
    BATCH_SIZE,
    CANONICAL_FIELDS,
    DocumentDBSource,
    _project_doc,
    build_filter,
)
from ctb_copilot.ports.source import SyncProgress


# ---------- build_filter ----------


def test_build_filter_minimum_just_period() -> None:
    f = build_filter(
        default_filter={},
        period="FY 2024-25",
        period_field="fin_year_id",
    )
    assert f == {"fin_year_id": "FY 2024-25"}


def test_build_filter_merges_static_defaults() -> None:
    f = build_filter(
        default_filter={"client_id": "abc", "status": "ACTIVE"},
        period="FY 2024-25",
        period_field="fin_year_id",
    )
    assert f == {"client_id": "abc", "status": "ACTIVE", "fin_year_id": "FY 2024-25"}


def test_build_filter_adds_reporting_period_when_configured() -> None:
    f = build_filter(
        default_filter={"client_id": "abc"},
        period="FY 2024-25",
        period_field="fin_year_id",
        reporting_period="Annual",
        reporting_period_field="reporting_period_id",
    )
    assert f["reporting_period_id"] == "Annual"


def test_build_filter_skips_reporting_period_when_field_missing() -> None:
    f = build_filter(
        default_filter={},
        period="FY 2024-25",
        period_field="fin_year_id",
        reporting_period="Annual",
        reporting_period_field=None,
    )
    assert "Annual" not in f.values()


def test_build_filter_overrides_win_over_default_and_period() -> None:
    f = build_filter(
        default_filter={"client_id": "default_client"},
        period="FY 2024-25",
        period_field="fin_year_id",
        overrides={"client_id": "override_client", "extra_field": "x"},
    )
    assert f["client_id"] == "override_client"
    assert f["extra_field"] == "x"
    assert f["fin_year_id"] == "FY 2024-25"


def test_build_filter_does_not_mutate_inputs() -> None:
    base = {"client_id": "abc"}
    overrides = {"x": 1}
    build_filter(
        default_filter=base,
        period="FY 2024-25",
        period_field="fin_year_id",
        overrides=overrides,
    )
    assert base == {"client_id": "abc"}
    assert overrides == {"x": 1}


def test_build_filter_supports_mongo_operators_passthrough() -> None:
    f = build_filter(
        default_filter={"amount": {"$gte": 1000}, "client_id": {"$in": ["a", "b"]}},
        period="FY 2024-25",
        period_field="fin_year_id",
    )
    assert f["amount"] == {"$gte": 1000}
    assert f["client_id"] == {"$in": ["a", "b"]}


# ---------- _project_doc ----------


def test_project_doc_keeps_only_canonical_fields() -> None:
    raw = {"_id": "mongo-id", "consol_gl_code": "1001", "client_id": "abc", "extra": "ignored"}
    projected = _project_doc(raw)
    assert "_id" not in projected
    assert "client_id" not in projected
    assert "extra" not in projected
    assert projected["consol_gl_code"] == "1001"


def test_project_doc_fills_missing_canonical_fields_with_none() -> None:
    raw = {"consol_gl_code": "1001"}
    projected = _project_doc(raw)
    assert set(projected.keys()) == set(CANONICAL_FIELDS)
    assert projected["amount_consolidated"] is None
    assert projected["fs_category"] is None


def test_canonical_fields_has_all_22() -> None:
    assert len(CANONICAL_FIELDS) == 22


# ---------- DocumentDBSource.stream_rows (mocked pymongo) ----------


def _mock_mongo_returning(docs: list[dict[str, Any]]):
    """Build a MongoClient mock whose cursor yields the given docs."""
    mock_collection = MagicMock()
    mock_collection.find.return_value = iter(docs)
    mock_db = MagicMock()
    mock_db.__getitem__.return_value = mock_collection
    mock_client = MagicMock()
    mock_client.__getitem__.return_value = mock_db
    return mock_client, mock_collection


@patch("ctb_copilot.adapters.source_docdb.MongoClient")
def test_stream_rows_yields_batched_canonical_dicts(mock_mongo_client_cls) -> None:
    docs = [
        {
            "_id": f"id-{i}",
            "consol_gl_code": f"acct-{i}",
            "fs_category": "Assets",
            "amount_consolidated": float(i),
        }
        for i in range(25)
    ]
    mock_client, _ = _mock_mongo_returning(docs)
    mock_mongo_client_cls.return_value = mock_client

    src = DocumentDBSource(uri="mongodb://x", database="db", collection="coll")
    batches = list(src.stream_rows(filter_doc={"x": 1}, batch_size=10))

    assert len(batches) == 3
    assert len(batches[0]) == 10
    assert len(batches[1]) == 10
    assert len(batches[2]) == 5
    assert batches[0][0]["consol_gl_code"] == "acct-0"
    assert "_id" not in batches[0][0]


@patch("ctb_copilot.adapters.source_docdb.MongoClient")
def test_stream_rows_invokes_progress_callback(mock_mongo_client_cls) -> None:
    docs = [{"consol_gl_code": str(i)} for i in range(15)]
    mock_client, _ = _mock_mongo_returning(docs)
    mock_mongo_client_cls.return_value = mock_client

    seen = []
    progress = SyncProgress(on_batch=lambda n: seen.append(n))

    src = DocumentDBSource(uri="mongodb://x", database="db", collection="coll")
    list(src.stream_rows(filter_doc={}, batch_size=10, progress=progress))

    # After two batches (10 + 5), progress should have been reported twice
    # with cumulative counts.
    assert seen == [10, 15]


@patch("ctb_copilot.adapters.source_docdb.MongoClient")
def test_stream_rows_closes_client(mock_mongo_client_cls) -> None:
    docs: list[dict[str, Any]] = [{"consol_gl_code": "1"}]
    mock_client, _ = _mock_mongo_returning(docs)
    mock_mongo_client_cls.return_value = mock_client

    src = DocumentDBSource(uri="mongodb://x", database="db", collection="coll")
    list(src.stream_rows(filter_doc={}, batch_size=10))

    mock_client.close.assert_called_once()


@patch("ctb_copilot.adapters.source_docdb.MongoClient")
def test_stream_rows_default_batch_size_is_10000(mock_mongo_client_cls) -> None:
    assert BATCH_SIZE == 10_000

    mock_client, mock_collection = _mock_mongo_returning([])
    mock_mongo_client_cls.return_value = mock_client

    src = DocumentDBSource(uri="mongodb://x", database="db", collection="coll")
    list(src.stream_rows(filter_doc={"x": 1}))

    args, kwargs = mock_collection.find.call_args
    assert kwargs.get("batch_size") == 10_000 or (len(args) >= 2 and args[1] == 10_000)
