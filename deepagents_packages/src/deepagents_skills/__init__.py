"""deepagents-skills: pi-career-skills harness on deepagents + middleware.

Keeps the thin-supervisor + 4-skill-subagent structure and the run-level
harness semantics (evidence store, completion gates, budgets, stall,
bounded auto-recovery) while swapping the agent runtime from pi-py hooks
to deepagents AgentMiddleware.  The career business layer is reused from
``pi_career_skills`` unchanged (see MIGRATION.md).
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
