"""DocumentDBSource — pull canonical-shape TB rows from AWS DocumentDB.

DocumentDB speaks the Mongo wire protocol, so pymongo is the right client.
We open a cursor with batch_size=BATCH_SIZE, yield batches of docs already
shaped to the canonical column names, and the orchestrator handles the
DuckDB insert + atomic swap.

Field naming assumption (v0.3): the source collection's field names already
match the canonical names (consol_gl_code, fs_category, amount_consolidated,
etc.). The customer's TB does match for our first user; for the next user
whose schema differs, we'll add a `field_mapping` env var that translates
their names to canonical names before yielding. v0.4.
"""

from __future__ import annotations

from typing import Any, Iterator

from pymongo import MongoClient

from ctb_copilot.ports.source import DatabaseSource, SyncProgress

BATCH_SIZE = 10_000

# Canonical fields the orchestrator expects in each yielded row. Same set
# as INSERT columns in ingest.py minus the three provenance columns
# (upload_id, row_number, period).
CANONICAL_FIELDS: tuple[str, ...] = (
    "consol_gl_code",
    "consol_gl_description",
    "entity_name",
    "entity_code",
    "gl_nature",
    "fs_category",
    "bs_classification",
    "fsli",
    "grouping",
    "sub_grouping",
    "functional_currency",
    "amount_functional_ccy",
    "amount_reporting_ccy",
    "adj_other_consolidated",
    "adj_nci",
    "adj_goodwill",
    "adj_ppa",
    "adj_intercompany",
    "adj_investment_capital",
    "adj_retained_earnings",
    "adj_fctr",
    "amount_consolidated",
)


def build_filter(
    *,
    default_filter: dict[str, Any],
    period: str,
    period_field: str,
    reporting_period_field: str | None = None,
    reporting_period: str | None = None,
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Merge the static + variable + override filter components into the
    final dict passed to pymongo `find()`.

    Priority (later wins): default_filter < period filter < reporting period
    < overrides. Returns a fresh dict; never mutates inputs.
    """
    merged: dict[str, Any] = dict(default_filter or {})
    merged[period_field] = period
    if reporting_period and reporting_period_field:
        merged[reporting_period_field] = reporting_period
    if overrides:
        merged.update(overrides)
    return merged


def _project_doc(doc: dict[str, Any]) -> dict[str, Any]:
    """Pick only the canonical fields out of a source document.

    Anything extra (e.g. Mongo's _id, audit fields, client-specific columns)
    is dropped silently. If a canonical field is missing, the row gets None
    for that key and the reconciliation check downstream will surface any
    structural problems.
    """
    return {field: doc.get(field) for field in CANONICAL_FIELDS}


def _project_consolidation_final_tb(doc: dict[str, Any]) -> dict[str, Any]:
    """Map a Uniqus `ConsolidationFinalTB` Mongo document to the canonical
    22-column shape.

    Source shape (Java/Spring `com.uniqus.core.models.mongo.ConsolidationFinalTB`):
      - consoleGlCode, consoleGlDesc, entityGlName
      - fsType ("Balance Sheet" / "Statement of P&L")
      - fsCategory, fsSubCategory, fsli, groupings, subGroupings
      - amountInFunctionalCurrency  → {currency, value, rateType}
      - amountInLocalCurrencyRecord → list[{currency, value, rateType}]

    Final-TB rows already carry the consolidated number (this is the
    post-consolidation collection), so the per-adjustment columns are set
    to 0.0. The reconciliation invariant
        amount_consolidated == amount_reporting_ccy + Σ(adj_*)
    holds trivially because reporting == consolidated and adjustments are 0.
    """
    func = doc.get("amountInFunctionalCurrency") or {}
    local_records = doc.get("amountInLocalCurrencyRecord") or []
    # FinalTB stores the reporting-currency value as a list (one entry per
    # rate-conversion path). Summing is safe: single-rate rows have one
    # entry; multi-rate rows each represent an independent slice that adds
    # to the consolidated total.
    consolidated = float(sum((rec.get("value") or 0.0) for rec in local_records))
    entity = doc.get("entityGlName")
    return {
        "consol_gl_code": doc.get("consoleGlCode"),
        "consol_gl_description": doc.get("consoleGlDesc"),
        "entity_name": entity,
        "entity_code": entity,  # FinalTB has no separate code; mirror the name
        "gl_nature": doc.get("fsType"),
        "fs_category": doc.get("fsCategory"),
        "bs_classification": doc.get("fsSubCategory"),
        "fsli": doc.get("fsli"),
        "grouping": doc.get("groupings"),
        "sub_grouping": doc.get("subGroupings"),
        "functional_currency": func.get("currency"),
        "amount_functional_ccy": func.get("value"),
        "amount_reporting_ccy": consolidated,
        "adj_other_consolidated": 0.0,
        "adj_nci": 0.0,
        "adj_goodwill": 0.0,
        "adj_ppa": 0.0,
        "adj_intercompany": 0.0,
        "adj_investment_capital": 0.0,
        "adj_retained_earnings": 0.0,
        "adj_fctr": 0.0,
        "amount_consolidated": consolidated,
    }


def _project_console_append_tb(doc: dict[str, Any]) -> dict[str, Any]:
    """Map a Uniqus `ConsoleAppendTB` Mongo document to the canonical
    22-column shape.

    Source shape (Java/Spring `com.uniqus.core.models.mongo.ConsoleAppendTB`):
      - consoleGlCode, consoleGlDesc, entityCode, entityName, entityGlCode
      - fsType ("Balance Sheet" / "Statement of P&L")
      - fsCategory, fsSubCategory, fsli, groupings, subGroupings
      - amountInFunctionalCurrency   → {currency, value, …}
      - amountInLocalCurrency        → {currency, value, …}
      - otherAdjustmentAmount, nciAmount, goodWillAmount, ppaAmount,
        iceAmount, iceShareCapitalAmount, retainedEarningsAmount,
        fctrAmount                    → each {value, …}
      - totalBalance                  → running consolidated total

    AppendTB is the row-level pivot: every adjustment module $inc-s into
    one of the *Amount fields. Currency semantics are the critical bit:

      - `amountInLocalCurrency`     → entity's own books, currency VARIES
                                      per entity (INR for an Indian sub,
                                      USD for a US sub, …). Cannot be
                                      summed across entities. Maps to
                                      ctb-copilot's `amount_functional_ccy`
                                      (the entity-functional view).

      - `amountInFunctionalCurrency`→ post-FX, in the group's reporting
                                      currency. All rows in same currency,
                                      so this is the one you sum to get
                                      consolidated totals. Maps to
                                      ctb-copilot's `amount_reporting_ccy`.

      - All `*Amount` adjustment fields are also in the group's reporting
        currency, alongside `amountInFunctionalCurrency`.

      - `totalBalance` is supposed to equal
            amountInFunctionalCurrency.value + Σ(adjustments)
        but the platform appears to populate it only on adjustment-POST
        cycles — a fresh "appended, not yet adjusted" scope has
        totalBalance=0 across the board. So we compute amount_consolidated
        from the components instead of trusting totalBalance; the two
        agree once the platform has fully posted.

    Naming clash to beware: Mongo "Functional" = group's functional/
    reporting currency (consistent across rows). ctb-copilot's
    `amount_functional_ccy` historically means the ENTITY's own books
    (varies per entity). So the two "functional"s are *opposite*; we
    map by SEMANTICS, not name.
    """

    def _val(field: str) -> float | None:
        sub = doc.get(field)
        if isinstance(sub, dict):
            return sub.get("value")
        return None

    local = doc.get("amountInLocalCurrency") or {}
    adjustments = [
        _val("otherAdjustmentAmount"),
        _val("nciAmount"),
        _val("goodWillAmount"),
        _val("ppaAmount"),
        _val("iceAmount"),
        _val("iceShareCapitalAmount"),
        _val("retainedEarningsAmount"),
        _val("fctrAmount"),
    ]
    reporting = _val("amountInFunctionalCurrency")
    consolidated = (reporting or 0.0) + sum((a or 0.0) for a in adjustments)
    return {
        "consol_gl_code": doc.get("consoleGlCode"),
        "consol_gl_description": doc.get("consoleGlDesc"),
        "entity_name": doc.get("entityName"),
        "entity_code": doc.get("entityCode"),
        "gl_nature": doc.get("fsType"),
        "fs_category": doc.get("fsCategory"),
        "bs_classification": doc.get("fsSubCategory"),
        "fsli": doc.get("fsli"),
        "grouping": doc.get("groupings"),
        "sub_grouping": doc.get("subGroupings"),
        # Entity's own books — currency varies per entity.
        "functional_currency": local.get("currency") if isinstance(local, dict) else None,
        "amount_functional_ccy": _val("amountInLocalCurrency"),
        # Group reporting currency — all rows aligned. Safe to sum.
        "amount_reporting_ccy": reporting,
        "adj_other_consolidated": adjustments[0],
        "adj_nci": adjustments[1],
        "adj_goodwill": adjustments[2],
        "adj_ppa": adjustments[3],
        "adj_intercompany": adjustments[4],
        "adj_investment_capital": adjustments[5],
        "adj_retained_earnings": adjustments[6],
        "adj_fctr": adjustments[7],
        "amount_consolidated": consolidated,
    }


_PROJECTORS = {
    "canonical": _project_doc,
    "consolidation_final_tb": _project_consolidation_final_tb,
    "console_append_tb": _project_console_append_tb,
}


def _project_for(shape: str):
    """Look up the projector for a given DOCDB_DOC_SHAPE."""
    if shape not in _PROJECTORS:
        raise ValueError(
            f"Unknown DOCDB_DOC_SHAPE={shape!r}. Expected one of {list(_PROJECTORS)}."
        )
    return _PROJECTORS[shape]


class DocumentDBSource(DatabaseSource):
    """pymongo-backed implementation of DatabaseSource for AWS DocumentDB."""

    def __init__(
        self,
        uri: str,
        database: str,
        collection: str,
        doc_shape: str = "canonical",
    ) -> None:
        self.uri = uri
        self.database = database
        self.collection_name = collection
        self.project = _project_for(doc_shape)

    def stream_rows(
        self,
        *,
        filter_doc: dict[str, Any],
        batch_size: int = BATCH_SIZE,
        progress: SyncProgress | None = None,
    ) -> Iterator[list[dict[str, Any]]]:
        """Open a cursor against `find(filter_doc)` and yield batches of
        canonical-shape row dicts.

        Connection is opened on first iteration and closed when iteration
        completes or the generator is closed. Server-side cursor is
        paginated by batch_size so we never materialize all docs at once.
        """
        client = MongoClient(self.uri)
        try:
            collection = client[self.database][self.collection_name]
            cursor = collection.find(filter_doc, batch_size=batch_size)
            batch: list[dict[str, Any]] = []
            for doc in cursor:
                batch.append(self.project(doc))
                if len(batch) >= batch_size:
                    if progress is not None:
                        progress.report(len(batch))
                    yield batch
                    batch = []
            if batch:
                if progress is not None:
                    progress.report(len(batch))
                yield batch
        finally:
            client.close()
