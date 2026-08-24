"""Evaluation package — schema, audit, runner, chain, compare, and CLI.

Public API for the Phase 8 evaluation system.  Task K owns the core
(schema/audit/profile_facts/seed_urls); Task L adds runner/chain/compare/CLI.
"""

from .audit import audit_chain, audit_record, is_regression_error_code
from .chain import run_chain
from .compare import compare_pi_to_source, merge_results, render_comparison_md
from .profile_facts import build_profile_facts
from .runner import ALL_SKILLS, run_question
from .schema import (
    EvalArtifact,
    EvalAttempt,
    EvalBudgetConsumed,
    EvalBudgetLimits,
    EvalChainLinkRecord,
    EvalChainRecord,
    EvalConfig,
    EvalEvent,
    EvalModel,
    EvalRecord,
    EvalRecordUnion,
    EvalResult,
    EvalRuntime,
    validate_record,
    validate_records,
)
from .seed_urls import SEED_URLS, resolve_seed_urls

__all__ = [
    # schema
    "EvalArtifact",
    "EvalAttempt",
    "EvalBudgetConsumed",
    "EvalBudgetLimits",
    "EvalChainLinkRecord",
    "EvalChainRecord",
    "EvalConfig",
    "EvalEvent",
    "EvalModel",
    "EvalRecord",
    "EvalRecordUnion",
    "EvalResult",
    "EvalRuntime",
    "validate_record",
    "validate_records",
    # audit
    "audit_chain",
    "audit_record",
    "is_regression_error_code",
    # profile_facts
    "build_profile_facts",
    # seed_urls
    "ALL_SKILLS",
    "SEED_URLS",
    "resolve_seed_urls",
    # runner (Task L)
    "run_question",
    # chain (Task L)
    "run_chain",
    # compare (Task L)
    "compare_pi_to_source",
    "merge_results",
    "render_comparison_md",
]
