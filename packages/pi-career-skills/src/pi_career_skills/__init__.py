"""pi-career-skills: Pi-py port of the career assistant job skills.

A run-level eval runtime that migrates the four career skills
(job-discovery / job-matching / resume-tailoring / career-planning), the 13 deterministic
career tools, and the trusted-kernel harness semantics (evidence store,
completion gate, budgets, stall, bounded auto-recovery) onto pi-py agents.

This is an evaluation/parity runtime, not a production backend: state is
per-run in memory; MySQL/Redis/MinIO/API/SSE are out of scope.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
