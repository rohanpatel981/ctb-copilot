"""Eval runner — wires golden YAML cases through run_query and grades each.

The `run_eval` function takes injected dependencies (llm, db_path) so it can
be tested or scripted. The `run()` entry point pulls from `config.settings`
and is the one wired up to `ctb-eval` in pyproject.toml.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import yaml

from ctb_copilot.eval.grader import CaseResult, GoldenCase, format_report, grade_case
from ctb_copilot.ports.llm import LLMProvider
from ctb_copilot.query import run_query


def load_cases(yaml_path: Path) -> list[GoldenCase]:
    raw = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError(f"{yaml_path} must contain a top-level list of cases.")
    return [GoldenCase.model_validate(c) for c in raw]


async def run_eval(
    yaml_path: Path,
    llm: LLMProvider,
    db_path: Path,
) -> list[CaseResult]:
    cases = load_cases(yaml_path)
    results: list[CaseResult] = []
    for case in cases:
        try:
            qr = await run_query(question=case.question, llm=llm, db_path=db_path)
            results.append(grade_case(case, qr.model_dump()))
        except Exception as e:
            results.append(grade_case(case, None, error=f"{type(e).__name__}: {e}"))
    return results


def _default_yaml_path() -> Path:
    return Path(__file__).parent / "golden.yaml"


def run() -> None:
    """Entry point for `uv run ctb-eval [path/to/cases.yaml]`."""
    from ctb_copilot.adapters.llm_anthropic import AnthropicLLM
    from ctb_copilot.config import settings

    yaml_path = Path(sys.argv[1]) if len(sys.argv) > 1 else _default_yaml_path()
    if not yaml_path.exists():
        print(f"eval: cases file not found: {yaml_path}", file=sys.stderr)
        sys.exit(2)

    llm = AnthropicLLM(api_key=settings.anthropic_api_key, model=settings.anthropic_model)
    results = asyncio.run(run_eval(yaml_path, llm, settings.duckdb_path))
    print(format_report(results))
    failures = sum(1 for r in results if not r.overall_passed)
    sys.exit(0 if failures == 0 else 1)
