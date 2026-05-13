"""Excel CTB ingestion: parse → validate → normalize → insert into DuckDB.

v1 hardcodes the column layout — the CTB format is fixed per the product contract.
Per-firm column mapping is a v2 problem. If the headers don't match expected
keywords, ingestion fails loudly rather than silently mis-mapping columns.
"""

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


def validate_headers(headers: list[str]) -> None:
    if len(headers) < 22:
        raise IngestError(
            f"Expected at least 22 columns, got {len(headers)}. "
            "Is this a consolidated TB in the expected format?"
        )
    for idx, keyword in HEADER_KEYWORDS:
        actual = (headers[idx] or "").lower()
        if keyword.lower() not in actual:
            raise IngestError(
                f"Header column {idx} expected to contain {keyword!r}, got {headers[idx]!r}. "
                "File schema does not match the expected CTB layout."
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
