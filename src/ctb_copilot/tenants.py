"""Tenant-identity types.

A CTB dataset is uniquely identified by the 6-tuple:
  (clientId, gaapId, reportingParentCompanyId, finYearId, reportingPeriodId, currencyId)

Every row in ctb_data carries all 6 columns so data from different
tenants is structurally non-overlapping. At sync time all 6 are required.
At query time finYearId and reportingPeriodId are optional (so YoY and
cross-period questions still work); the other 4 are required.

`status` is NOT part of the identity. Every row we ingest is implicitly
status='ACTIVE' because the sync orchestrator pins that in the Mongo
filter and we don't store inactive records.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class TenantSync(BaseModel):
    """Full 6-tuple required at sync time. FE sends in camelCase; we
    accept either alias (`clientId`) or snake_case (`client_id`)."""

    model_config = ConfigDict(populate_by_name=True, str_strip_whitespace=True)

    client_id: str = Field(alias="clientId", min_length=1)
    gaap_id: str = Field(alias="gaapId", min_length=1)
    reporting_parent_company_id: str = Field(alias="reportingParentCompanyId", min_length=1)
    fin_year_id: str = Field(alias="finYearId", min_length=1)
    reporting_period_id: str = Field(alias="reportingPeriodId", min_length=1)
    currency_id: str = Field(alias="currencyId", min_length=1)

    def as_dict(self) -> dict[str, str]:
        """Snake-case dict of all 6 fields, suitable for SQL parameter binding."""
        return {
            "client_id": self.client_id,
            "gaap_id": self.gaap_id,
            "reporting_parent_company_id": self.reporting_parent_company_id,
            "fin_year_id": self.fin_year_id,
            "reporting_period_id": self.reporting_period_id,
            "currency_id": self.currency_id,
        }


class TenantScope(BaseModel):
    """Query-time scope. 4 fields required, 2 optional (so YoY queries work)."""

    model_config = ConfigDict(populate_by_name=True, str_strip_whitespace=True)

    client_id: str = Field(alias="clientId", min_length=1)
    gaap_id: str = Field(alias="gaapId", min_length=1)
    reporting_parent_company_id: str = Field(alias="reportingParentCompanyId", min_length=1)
    currency_id: str = Field(alias="currencyId", min_length=1)
    fin_year_id: str | None = Field(default=None, alias="finYearId")
    reporting_period_id: str | None = Field(default=None, alias="reportingPeriodId")

    def as_filter_pairs(self) -> list[tuple[str, str]]:
        """Snake-case (column, value) pairs for the columns the server
        auto-injects into every query as tenant scope.

        Only the 4 true tenant-identity columns (client, gaap, parent,
        currency). `fin_year_id` and `reporting_period_id` are NOT
        injected even when the scope carries them — those UUIDs name a
        single (year, period) combo, so injecting them would lock every
        query to one year and silently break cross-period questions like
        "diff between FY 26-27 and FY 27-28". Time filtering is handled
        by the LLM choosing values from `fin_year_period` (the
        human-readable label column) per the system prompt.
        """
        return [
            ("client_id", self.client_id),
            ("gaap_id", self.gaap_id),
            ("reporting_parent_company_id", self.reporting_parent_company_id),
            ("currency_id", self.currency_id),
        ]


# The 6 tenant ID column names, in canonical order. Used by db.py for
# schema generation and by ingest/sync for record building. Kept in one
# place so adding/removing a tenant key is a single-line change.
TENANT_ID_COLUMNS: tuple[str, ...] = (
    "client_id",
    "gaap_id",
    "reporting_parent_company_id",
    "fin_year_id",
    "reporting_period_id",
    "currency_id",
)
