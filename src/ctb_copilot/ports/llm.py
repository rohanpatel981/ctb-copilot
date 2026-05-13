from typing import Literal, Protocol

from pydantic import BaseModel, Field


class SQLPlan(BaseModel):
    """What the LLM returns for a text-to-SQL request."""

    sql: str = Field(..., description="A single SELECT statement against ctb_data.")
    explanation: str = Field(
        ...,
        description=(
            "One-paragraph explanation of what the query does, intended for a CA "
            "reviewer. Mention assumptions (sign convention, which amount column "
            "was used, whether entities were rolled up)."
        ),
    )
    post_process: Literal["none", "yoy_pct", "ratio"] = Field(
        default="none",
        description=(
            "Optional post-processing step applied to the result rows after SQL execution. "
            "'yoy_pct' computes year-over-year percentage change between consecutive periods."
        ),
    )
    confidence: Literal["high", "medium", "low"] = Field(
        ...,
        description=(
            "Self-reported confidence based on whether the question maps cleanly to the "
            "schema, whether assumptions had to be made, and whether the result is "
            "expected to be auditable without ambiguity."
        ),
    )


class LLMProvider(Protocol):
    """The text-to-SQL surface. Adapters: AnthropicLLM (v1), BedrockLLM (v2)."""

    async def generate_sql_plan(
        self,
        *,
        schema_ddl: str,
        sample_rows: str,
        question: str,
    ) -> SQLPlan: ...
