"""Tests for tenant-scope SQL rewriting. These are the load-bearing
tests for multi-tenant isolation at query time — even if Claude's SQL
forgets the tenant filter, the rewriter guarantees it ends up scoped."""

from __future__ import annotations

import re

import pytest
from sqlglot import exp, parse_one

from ctb_copilot.query import (
    UnsafeSQLError,
    inject_tenant_filters,
    validate_safe_select,
)
from ctb_copilot.tenants import TenantScope


def _full_scope() -> TenantScope:
    return TenantScope(
        clientId="abc-123",
        gaapId="ind_as",
        reportingParentCompanyId="rpc-001",
        currencyId="INR",
        finYearId="FY 2024-25",
        reportingPeriodId="Annual",
    )


def _scope_without_year() -> TenantScope:
    return TenantScope(
        clientId="abc-123",
        gaapId="ind_as",
        reportingParentCompanyId="rpc-001",
        currencyId="INR",
        # finYearId + reportingPeriodId left None (cross-period query)
    )


def _parse(sql: str) -> exp.Expression:
    return parse_one(sql, dialect="duckdb")


def _all_columns_in_where(rewritten: str, *cols: str) -> bool:
    """All expected column names appear in any WHERE clause."""
    tree = _parse(rewritten)
    where_text = " ".join(w.sql(dialect="duckdb") for w in tree.find_all(exp.Where))
    return all(col in where_text for col in cols)


# ---------- happy path: basic injection ----------


def test_inject_adds_tenant_to_select_without_where() -> None:
    sql = "SELECT * FROM ctb_data"
    out = inject_tenant_filters(sql, _full_scope())
    assert _all_columns_in_where(out, "client_id", "gaap_id", "reporting_parent_company_id",
                                  "currency_id", "fin_year_id", "reporting_period_id")
    assert "'abc-123'" in out
    assert "'ind_as'" in out


def test_inject_preserves_existing_where_via_and() -> None:
    sql = "SELECT SUM(amount_consolidated) FROM ctb_data WHERE fs_category = 'Assets'"
    out = inject_tenant_filters(sql, _full_scope())
    # Original filter still there
    assert "fs_category" in out and "Assets" in out
    # All 6 tenant filters added
    assert _all_columns_in_where(out, "client_id", "gaap_id", "reporting_parent_company_id",
                                  "currency_id", "fin_year_id", "reporting_period_id")


def test_inject_only_adds_filters_present_in_scope() -> None:
    sql = "SELECT * FROM ctb_data"
    out = inject_tenant_filters(sql, _scope_without_year())
    # 4 required filters present
    assert _all_columns_in_where(out, "client_id", "gaap_id", "reporting_parent_company_id", "currency_id")
    # Optional 2 absent (no fin_year_id / reporting_period_id constraint)
    # Look at the WHERE-only text to avoid false positives from column lists
    tree = _parse(out)
    where_text = " ".join(w.sql(dialect="duckdb") for w in tree.find_all(exp.Where))
    assert "fin_year_id" not in where_text
    assert "reporting_period_id" not in where_text


# ---------- doesn't touch non-ctb_data ----------


def test_inject_ignores_selects_that_dont_scan_ctb_data() -> None:
    sql = "SELECT * FROM ingestions"
    out = inject_tenant_filters(sql, _full_scope())
    tree = _parse(out)
    where_clauses = list(tree.find_all(exp.Where))
    # The Select scans ingestions, not ctb_data — no WHERE added
    assert where_clauses == []


# ---------- subqueries and CTEs ----------


def test_inject_scopes_subquery_that_scans_ctb_data() -> None:
    sql = "SELECT * FROM (SELECT * FROM ctb_data) sub"
    out = inject_tenant_filters(sql, _full_scope())
    # The inner select gets all 6 filters
    tree = _parse(out)
    inner_wheres = [w.sql(dialect="duckdb") for w in tree.find_all(exp.Where)]
    assert any("client_id" in w for w in inner_wheres)


def test_inject_scopes_cte_that_scans_ctb_data() -> None:
    sql = "WITH ct AS (SELECT * FROM ctb_data WHERE fs_category='Assets') SELECT * FROM ct"
    out = inject_tenant_filters(sql, _full_scope())
    tree = _parse(out)
    # Find the CTE's inner Select; its WHERE should include client_id
    cte_wheres = [w.sql(dialect="duckdb") for w in tree.find_all(exp.Where)]
    assert any("client_id" in w and "Assets" in w for w in cte_wheres)


# ---------- safety: rewrite is purely additive ----------


def test_rewritten_sql_still_passes_safety_validator() -> None:
    sql = "SELECT fs_category, SUM(amount_consolidated) FROM ctb_data GROUP BY fs_category"
    out = inject_tenant_filters(sql, _full_scope())
    # No exception
    validate_safe_select(out)


def test_inject_cannot_turn_select_into_destructive_statement() -> None:
    sql = "SELECT * FROM ctb_data"
    out = inject_tenant_filters(sql, _full_scope())
    tree = _parse(out)
    # Still a SELECT-ish tree, never a write
    assert isinstance(tree, (exp.Select, exp.Union, exp.With))
    for node in tree.walk():
        assert not isinstance(node, (exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.Alter, exp.Create))


# ---------- value handling ----------


def test_inject_uses_string_literals_for_id_values() -> None:
    sql = "SELECT * FROM ctb_data"
    out = inject_tenant_filters(sql, _full_scope())
    # The literal values appear with quotes — string literals, not column refs
    assert "'abc-123'" in out
    assert "'FY 2024-25'" in out
    assert "'Annual'" in out


def test_inject_handles_apostrophes_in_values() -> None:
    """Make sure a value with an apostrophe doesn't break the SQL.
    sqlglot's serializer escapes it via standard SQL doubling."""
    weird_scope = TenantScope(
        clientId="O'Reilly Group",
        gaapId="ind_as",
        reportingParentCompanyId="rpc",
        currencyId="INR",
    )
    sql = "SELECT * FROM ctb_data"
    out = inject_tenant_filters(sql, weird_scope)
    # Re-parsing must succeed — proves the escape was correct
    tree = _parse(out)
    assert isinstance(tree, exp.Select)
    # The escaped form appears verbatim in the SQL
    assert "O''Reilly Group" in out or "O\\'Reilly Group" in out


# ---------- edge cases ----------


def test_inject_passes_through_when_no_pairs_in_scope() -> None:
    """If somehow scope is empty (won't happen in practice — TenantScope
    requires the 4 mandatory fields), the SQL should be returned as-is."""
    sql = "SELECT * FROM ctb_data"

    class EmptyScope:
        def as_filter_pairs(self):
            return []

    out = inject_tenant_filters(sql, EmptyScope())  # type: ignore[arg-type]
    assert out == sql


def test_inject_handles_aliased_ctb_data_reference() -> None:
    """sqlglot Table.name returns the underlying table name, so an alias
    shouldn't hide the reference."""
    sql = "SELECT t.fs_category FROM ctb_data AS t"
    out = inject_tenant_filters(sql, _full_scope())
    assert _all_columns_in_where(out, "client_id")
