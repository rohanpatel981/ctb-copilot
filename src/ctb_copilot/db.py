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
    upload_id                   VARCHAR NOT NULL,
    row_number                  INTEGER NOT NULL,
    -- Tenant identity. Every row carries all 6 keys so data from
    -- different (client, gaap, parent, year, period, currency) tuples
    -- is structurally non-overlapping. status is implicit ACTIVE
    -- because the sync filter pins it at the source.
    client_id                   VARCHAR NOT NULL,
    gaap_id                     VARCHAR NOT NULL,
    reporting_parent_company_id VARCHAR NOT NULL,
    fin_year_id                 VARCHAR NOT NULL,
    reporting_period_id         VARCHAR NOT NULL,
    currency_id                 VARCHAR NOT NULL,
    -- Human-readable financial-year-period label supplied at sync/upload
    -- time (e.g. 'FY 2024-25', 'FY 2025-26 Q1'). Distinct from the UUID
    -- fin_year_id/reporting_period_id; this is what the LLM uses for
    -- cross-period queries (YoY, multi-year comparison) because UUIDs
    -- pin a single combo while the label can match multiple syncs.
    fin_year_period             VARCHAR,
    -- CTB row content
    consol_gl_code              VARCHAR,
    consol_gl_description       VARCHAR,
    gl_nature                   VARCHAR,
    fs_category                 VARCHAR,
    bs_classification           VARCHAR,
    fsli                        VARCHAR,
    grouping                    VARCHAR,
    sub_grouping                VARCHAR,
    entity_name                 VARCHAR,
    entity_code                 VARCHAR,
    functional_currency         VARCHAR,
    amount_functional_ccy       DOUBLE,
    amount_reporting_ccy        DOUBLE,
    adj_other_consolidated      DOUBLE,
    adj_nci                     DOUBLE,
    adj_goodwill                DOUBLE,
    adj_ppa                     DOUBLE,
    adj_intercompany            DOUBLE,
    adj_investment_capital      DOUBLE,
    adj_retained_earnings       DOUBLE,
    adj_fctr                    DOUBLE,
    amount_consolidated         DOUBLE
"""

CANONICAL_DDL = f"""
CREATE TABLE IF NOT EXISTS ctb_data ({_CTB_DATA_COLUMNS});

-- Composite index on the tenant identity tuple. Every realistic query
-- includes at least 4 of these in its WHERE clause (server-injected),
-- so this index is hit constantly.
CREATE INDEX IF NOT EXISTS idx_ctb_tenant
    ON ctb_data(client_id, gaap_id, reporting_parent_company_id, currency_id);
CREATE INDEX IF NOT EXISTS idx_ctb_fin_year_id ON ctb_data(fin_year_id);
CREATE INDEX IF NOT EXISTS idx_ctb_fin_year_period ON ctb_data(fin_year_period);
CREATE INDEX IF NOT EXISTS idx_ctb_fs_category ON ctb_data(fs_category);
CREATE INDEX IF NOT EXISTS idx_ctb_entity_code ON ctb_data(entity_code);

-- Sync writes here first; on success an atomic swap moves rows into
-- ctb_data so queries during a long sync see the previous tuple's data
-- intact, then flip to the new data only on commit.
CREATE TABLE IF NOT EXISTS ctb_data_staging ({_CTB_DATA_COLUMNS});

CREATE TABLE IF NOT EXISTS ingestions (
    id              VARCHAR PRIMARY KEY,
    filename        VARCHAR,
    status          VARCHAR NOT NULL,
    row_count       INTEGER,
    error           VARCHAR,
    uploaded_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at    TIMESTAMP,
    source_type     VARCHAR DEFAULT 'excel',
    -- All 6 tenant IDs for audit + per-tenant ingestion history.
    -- source_metadata JSON also stores the full filter sent to Mongo.
    client_id                   VARCHAR,
    gaap_id                     VARCHAR,
    reporting_parent_company_id VARCHAR,
    fin_year_id                 VARCHAR,
    reporting_period_id         VARCHAR,
    currency_id                 VARCHAR,
    fin_year_period             VARCHAR,
    source_metadata             JSON
);
"""

# Migrations applied to pre-existing databases. DuckDB ignores ADD COLUMN
# IF NOT EXISTS for columns that already exist, so this is idempotent.
# v0.5 dropped the `period` column on ctb_data + ingestions in favor of
# the 6 tenant IDs. Migration here is destructive on purpose — existing
# rows would be missing the new identity columns and we don't want to
# guess defaults. Tenants re-sync from their source DB after upgrade.
_MIGRATIONS = (
    # Backfill columns from earlier versions (idempotent).
    "ALTER TABLE ingestions ADD COLUMN IF NOT EXISTS source_type VARCHAR DEFAULT 'excel'",
    "ALTER TABLE ingestions ADD COLUMN IF NOT EXISTS source_metadata JSON",
    "ALTER TABLE ingestions ADD COLUMN IF NOT EXISTS client_id VARCHAR",
    "ALTER TABLE ingestions ADD COLUMN IF NOT EXISTS gaap_id VARCHAR",
    "ALTER TABLE ingestions ADD COLUMN IF NOT EXISTS reporting_parent_company_id VARCHAR",
    "ALTER TABLE ingestions ADD COLUMN IF NOT EXISTS fin_year_id VARCHAR",
    "ALTER TABLE ingestions ADD COLUMN IF NOT EXISTS reporting_period_id VARCHAR",
    "ALTER TABLE ingestions ADD COLUMN IF NOT EXISTS currency_id VARCHAR",
    # v0.5 → v0.6: add fin_year_period (human-readable FY label) to all
    # data tables. Idempotent on fresh DBs and existing DBs alike.
    "ALTER TABLE ctb_data ADD COLUMN IF NOT EXISTS fin_year_period VARCHAR",
    "ALTER TABLE ctb_data_staging ADD COLUMN IF NOT EXISTS fin_year_period VARCHAR",
    "ALTER TABLE ingestions ADD COLUMN IF NOT EXISTS fin_year_period VARCHAR",
    # The v0.4 `period` column is gone (replaced by fin_year_id). Drop it
    # from old databases if present. DuckDB raises if the column doesn't
    # exist; init_db catches that below.
)


def _drop_legacy_period_columns(conn) -> None:
    """v0.4 → v0.5 cleanup. Best-effort drops; errors are expected on
    fresh databases that never had the column."""
    import duckdb as _duckdb

    for stmt in (
        "ALTER TABLE ctb_data DROP COLUMN IF EXISTS period",
        "ALTER TABLE ctb_data_staging DROP COLUMN IF EXISTS period",
        "ALTER TABLE ingestions DROP COLUMN IF EXISTS period",
    ):
        try:
            conn.execute(stmt)
        except _duckdb.Error:
            pass


def _maybe_drop_legacy_ctb_data(conn) -> None:
    """v0.4 → v0.5 destructive migration for ctb_data + ctb_data_staging.

    The v0.5 schema requires 6 NOT-NULL tenant ID columns on every row.
    Existing v0.4 rows have no tenant identity and there's no safe way to
    guess one, so the tables are dropped and CREATE TABLE IF NOT EXISTS
    recreates them empty. The customer re-syncs from their source DB.

    Ingestions table is left intact (audit history is preserved); the
    nullable tenant ID columns are added via _MIGRATIONS.
    """
    import duckdb as _duckdb

    for table in ("ctb_data", "ctb_data_staging"):
        try:
            cols = conn.execute(f"PRAGMA table_info({table})").fetchall()
        except _duckdb.Error:
            continue  # table doesn't exist yet — nothing to migrate
        if not cols:
            continue
        column_names = {row[1] for row in cols}
        if "client_id" not in column_names:
            # Legacy v0.4 layout; drop so the new CREATE picks up the
            # tenant-aware schema.
            conn.execute(f"DROP TABLE IF EXISTS {table}")

# The DDL shown to the LLM (without indexes; columns only). Kept distinct from
# CANONICAL_DDL because the LLM doesn't need to know about indexes or the
# ingestions table.
LLM_SCHEMA_DDL = """
CREATE TABLE ctb_data (
    upload_id                   VARCHAR,
    row_number                  INTEGER,
    -- Tenant identity (server-enforced filters; the LLM will see these
    -- already constrained in the WHERE clause). fin_year_id is the
    -- canonical "which financial year" key; reporting_period_id is the
    -- intra-year cadence ('Annual', 'Q4', ...). Both can vary in a
    -- single query (e.g., YoY) when not pinned by the scope.
    client_id                   VARCHAR,
    gaap_id                     VARCHAR,
    reporting_parent_company_id VARCHAR,
    fin_year_id                 VARCHAR,
    reporting_period_id         VARCHAR,
    currency_id                 VARCHAR,
    consol_gl_code              VARCHAR,
    consol_gl_description       VARCHAR,
    gl_nature                   VARCHAR,
    fs_category                 VARCHAR,
    bs_classification           VARCHAR,
    fsli                        VARCHAR,
    grouping                    VARCHAR,
    sub_grouping                VARCHAR,
    entity_name                 VARCHAR,
    entity_code                 VARCHAR,
    functional_currency         VARCHAR,
    amount_functional_ccy       DOUBLE,
    amount_reporting_ccy        DOUBLE,
    adj_other_consolidated      DOUBLE,
    adj_nci                     DOUBLE,
    adj_goodwill                DOUBLE,
    adj_ppa                     DOUBLE,
    adj_intercompany            DOUBLE,
    adj_investment_capital      DOUBLE,
    adj_retained_earnings       DOUBLE,
    adj_fctr                    DOUBLE,
    amount_consolidated         DOUBLE
);
""".strip()


def init_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with duckdb.connect(str(path)) as conn:
        # Drop legacy v0.4 ctb_data + staging tables BEFORE CREATE runs,
        # so the CREATE IF NOT EXISTS picks up the new tenant-aware schema.
        _maybe_drop_legacy_ctb_data(conn)
        conn.execute(CANONICAL_DDL)
        for stmt in _MIGRATIONS:
            conn.execute(stmt)
        _drop_legacy_period_columns(conn)


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
    """Read connection used by query, list, and status endpoints.

    Historically opened with read_only=True as defence-in-depth on top of the
    sqlglot SELECT-only validator. DuckDB, however, refuses to open a RO
    connection while any RW connection (or its in-process metadata cache) is
    live on the same file:

        ConnectionException: Can't open a connection to same database file
        with a different configuration than existing connections

    This collides with the sync background task, which holds a RW connection
    for the duration of a sync, so every UI poll during a sync 500s. Drop the
    flag and rely on the SELECT-only validator in `query.py` for write safety.
    """
    conn = duckdb.connect(str(path))
    try:
        yield conn
    finally:
        conn.close()
