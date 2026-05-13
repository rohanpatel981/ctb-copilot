"""Text-to-SQL orchestration: LLM → validate → execute (read-only) → post-process.

Validation is two layers: sqlglot parses the SQL and rejects anything that isn't
a single SELECT (no INSERT/UPDATE/DELETE/DDL/ATTACH/COPY/PRAGMA), then we open
the DB in read-only mode as defence in depth. If the parser is fooled, the
DB itself will reject the write.
"""

from pathlib import Path

from pydantic import BaseModel
from sqlglot import exp, parse_one

from ctb_copilot.db import LLM_SCHEMA_DDL, ro_connection
from ctb_copilot.ports.llm import LLMProvider, SQLPlan


class YoYChange(BaseModel):
    from_period: str
    to_period: str
    metric: str
    from_value: float | None
    to_value: float | None
    pct_change: float | None


class QueryResult(BaseModel):
    question: str
    sql: str
    explanation: str
    columns: list[str]
    rows: list[dict]
    post_process: str
    yoy_changes: list[YoYChange] = []
    ratios: list[dict] = []
    confidence: str


class UnsafeSQLError(ValueError):
    pass


_FORBIDDEN_NODES = (
    exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.Alter, exp.Create,
    exp.Merge, exp.Command,
)


def validate_safe_select(sql: str) -> None:
    """Raise UnsafeSQLError if `sql` is anything other than a single SELECT."""
    try:
        tree = parse_one(sql, dialect="duckdb")
    except Exception as e:
        raise UnsafeSQLError(f"Could not parse SQL: {e}") from e

    if tree is None:
        raise UnsafeSQLError("Empty SQL.")

    if not isinstance(tree, (exp.Select, exp.Union, exp.With)):
        raise UnsafeSQLError(f"Only SELECT statements are allowed; got {type(tree).__name__}.")

    for node in tree.walk():
        if isinstance(node, _FORBIDDEN_NODES):
            raise UnsafeSQLError(f"Forbidden statement type: {type(node).__name__}.")


def _compute_yoy(rows: list[dict], columns: list[str]) -> list[YoYChange]:
    """Compute YoY % change for each numeric column between consecutive periods.

    Expects rows already ordered by period. Sign convention is preserved: dividing
    by the signed prior-period value naturally handles liabilities/revenue.
    Returns empty list if `period` isn't in columns.
    """
    if "period" not in columns or len(rows) < 2:
        return []
    numeric_cols = [
        c for c in columns
        if c != "period" and any(isinstance(r.get(c), (int, float)) for r in rows)
    ]
    changes: list[YoYChange] = []
    for prev, curr in zip(rows, rows[1:]):
        for col in numeric_cols:
            prev_val = prev.get(col)
            curr_val = curr.get(col)
            if not isinstance(prev_val, (int, float)) or not isinstance(curr_val, (int, float)):
                continue
            if prev_val == 0:
                pct = None
            else:
                pct = round((curr_val - prev_val) / prev_val * 100, 4)
            changes.append(YoYChange(
                from_period=str(prev["period"]),
                to_period=str(curr["period"]),
                metric=col,
                from_value=float(prev_val),
                to_value=float(curr_val),
                pct_change=pct,
            ))
    return changes


def _compute_ratio(rows: list[dict], columns: list[str]) -> list[dict]:
    """If two numeric columns exist (besides period), return col1/col2 per row."""
    numeric_cols = [
        c for c in columns
        if c != "period" and any(isinstance(r.get(c), (int, float)) for r in rows)
    ]
    if len(numeric_cols) < 2:
        return []
    num_col, den_col = numeric_cols[0], numeric_cols[1]
    ratios = []
    for r in rows:
        num, den = r.get(num_col), r.get(den_col)
        value = (num / den) if isinstance(num, (int, float)) and isinstance(den, (int, float)) and den != 0 else None
        entry = {"numerator": num_col, "denominator": den_col, "value": value}
        if "period" in columns:
            entry["period"] = r.get("period")
        ratios.append(entry)
    return ratios


async def run_query(*, question: str, llm: LLMProvider, db_path: Path) -> QueryResult:
    plan: SQLPlan = await llm.generate_sql_plan(
        schema_ddl=LLM_SCHEMA_DDL,
        question=question,
    )
    validate_safe_select(plan.sql)

    with ro_connection(db_path) as conn:
        cursor = conn.execute(plan.sql)
        columns = [d[0] for d in cursor.description]
        rows_raw = cursor.fetchall()
    rows = [dict(zip(columns, r)) for r in rows_raw]

    yoy_changes: list[YoYChange] = []
    ratios: list[dict] = []
    if plan.post_process == "yoy_pct":
        yoy_changes = _compute_yoy(rows, columns)
    elif plan.post_process == "ratio":
        ratios = _compute_ratio(rows, columns)

    return QueryResult(
        question=question,
        sql=plan.sql,
        explanation=plan.explanation,
        columns=columns,
        rows=rows,
        post_process=plan.post_process,
        yoy_changes=yoy_changes,
        ratios=ratios,
        confidence=plan.confidence,
    )
