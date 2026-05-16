"""DuckDB connection management and schema.

The canonical schema is the single source of truth for what columns text-to-SQL
can target. If you add a column to `ctb_data`, update CANONICAL_DDL here and the
ingest mapping in `ingest.py` in the same change.
"""

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import duckdb

_CTB_DATA_COLUMNS = """
    upload_id              VARCHAR NOT NULL,
    row_number             INTEGER NOT NULL,
    period                 VARCHAR NOT NULL,
    consol_gl_code         VARCHAR,
    consol_gl_description  VARCHAR,
    gl_nature              VARCHAR,
    fs_category            VARCHAR,
    bs_classification      VARCHAR,
    fsli                   VARCHAR,
    grouping               VARCHAR,
    sub_grouping           VARCHAR,
    entity_name            VARCHAR,
    entity_code            VARCHAR,
    functional_currency    VARCHAR,
    amount_functional_ccy  DOUBLE,
    amount_reporting_ccy   DOUBLE,
    adj_other_consolidated DOUBLE,
    adj_nci                DOUBLE,
    adj_goodwill           DOUBLE,
    adj_ppa                DOUBLE,
    adj_intercompany       DOUBLE,
    adj_investment_capital DOUBLE,
    adj_retained_earnings  DOUBLE,
    adj_fctr               DOUBLE,
    amount_consolidated    DOUBLE
"""

CANONICAL_DDL = f"""
CREATE TABLE IF NOT EXISTS ctb_data ({_CTB_DATA_COLUMNS});

CREATE INDEX IF NOT EXISTS idx_ctb_period ON ctb_data(period);
CREATE INDEX IF NOT EXISTS idx_ctb_fs_category ON ctb_data(fs_category);
CREATE INDEX IF NOT EXISTS idx_ctb_entity_code ON ctb_data(entity_code);

-- Sync writes here first; on success an atomic swap moves rows into
-- ctb_data so queries during a long sync see the OLD period's data
-- intact, then flip to the new data only on commit.
CREATE TABLE IF NOT EXISTS ctb_data_staging ({_CTB_DATA_COLUMNS});

CREATE TABLE IF NOT EXISTS ingestions (
    id              VARCHAR PRIMARY KEY,
    filename        VARCHAR,
    period          VARCHAR,
    status          VARCHAR NOT NULL,
    row_count       INTEGER,
    error           VARCHAR,
    uploaded_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at    TIMESTAMP,
    source_type     VARCHAR DEFAULT 'excel',
    source_metadata JSON
);
"""

# Migrations applied to pre-existing databases. DuckDB silently ignores
# columns that already exist, so this is idempotent.
_MIGRATIONS = (
    "ALTER TABLE ingestions ADD COLUMN IF NOT EXISTS source_type VARCHAR DEFAULT 'excel'",
    "ALTER TABLE ingestions ADD COLUMN IF NOT EXISTS source_metadata JSON",
)

# The DDL shown to the LLM (without indexes; columns only). Kept distinct from
# CANONICAL_DDL because the LLM doesn't need to know about indexes or the
# ingestions table.
LLM_SCHEMA_DDL = """
CREATE TABLE ctb_data (
    upload_id              VARCHAR,
    row_number             INTEGER,
    period                 VARCHAR,
    consol_gl_code         VARCHAR,
    consol_gl_description  VARCHAR,
    gl_nature              VARCHAR,
    fs_category            VARCHAR,
    bs_classification      VARCHAR,
    fsli                   VARCHAR,
    grouping               VARCHAR,
    sub_grouping           VARCHAR,
    entity_name            VARCHAR,
    entity_code            VARCHAR,
    functional_currency    VARCHAR,
    amount_functional_ccy  DOUBLE,
    amount_reporting_ccy   DOUBLE,
    adj_other_consolidated DOUBLE,
    adj_nci                DOUBLE,
    adj_goodwill           DOUBLE,
    adj_ppa                DOUBLE,
    adj_intercompany       DOUBLE,
    adj_investment_capital DOUBLE,
    adj_retained_earnings  DOUBLE,
    adj_fctr               DOUBLE,
    amount_consolidated    DOUBLE
);
""".strip()


def init_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with duckdb.connect(str(path)) as conn:
        conn.execute(CANONICAL_DDL)
        for stmt in _MIGRATIONS:
            conn.execute(stmt)


@contextmanager
def rw_connection(path: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    """Read-write connection. Used by ingestion + the ingestions table."""
    conn = duckdb.connect(str(path))
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def ro_connection(path: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    """Read-only connection. Used by the query path as defence-in-depth on top
    of SQL validation — even if a non-SELECT slipped through the parser, the
    DB itself would reject the write."""
    conn = duckdb.connect(str(path), read_only=True)
    try:
        yield conn
    finally:
        conn.close()
