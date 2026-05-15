"""Tests for the eval grader. No LLM, no DB, no config — pure logic."""

from __future__ import annotations

from ctb_copilot.eval.grader import (
    Check,
    GoldenCase,
    format_report,
    grade_case,
)


def _result(sql: str = "SELECT 1", rows: list[dict] | None = None, **kwargs) -> dict:
    return {
        "sql": sql,
        "rows": rows or [],
        "explanation": "test",
        "post_process": kwargs.get("post_process", "none"),
        "confidence": kwargs.get("confidence", "high"),
        "columns": list(rows[0].keys()) if rows else [],
    }


def test_no_checks_defaults_to_pass() -> None:
    case = GoldenCase(id="x", question="?")
    r = grade_case(case, _result())
    assert r.overall_passed
    assert len(r.checks) == 1
    assert r.checks[0].name == "no_checks_defined"


def test_sql_contains_passes() -> None:
    case = GoldenCase(id="x", question="?", checks=Check(sql_contains=["fs_category", "Assets"]))
    r = grade_case(case, _result(sql="SELECT SUM(amount_consolidated) FROM ctb_data WHERE fs_category='Assets'"))
    assert r.overall_passed


def test_sql_contains_fails_when_missing() -> None:
    case = GoldenCase(id="x", question="?", checks=Check(sql_contains=["entity_code"]))
    r = grade_case(case, _result(sql="SELECT * FROM ctb_data"))
    assert not r.overall_passed
    assert any("entity_code" in c.message for c in r.checks if not c.passed)


def test_sql_contains_is_case_insensitive() -> None:
    case = GoldenCase(id="x", question="?", checks=Check(sql_contains=["FS_CATEGORY"]))
    r = grade_case(case, _result(sql="select fs_category from ctb_data"))
    assert r.overall_passed


def test_sql_not_contains_rejects_destructive() -> None:
    case = GoldenCase(id="x", question="?", checks=Check(sql_not_contains=["DROP", "DELETE"]))
    r = grade_case(case, _result(sql="DROP TABLE ctb_data"))
    assert not r.overall_passed


def test_row_count_exact() -> None:
    case = GoldenCase(id="x", question="?", checks=Check(row_count=3))
    r = grade_case(case, _result(rows=[{"a": 1}, {"a": 2}, {"a": 3}]))
    assert r.overall_passed
    r2 = grade_case(case, _result(rows=[{"a": 1}]))
    assert not r2.overall_passed


def test_row_count_range() -> None:
    case = GoldenCase(id="x", question="?", checks=Check(row_count_min=5, row_count_max=20))
    r = grade_case(case, _result(rows=[{"i": i} for i in range(13)]))
    assert r.overall_passed
    r2 = grade_case(case, _result(rows=[{"i": 1}]))
    assert not r2.overall_passed


def test_confidence_min_high_accepts_high() -> None:
    case = GoldenCase(id="x", question="?", checks=Check(confidence_min="high"))
    r = grade_case(case, _result(confidence="high"))
    assert r.overall_passed


def test_confidence_min_high_rejects_medium() -> None:
    case = GoldenCase(id="x", question="?", checks=Check(confidence_min="high"))
    r = grade_case(case, _result(confidence="medium"))
    assert not r.overall_passed


def test_confidence_min_low_accepts_anything() -> None:
    case = GoldenCase(id="x", question="?", checks=Check(confidence_min="low"))
    assert grade_case(case, _result(confidence="low")).overall_passed
    assert grade_case(case, _result(confidence="medium")).overall_passed
    assert grade_case(case, _result(confidence="high")).overall_passed


def test_post_process_match() -> None:
    case = GoldenCase(id="x", question="?", checks=Check(post_process="yoy_pct"))
    assert grade_case(case, _result(post_process="yoy_pct")).overall_passed
    assert not grade_case(case, _result(post_process="none")).overall_passed


def test_first_row_total_approx_within_tolerance() -> None:
    case = GoldenCase(id="x", question="?", checks=Check(
        first_row_total_approx={"value": 100.0, "abs_tol": 1.0},
    ))
    r = grade_case(case, _result(rows=[{"period": "FY 2024-25", "total": 100.5}]))
    assert r.overall_passed
    r2 = grade_case(case, _result(rows=[{"period": "FY 2024-25", "total": 200.0}]))
    assert not r2.overall_passed


def test_execution_error_short_circuits() -> None:
    case = GoldenCase(id="x", question="?", checks=Check(sql_contains=["xxx"]))
    r = grade_case(case, None, error="UnsafeSQLError: nope")
    assert not r.overall_passed
    assert r.error == "UnsafeSQLError: nope"


def test_format_report_renders_pass_fail_counts() -> None:
    case = GoldenCase(id="x", question="?")
    results = [
        grade_case(case, _result()),
        grade_case(case, None, error="boom"),
    ]
    report = format_report(results)
    assert "1 / 2 passed" in report
    assert "[✓]" in report
    assert "[✗]" in report
