"""Excel CTB ingestion: parse → validate → normalize → insert into DuckDB.

v1 hardcodes the column layout — the CTB format is fixed per the product contract.
Per-firm column mapping is a v2 problem. If the headers don't match expected
keywords, ingestion fails loudly rather than silently mis-mapping columns.
"""

import math
import uuid
from pathlib import Path

from python_calamine import CalamineWorkbook

from ctb_copilot.db import rw_connection

COLUMN_MAP: list[tuple[int, str]] = [
    (0, "consol_gl_code"),
    (1, "consol_gl_description"),
    (2, "entity_name"),
    (3, "entity_code"),
    (4, "gl_nature"),
    (5, "fs_category"),
    (6, "bs_classification"),
    (7, "fsli"),
    (8, "grouping"),
    (9, "sub_grouping"),
    (10, "functional_currency"),
    (11, "amount_functional_ccy"),
    (12, "amount_reporting_ccy"),
    (13, "adj_other_consolidated"),
    (14, "adj_nci"),
    (15, "adj_goodwill"),
    (16, "adj_ppa"),
    (17, "adj_intercompany"),
    (18, "adj_investment_capital"),
    (19, "adj_retained_earnings"),
    (20, "adj_fctr"),
    (21, "amount_consolidated"),
]

HEADER_KEYWORDS: list[tuple[int, str]] = [
    (0, "consol gl code"),
    (1, "description"),
    (2, "entity name"),
    (3, "entity code"),
    (4, "nature"),
    (5, "fs category"),
    (6, "classification"),
    (7, "fsli"),
    (8, "grouping"),
    (10, "functional currency"),
    (13, "consolidated adjustments"),
    (14, "non controlling"),
    (15, "goodwill"),
    (21, "consolidation currency"),
]

TEXT_COLS = {
    "consol_gl_code", "consol_gl_description", "entity_name", "entity_code",
    "gl_nature", "fs_category", "bs_classification", "fsli", "grouping",
    "sub_grouping", "functional_currency",
}


class IngestError(Exception):
    pass


def _coerce_text(v) -> str | None:
    if v is None or v == "":
        return None
    text = str(v).strip()
    return text or None


def _coerce_numeric(v) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


_AMOUNT_KEYWORDS = (
    "amount in functional",
    "amount in consolidation",
    "amount in reporting",
    "debit",
    "credit",
    "closing balance",
)
_ADJUSTMENT_KEYWORDS = (
    "consolidated adjustments",
    "non controlling",
    "intercompany elimination",
    "purchase price allocation",
)


def _detect_artifact_type(headers: list[str]) -> str:
    """Heuristic classification of what an Excel file probably is.

    Used to turn schema mismatches into informative error messages so users
    know *why* their file was rejected and what to upload instead.
    """
    joined = " | ".join((h or "").lower() for h in headers)
    has_amounts = any(kw in joined for kw in _AMOUNT_KEYWORDS)
    has_adjustments = any(kw in joined for kw in _ADJUSTMENT_KEYWORDS)
    has_entity_gl_code = "entity gl code" in joined

    if has_entity_gl_code and not has_amounts:
        return "mapping_table"
    if has_amounts and not has_adjustments:
        return "entity_trial_balance"
    if has_amounts and has_adjustments:
        return "ctb_variant"
    return "unknown"


def validate_headers(headers: list[str]) -> None:
    artifact = _detect_artifact_type(headers)

    if len(headers) < 22:
        if artifact == "mapping_table":
            raise IngestError(
                f"This looks like a chart-of-accounts mapping table ({len(headers)} columns; no amount columns detected). "
                "Mapping tables describe how entity-level accounts map to consolidated codes — useful as reference, but "
                "they don't contain the numbers needed for Q&A. ctb-copilot ingests the consolidated trial balance with "
                "amounts (typically a 22-column 'Consolidated TB - Detailed' export). Did you mean to upload that?"
            )
        if artifact == "entity_trial_balance":
            raise IngestError(
                f"This looks like an entity-level trial balance ({len(headers)} columns; has amount columns but no "
                "consolidation adjustments). ctb-copilot ingests the *consolidated* TB — the output of the consolidation "
                "process, which adds columns for NCI, goodwill, intercompany eliminations, FCTR, etc. (22 columns total). "
                "Did you mean to upload the consolidated TB instead?"
            )
        raise IngestError(
            f"Expected at least 22 columns in the Consolidated TB layout, got {len(headers)}. "
            "ctb-copilot ingests the consolidated trial balance ('Consolidated TB - Detailed' export). "
            "See the README → 'Expected CTB format' for the canonical 22-column layout. "
            "Support for variant CTB shapes is on the roadmap."
        )

    for idx, keyword in HEADER_KEYWORDS:
        actual = (headers[idx] or "").lower()
        if keyword.lower() not in actual:
            raise IngestError(
                f"Column {idx + 1} (header: {headers[idx]!r}) doesn't match the expected CTB layout — "
                f"expected a column containing {keyword!r}. The file has the right number of columns "
                "but their order or names differ. See README → 'Expected CTB format'."
            )


def _parse(path: Path) -> tuple[list[str], list[list]]:
    wb = CalamineWorkbook.from_path(str(path))
    sheet = wb.get_sheet_by_name(wb.sheet_names[0])
    rows = sheet.to_python()
    if not rows:
        raise IngestError("File contains no rows.")
    headers = [str(h) if h is not None else "" for h in rows[0]]
    return headers, rows[1:]


_INSERT_COLUMNS = [
    "upload_id", "row_number", "period",
    *[canonical for _, canonical in COLUMN_MAP],
]
_INSERT_SQL = (
    f"INSERT INTO ctb_data ({', '.join(_INSERT_COLUMNS)}) "
    f"VALUES ({', '.join(['?'] * len(_INSERT_COLUMNS))})"
)
_COL_IDX = {name: i for i, name in enumerate(_INSERT_COLUMNS)}

# Columns that should sum to amount_consolidated per row. Excludes
# amount_functional_ccy because it's in the entity's own currency and would
# mix currencies if rolled up.
_RECONCILIATION_COMPONENTS = (
    "amount_reporting_ccy",
    "adj_other_consolidated",
    "adj_nci",
    "adj_goodwill",
    "adj_ppa",
    "adj_intercompany",
    "adj_investment_capital",
    "adj_retained_earnings",
    "adj_fctr",
)


def validate_reconciliation(prepared: list[list], *, abs_tol: float = 0.01, rel_tol: float = 1e-9) -> None:
    """Verify that amount_consolidated = sum(_RECONCILIATION_COMPONENTS) for every row.

    The CTB satisfies this invariant by construction. A mismatch usually means
    the file was hand-edited in Excel, the consolidation producer has a bug,
    or the upload was the wrong artifact (caught earlier by header validation).

    Uses math.isclose so floating-point drift on large values is tolerated;
    real arithmetic errors (≥0.01 INR for typical magnitudes) are flagged.
    """
    failures: list[tuple[int, float, float, float]] = []
    component_indices = [_COL_IDX[c] for c in _RECONCILIATION_COMPONENTS]
    actual_idx = _COL_IDX["amount_consolidated"]
    row_num_idx = _COL_IDX["row_number"]

    for record in prepared:
        computed = sum((record[i] or 0.0) for i in component_indices)
        actual = record[actual_idx] or 0.0
        if not math.isclose(actual, computed, abs_tol=abs_tol, rel_tol=rel_tol):
            failures.append((record[row_num_idx], actual, computed, actual - computed))

    if failures:
        examples = "\n  ".join(
            f"Excel row {r}: actual={a:,.2f}, computed={c:,.2f}, diff={d:,.2f}"
            for r, a, c, d in failures[:5]
        )
        more = f"\n  ...and {len(failures) - 5} more failing rows" if len(failures) > 5 else ""
        raise IngestError(
            f"Reconciliation check failed: {len(failures)} of {len(prepared)} rows where "
            f"amount_consolidated != amount_reporting_ccy + Σ(adj_*). "
            "The consolidated TB should satisfy this invariant by construction — a mismatch "
            "usually means the file was hand-edited, the consolidation producer has a bug, "
            "or some columns carry an unexpected meaning. Sample failing rows:\n  "
            f"{examples}{more}"
        )


def ingest_file(
    db_path: Path,
    source_path: Path,
    *,
    period: str,
    upload_id: str | None = None,
    original_filename: str | None = None,
) -> tuple[str, int]:
    """Parse `source_path` and insert into ctb_data under `period`.

    Returns (upload_id, row_count). Raises IngestError on schema mismatch.

    If `upload_id` is provided, assumes the API layer has already inserted an
    ingestions row in 'pending' state (so the user could see status immediately
    on upload). Otherwise creates a fresh row.
    """
    pre_registered = upload_id is not None
    upload_id = upload_id or str(uuid.uuid4())
    filename = original_filename or source_path.name

    with rw_connection(db_path) as conn:
        if pre_registered:
            conn.execute("UPDATE ingestions SET status='reading' WHERE id=?", [upload_id])
        else:
            conn.execute(
                "INSERT INTO ingestions (id, filename, period, status) VALUES (?, ?, ?, 'reading')",
                [upload_id, filename, period],
            )

    try:
        headers, raw_rows = _parse(source_path)
        validate_headers(headers)

        prepared: list[list] = []
        for i, row in enumerate(raw_rows):
            padded = list(row) + [None] * max(0, 22 - len(row))
            record: list = [upload_id, i + 2, period]
            for idx, canonical in COLUMN_MAP:
                raw = padded[idx]
                if canonical in TEXT_COLS:
                    record.append(_coerce_text(raw))
                else:
                    record.append(_coerce_numeric(raw))
            prepared.append(record)

        validate_reconciliation(prepared)

        with rw_connection(db_path) as conn:
            conn.execute("UPDATE ingestions SET status='inserting' WHERE id=?", [upload_id])
            conn.executemany(_INSERT_SQL, prepared)
            conn.execute(
                "UPDATE ingestions SET status='done', row_count=?, completed_at=CURRENT_TIMESTAMP WHERE id=?",
                [len(prepared), upload_id],
            )

        return upload_id, len(prepared)

    except Exception as e:
        with rw_connection(db_path) as conn:
            conn.execute(
                "UPDATE ingestions SET status='failed', error=?, completed_at=CURRENT_TIMESTAMP WHERE id=?",
                [str(e), upload_id],
            )
        raise
