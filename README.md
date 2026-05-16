# ctb-copilot

**Ask plain-English questions about your consolidated trial balance. Get answers with the SQL that produced them and the rows that summed to the total — auditable, verifiable, exportable.**

Built for chartered accountants and finance teams running consolidation across multiple entities, currencies, and financial years. Every answer carries its receipts so the analyst can paste it straight into working papers and the engagement manager can sign off without re-running it from scratch.

> Public preview, v0.3 — ships the DocumentDB sync path so customers with their TB already in AWS DocumentDB (or any Mongo-compatible store) can skip the Excel-upload step. Ingestion guards (header validation, per-row reconciliation), audit trail, Excel export, eval harness, atomic-swap sync, and a 43-test suite all in. Docker / S3 / auth are the next milestone (v1.0).

## What it does, in one example

```
You ▸ "What's the total of current liabilities for FY 2024-25?"

Assistant ▸  ₹ (37.84) Cr   (negative per trial-balance convention)

  ▸ View SQL
      SELECT period, bs_classification, SUM(amount_consolidated) AS total
      FROM ctb_data
      WHERE period = 'FY 2024-25'
        AND bs_classification = 'Current liabilities'
      GROUP BY period, bs_classification;

  ▸ View 247 source rows
      [paginated table — every row that contributed to the total]

  Confidence: 🟢 High

  [ 📄 Export to Excel ]   ← downloads a 3-sheet .xlsx
                            (Answer / SQL / Source Rows)
                            ready for working papers
```

## How a CA firm uses it

### 1. One-time setup (~10 minutes, IT admin)

```sh
git clone https://github.com/rohanpatel981/ctb-copilot
cd ctb-copilot
uv sync                     # installs Python deps via uv
cp .env.example .env
$EDITOR .env                # paste ANTHROPIC_API_KEY
```

Then in two terminals:

```sh
uv run ctb-api              # API on http://localhost:8000
uv run ctb-ui               # UI opens at http://localhost:8501
```

> Docker image + one-command deployment is on the v1.0 roadmap. For now, the project runs as two local processes (FastAPI backend + Streamlit UI).

### 2. Analyst day-to-day

There are two ways to get data into ctb-copilot. Pick the one that matches how the firm already manages their consolidated TBs.

**Path A — Drop in an Excel file** (the default; works for anyone):

1. **Upload a CTB.** Drag the consolidated trial balance Excel file into the upload zone. Pick the financial year from the dropdown (FY 2005-06 through FY 2049-50).
2. **Wait ~30 sec.** The file is parsed locally (`polars` + `python-calamine`), normalized into a canonical schema, and inserted into a local DuckDB database. Progress is visible per upload in the sidebar.
3. **Re-upload to replace.** Uploading a second file with the same FY tag overrides the previous data for that period. Audit trail is preserved (old uploads tagged `replaced`, not deleted from history).
4. **Ask in plain English.** *"YoY change in revenue?"*, *"Goodwill on the consolidated BS?"*, *"Revenue by entity, FY 2024-25"*. Claude translates the question to SQL; the SQL runs locally on DuckDB; the answer comes back with the SQL it ran, the rows it summed, and a confidence rating.
5. **Paste into working papers.** Click *Export to Excel* — downloads a 3-sheet `.xlsx`: Answer (question + confidence + explanation + any YoY/ratio breakdowns), SQL (the exact query that ran), and Source Rows (every row that contributed to the total, headers frozen, columns auto-sized). Drop it into the engagement folder. Done.

**Path B — Sync directly from AWS DocumentDB** (for firms whose TB already lives in a database):

1. **Configure once** by setting `DOCDB_URI`, `DOCDB_DATABASE`, `DOCDB_COLLECTION`, `DOCDB_DEFAULT_FILTER`, and `DOCDB_PERIOD_FIELD` in `.env` (see [Configuration](#configuration)).
2. **In the sidebar** of the UI, the "Sync from DocumentDB" section appears. Pick the FY period, optionally inspect the filter preview, click *Sync now*.
3. The container pulls the matching documents from DocumentDB in batches of 10,000, validates the reconciliation invariant per batch, and writes them to a staging table. Live row-count shows progress.
4. On success, an **atomic swap** replaces the previous period's data with the new sync — queries during the sync still see the old data, then flip to the new data on commit. No torn reads. On failure, staging is cleaned and the old period is untouched.
5. For 1M rows from a same-region DocumentDB instance, expect ~60–90 seconds end to end.

Both paths share the same downstream pipeline — same canonical schema, same reconciliation guard, same sign convention, same audit-trail UI. You can use Excel for ad-hoc files and DocumentDB sync for the production close cadence in the same deployment.

### 3. What makes the answers trustworthy

Every response has three artifacts a CA can verify:

- **The SQL Claude generated**, verbatim. No hidden processing.
- **The source rows** that contributed to the total — every Excel row with original row number and entity, paginated and filterable.
- **Confidence rating** based on whether the question maps cleanly to the schema (🟢 high), required an assumption like rolling up across entities (🟡 medium), or is ambiguous (🔴 low).

There is no black-box step. Two layers of safety also gate the SQL itself: a `sqlglot` parse-tree check that rejects anything that isn't a `SELECT`, plus a read-only DuckDB connection that physically can't write.

A third guardrail runs at **ingestion** time. Every uploaded CTB has to satisfy the reconciliation invariant on every row: `amount_consolidated = amount_reporting_ccy + Σ(adj_*)`. If even one row fails (within `math.isclose` tolerance), the upload is rejected with a list of the offending Excel row numbers and the actual/computed/diff values. Hand-edited CTBs and producer bugs get caught before they can produce wrong Q&A answers.

Files that aren't a consolidated TB at all (mapping tables, entity-level TBs) get a context-rich rejection that names what the file probably is, not a generic "wrong column count" message.

## Where the data lives

This is critical for CA work — client trial-balance data must stay where the firm wants it.

```
CTB Excel  ──parsed locally──>  DuckDB on your machine    (data stays here)
                                       │
                                       ▼
User question  ──>  POST /query (local)
                       │
                       ▼
              Anthropic API:  schema (column names, no data)  +  question text
                       │
                       ▼
                  SQL string returned
                       │
                       ▼
              Runs on local DuckDB ──> rows returned to local UI
```

The only things that leave your machine over the network: the **table schema** (column names, no values) and the **question text**. No row data, no entity names, no amounts. The CTB file lives in `data/uploads/` and the DuckDB database lives at `data/ctb.duckdb` — both on the host where you ran `uv run ctb-api`.

## Expected CTB format

The Excel file must have one sheet with 22 columns matching this layout (the standard "Consolidated TB — Detailed" export):

| # | Column | Used as |
|---:|---|---|
| 1 | Consol GL Code | `consol_gl_code` |
| 2 | Consol GL Description | `consol_gl_description` |
| 3 | Entity Name | `entity_name` |
| 4 | Entity Code | `entity_code` |
| 5 | GL Nature | `gl_nature` (Balance Sheet / Statement of PL) |
| 6 | FS Category | `fs_category` (Assets / Liabilities / Equity / Revenue / Expense / Tax expense / Other Comprehensive) |
| 7 | Balance Sheet Classification | `bs_classification` |
| 8 | FSLI | `fsli` |
| 9 | Grouping | `grouping` |
| 10 | Sub-Grouping | `sub_grouping` |
| 11 | Functional currency | `functional_currency` |
| 12 | Amount in functional currency (entity) | `amount_functional_ccy` |
| 13 | Amount in functional currency (reporting) | `amount_reporting_ccy` |
| 14 | Other Consolidated Adjustments | `adj_other_consolidated` |
| 15 | Non Controlling Interest (NCI) | `adj_nci` |
| 16 | Goodwill | `adj_goodwill` |
| 17 | Purchase Price Allocation (PPA) | `adj_ppa` |
| 18 | Other intercompany eliminations | `adj_intercompany` |
| 19 | Investment, share capital and pre-acquisition | `adj_investment_capital` |
| 20 | Retained Earnings | `adj_retained_earnings` |
| 21 | Foreign currency translation reserve | `adj_fctr` |
| 22 | Amount in consolidation currency | `amount_consolidated` |

A reconciliation invariant holds per row (verified against real CTB data and **enforced at ingestion**): `amount_consolidated = amount_reporting_ccy + Σ(adj_*)`. Files that don't reconcile are rejected with row-level diagnostics. The prompt also encodes the invariant so Claude can decompose any consolidated figure into its components on request.

Trial-balance sign convention is preserved end-to-end: **Assets and Expenses are positive; Liabilities, Equity, and Revenue are negative.** Answers are presented with the convention, not flipped to absolutes — auditors expect it that way.

If a file doesn't match this layout, ingestion fails loudly with a header-mismatch error rather than silently mis-mapping columns. Support for variant CTB layouts (LLM-assisted column detection) is on the roadmap.

## Configuration

Everything is environment-variable driven, twelve-factor style. See `.env.example`.

| Variable | Default | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | (required) | Your Anthropic API key |
| `ANTHROPIC_MODEL` | `claude-opus-4-7` | Claude model used for SQL generation |
| `STORAGE_DIR` | `./data/uploads` | Where uploaded Excel files are stored |
| `DUCKDB_PATH` | `./data/ctb.duckdb` | Path to the DuckDB database file |
| `API_HOST` | `127.0.0.1` | FastAPI bind host |
| `API_PORT` | `8000` | FastAPI port |
| `API_BASE_URL` | `http://127.0.0.1:8000` | How the UI reaches the API |

### DocumentDB sync (optional)

Set these only if you want the sync path. The Excel-upload path works regardless.

| Variable | Default | Purpose |
|---|---|---|
| `DOCDB_URI` | (unset) | Mongo connection string. For AWS DocumentDB include `tls=true&replicaSet=rs0&retryWrites=false`. |
| `DOCDB_DATABASE` | (unset) | DB name inside the cluster |
| `DOCDB_COLLECTION` | (unset) | Collection that holds CTB documents in canonical-schema shape |
| `DOCDB_DEFAULT_FILTER` | `{}` | JSON object — the static filter applied to every sync (e.g. `{"client_id":"abc","gaap_id":"ind_as","status":"ACTIVE"}`) |
| `DOCDB_PERIOD_FIELD` | (unset) | Which field in the source documents carries the FY (e.g. `fin_year_id`). The selected period is injected as `{<field>: <period>}` at sync time. |
| `DOCDB_REPORTING_PERIOD` | (unset) | Optional fixed value like `"Annual"` or `"Q4"` |
| `DOCDB_REPORTING_PERIOD_FIELD` | `reporting_period_id` | Which field that fixed value goes into |

Sync is triggered manually from the UI (or by `POST /sync {"period": "FY 2024-25"}`). For different customers with different schemas, no code change is needed — just set their fields in their `.env`. Per-sync overrides via `filter_override` in the request body let you target specific subsets without changing config.

## Architecture

```
Streamlit UI  ─HTTP─>  FastAPI  ─>  DuckDB (local)  ─>  Claude Opus 4.7
                                                       (text-to-SQL with prompt caching)
```

Three principles:
1. **Auditability over magic.** Every answer ships with the SQL and source rows. No hidden post-processing.
2. **Sign convention preserved.** Trial-balance convention isn't flipped to absolutes; the explanation calls out the sign.
3. **Data residency by default.** No row data leaves the host machine. Only schema + question go to Anthropic.

The codebase uses the ports-and-adapters pattern so the storage backend and LLM provider can swap without touching business logic. Today: `LocalDiskStorage` + `AnthropicLLM`. Roadmap: S3-compatible storage adapter (S3 / R2 / MinIO), Bedrock LLM adapter.

## For developers

```sh
uv sync --extra dev
uv run pytest                    # 43 unit tests (grader + export + sync, no LLM/Mongo needed)
uv run ruff check src/           # lint
uv run ctb-eval                  # run the eval suite (needs ANTHROPIC_API_KEY in .env)
```

Project layout:

```
src/ctb_copilot/
├── config.py              pydantic-settings; reads .env + env vars
├── db.py                  DuckDB schema + connection management (RW + RO)
├── ingest.py              Excel → DuckDB; header detection + reconciliation guard
├── sync.py                DatabaseSource → DuckDB with atomic-swap on success
├── query.py               Orchestrator: LLM → safety-validate → execute → post-process
├── api.py                 FastAPI service
├── ui.py                  Streamlit UI (chat + upload + Excel download + DocDB sync)
├── export.py              QueryResult → 3-sheet .xlsx (Answer / SQL / Source Rows)
├── eval/
│   ├── grader.py          Pure-logic checks against a QueryResult (no LLM/DB)
│   ├── runner.py          Load YAML, run queries, grade, report
│   └── golden.yaml        10 hand-curated Q&A cases against the sample CTB
├── ports/
│   ├── llm.py             LLMProvider Protocol + SQLPlan model
│   ├── storage.py         StorageBackend Protocol
│   └── source.py          DatabaseSource Protocol (sync from upstream DB)
└── adapters/
    ├── llm_anthropic.py   Claude Opus 4.7 with adaptive thinking,
    │                      structured outputs, and prompt caching
    ├── storage_local.py   Local disk implementation
    └── source_docdb.py    AWS DocumentDB (pymongo) — batched cursor + filter merge

tests/
├── test_grader.py         14 tests covering every check type
├── test_export.py          9 tests covering the workbook builder
├── test_source_docdb.py   14 tests for filter merging + batched streaming (mocked pymongo)
└── test_sync.py            6 tests for atomic-swap orchestrator (real DuckDB, fake source)
```

To add an adapter (e.g. S3 storage, Bedrock LLM), implement the Protocol in `ports/` and wire it in `api.py`. Business logic depends on the Protocol, not the concrete class.

### Running the eval suite

```sh
# Prerequisite: ingest the sample CTB under FY 2024-25 (via the UI or directly)
uv run ctb-eval                  # uses src/ctb_copilot/eval/golden.yaml
uv run ctb-eval path/to/cases.yaml   # or pass your own cases file
```

The runner prints a pass/fail table per case + per check, and exits non-zero if anything fails — wires cleanly into CI later. Each case can assert against: substrings in the generated SQL (positive + negative), row count (exact or range), `post_process` value, minimum confidence, and an approximate total of the numeric values in the first returned row.

## Roadmap

**v0.1 — shipped:**
- CTB ingestion via polars + python-calamine
- Text-to-SQL via Claude Opus 4.7 with adaptive thinking, structured outputs, and prompt caching on the system prompt
- SQL safety validation (sqlglot, SELECT-only) + read-only DuckDB execution
- YoY and ratio post-processing
- Streamlit UI with audit trail (SQL + source rows + confidence)
- Multi-FY, multi-entity, multi-currency support
- FY dropdown (FY 2005-06 → FY 2049-50)
- Override semantics on duplicate FY tag

**v0.2 — shipped:**
- Context-rich error messages for non-CTB uploads (detects mapping tables, entity-level TBs, and close-but-wrong shapes)
- Per-row reconciliation check at ingest (`amount_consolidated = amount_reporting_ccy + Σ(adj_*)`); hard-fails with row-level diagnostics
- Eval harness with 10 golden Q&A pairs, grader supporting six check types, and a `ctb-eval` CLI
- 3-sheet Excel export (Answer / SQL / Source Rows) from the chat UI
- Unit test suite (23 tests across grader + export, no LLM needed)

**v0.3 — shipped:**
- AWS DocumentDB (Mongo-compatible) sync path — alternative to Excel upload
- `DatabaseSource` Protocol so Postgres/MySQL adapters slot in later without orchestrator changes
- Batched cursor (10K docs per batch) with live row-count progress
- Atomic-swap semantics — staging table + transactional flip; queries during a sync see the previous period's data, then snap to the new data on commit; failures roll back without touching `ctb_data`
- Filter builder that composes static config + selected period + per-sync overrides; supports full Mongo operators
- 20 new tests covering filter merging, doc projection, batched streaming, atomic swap, rollback, reconciliation rejection mid-sync

**v0.4 — next:**
- Field-mapping config for the next customer whose source schema differs from the canonical 22-column shape
- Streaming responses in the chat UI
- "Flag wrong" feedback loop → review queue
- Expand golden Q&As to ~30 cases (multi-FY, ratios, edge cases)
- Confidence-scoring improvements (hybrid: model self-report + deterministic signals)

**v1.0 — deployable to clients:**
- Docker image + `docker-compose.yml` + one-command setup
- S3-compatible storage adapter (S3 / R2 / MinIO)
- API-key auth
- SSO via OIDC (Microsoft 365, Google Workspace)
- Engagement workspaces (multi-tenant per-deployment)
- Additional `DatabaseSource` adapters: Postgres, MySQL, SQL Server

**Beyond:**
- LLM-assisted column mapping for variant CTB formats
- Engagement manager approval workflow + audit pack export (PDF) for working papers
- Multi-LLM support (Bedrock, Vertex, Azure) via the existing `LLMProvider` Protocol

## License

[MIT](LICENSE).

## Status & feedback

Public preview, single-developer side project. v0.3 adds the DocumentDB sync path so customers with their TB in a Mongo-compatible DB can skip Excel entirely. Combined with v0.2's ingestion guardrails, audit trail, Excel export, and eval harness, this is enough for the first real user deployment. Docker packaging, S3 storage, and auth land in v1.0. Use with verification (the UI makes it easy). PRs and issues welcome.
