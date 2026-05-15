"""Eval grader — pure-logic comparison of QueryResult against expected checks.

Has no LLM or config dependencies, so it can be unit-tested freely. The runner
in `runner.py` calls the LLM, then hands the result dict to `grade_case` here.
"""

from __future__ import annotations

import math
from typing import Any, Literal

from pydantic import BaseModel, Field


class Check(BaseModel):
    """Expected behavior for a single golden case. All checks are optional."""

    sql_contains: list[str] = Field(default_factory=list, description="Case-insensitive substrings that MUST appear in the generated SQL.")
    sql_not_contains: list[str] = Field(default_factory=list, description="Case-insensitive substrings that MUST NOT appear (e.g. ABS, DROP).")
    row_count: int | None = Field(default=None, description="Exact expected row count.")
    row_count_min: int | None = Field(default=None, description="Minimum acceptable row count (alternative to row_count).")
    row_count_max: int | None = Field(default=None, description="Maximum acceptable row count (alternative to row_count).")
    post_process: Literal["none", "yoy_pct", "ratio"] | None = None
    confidence_min: Literal["low", "medium", "high"] | None = Field(default=None, description="Minimum acceptable confidence.")
    first_row_total_approx: dict | None = Field(default=None, description="Approximate target for the sum of numeric values in the first row: {value: float, abs_tol: float}.")


class GoldenCase(BaseModel):
    id: str
    question: str
    description: str = ""
    checks: Check = Field(default_factory=Check)
    notes: str = ""


class CheckResult(BaseModel):
    name: str
    passed: bool
    message: str = ""


class CaseResult(BaseModel):
    case_id: str
    question: str
    overall_passed: bool
    checks: list[CheckResult]
    sql: str = ""
    explanation: str = ""
    error: str | None = None


_CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}


def _sum_numeric(row: dict) -> float:
    return sum(v for v in row.values() if isinstance(v, (int, float)) and not isinstance(v, bool))


def grade_case(case: GoldenCase, result: dict[str, Any] | None, error: str | None = None) -> CaseResult:
    """Compare a QueryResult dict against a GoldenCase's expected checks."""
    if error is not None or result is None:
        return CaseResult(
            case_id=case.id,
            question=case.question,
            overall_passed=False,
            checks=[CheckResult(name="execution", passed=False, message=error or "no result")],
            error=error,
        )

    sql = result.get("sql", "")
    sql_lower = sql.lower()
    rows = result.get("rows", [])
    checks: list[CheckResult] = []

    for needle in case.checks.sql_contains:
        ok = needle.lower() in sql_lower
        checks.append(CheckResult(
            name=f"sql_contains:{needle}",
            passed=ok,
            message="" if ok else f"SQL missing required substring {needle!r}",
        ))

    for needle in case.checks.sql_not_contains:
        ok = needle.lower() not in sql_lower
        checks.append(CheckResult(
            name=f"sql_not_contains:{needle}",
            passed=ok,
            message="" if ok else f"SQL contains forbidden substring {needle!r}",
        ))

    if case.checks.row_count is not None:
        actual = len(rows)
        ok = actual == case.checks.row_count
        checks.append(CheckResult(
            name="row_count",
            passed=ok,
            message="" if ok else f"row count {actual} != expected {case.checks.row_count}",
        ))

    if case.checks.row_count_min is not None:
        actual = len(rows)
        ok = actual >= case.checks.row_count_min
        checks.append(CheckResult(
            name="row_count_min",
            passed=ok,
            message="" if ok else f"row count {actual} < min {case.checks.row_count_min}",
        ))

    if case.checks.row_count_max is not None:
        actual = len(rows)
        ok = actual <= case.checks.row_count_max
        checks.append(CheckResult(
            name="row_count_max",
            passed=ok,
            message="" if ok else f"row count {actual} > max {case.checks.row_count_max}",
        ))

    if case.checks.post_process is not None:
        actual_pp = result.get("post_process", "none")
        ok = actual_pp == case.checks.post_process
        checks.append(CheckResult(
            name="post_process",
            passed=ok,
            message="" if ok else f"post_process={actual_pp!r}, expected {case.checks.post_process!r}",
        ))

    if case.checks.confidence_min is not None:
        actual_conf = result.get("confidence", "low")
        actual_rank = _CONFIDENCE_RANK.get(actual_conf, 0)
        expected_rank = _CONFIDENCE_RANK.get(case.checks.confidence_min, 0)
        ok = actual_rank >= expected_rank
        checks.append(CheckResult(
            name="confidence_min",
            passed=ok,
            message="" if ok else f"confidence={actual_conf!r}, expected ≥ {case.checks.confidence_min!r}",
        ))

    if case.checks.first_row_total_approx is not None:
        spec = case.checks.first_row_total_approx
        target = spec["value"]
        tol = spec.get("abs_tol", 1.0)
        if not rows:
            checks.append(CheckResult(name="first_row_total_approx", passed=False, message="no rows returned"))
        else:
            total = _sum_numeric(rows[0])
            ok = math.isclose(total, target, abs_tol=tol, rel_tol=1e-6)
            checks.append(CheckResult(
                name="first_row_total_approx",
                passed=ok,
                message="" if ok else f"first row numeric sum={total:,.2f}, expected ≈{target:,.2f} (±{tol})",
            ))

    if not checks:
        checks.append(CheckResult(name="no_checks_defined", passed=True, message="(case has no checks — counts as pass)"))

    return CaseResult(
        case_id=case.id,
        question=case.question,
        overall_passed=all(c.passed for c in checks),
        checks=checks,
        sql=sql,
        explanation=result.get("explanation", ""),
    )


def format_report(results: list[CaseResult]) -> str:
    """Pretty-print a list of CaseResult into a string report."""
    lines: list[str] = []
    passed = sum(1 for r in results if r.overall_passed)
    total = len(results)

    for r in results:
        icon = "✓" if r.overall_passed else "✗"
        lines.append(f"[{icon}] {r.case_id}")
        lines.append(f"    Q: {r.question}")
        for c in r.checks:
            cicon = "✓" if c.passed else "✗"
            tail = f"  — {c.message}" if c.message else ""
            lines.append(f"      {cicon} {c.name}{tail}")
        if not r.overall_passed and r.sql:
            sql_preview = r.sql.replace("\n", " ").strip()
            if len(sql_preview) > 100:
                sql_preview = sql_preview[:97] + "..."
            lines.append(f"      SQL: {sql_preview}")
        if r.error:
            lines.append(f"      ERROR: {r.error}")
        lines.append("")

    bar = "━" * 72
    pct = (100 * passed / total) if total else 0
    lines.append(bar)
    lines.append(f"  {passed} / {total} passed  ({pct:.0f}%)")
    return "\n".join(lines)
