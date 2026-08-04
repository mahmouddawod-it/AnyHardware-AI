"""CPU-only adaptive runner for local GGUF models."""

from .planner import Machine, RunPlan, plan_run

__all__ = ["Machine", "RunPlan", "plan_run"]
